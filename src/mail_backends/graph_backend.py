"""Microsoft Graph mail backend (Outlook.com + Microsoft 365)."""
from __future__ import annotations

import base64
import logging
import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import httpx

from ..body_format import compose_body, render_body
from ..utils import OUTBOUND_TRACKING_HEADER
from .accounts import MailAccount
from .base import (
    MailAttachmentContent,
    MailAttachmentInfo,
    MailBackend,
    MailBackendError,
    MailFolder,
    MailMessage,
    OutgoingAttachment,
    OutgoingMessage,
    preview_text,
    select_attachment,
)
from .graph_calendar import GraphCalendarMixin
from .graph_contacts import GraphContactsMixin

_log = logging.getLogger(__name__)

_DEFAULT_GRAPH_BASE = "https://graph.microsoft.com"
_DEFAULT_SCOPE = (
    "offline_access https://graph.microsoft.com/User.Read "
    "https://graph.microsoft.com/Mail.ReadWrite "
    "https://graph.microsoft.com/Mail.Send "
    "https://graph.microsoft.com/Calendars.ReadWrite "
    "https://graph.microsoft.com/Contacts.ReadWrite"
)
_APP_SCOPE = "https://graph.microsoft.com/.default"

_WELL_KNOWN_GRAPH = {
    "inbox": "inbox",
    "sent": "sentitems",
    "drafts": "drafts",
    "trash": "deleteditems",
    "junk": "junkemail",
    "archive": "archive",
}
_GRAPH_TO_ALIAS = {v: k for k, v in _WELL_KNOWN_GRAPH.items()}

# Display names Outlook uses for the well-known folders, so ``list_folders``
# can tag them even though Graph hands back opaque ids.
_DISPLAY_ALIASES = {
    "inbox": "inbox",
    "sent items": "sent",
    "drafts": "drafts",
    "deleted items": "trash",
    "junk email": "junk",
    "junk e-mail": "junk",
    "archive": "archive",
}

_MSG_SELECT = (
    "id,subject,from,toRecipients,receivedDateTime,sentDateTime,isRead,"
    "hasAttachments,bodyPreview,importance,conversationId,internetMessageId,flag"
)
_MSG_GET_SELECT = _MSG_SELECT + ",body,ccRecipients,bccRecipients,replyTo"

# Graph folder ids are long opaque tokens (typically 40+ chars, no whitespace/slash).
_GRAPH_ID_RE = re.compile(r"^[A-Za-z0-9+/=_-]{20,}$")

#: Pages of a locally-filtered search to walk before giving up. Bounds the
#: cost of a query whose predicates Graph will not evaluate server-side.
_MAX_SEARCH_PAGES = 5

#: Graph 413s a ``sendMail`` JSON body around 4MB. Refuse earlier, on raw
#: attachment bytes, so the caller gets a useful error instead of a 413.
_GRAPH_MAX_INLINE_ATTACHMENT_BYTES = 3 * 1024 * 1024


def _graph_remedy(status: int, code: Optional[str]) -> str:
    """What the caller can do about a Graph rejection, appended to the raw text.

    Graph's own wording ("InefficientFilter", "ErrorAccessDenied") reads like
    a bug in this server; naming the wall and the way round it is the
    difference between an agent retrying blindly and an agent fixing the call.
    """
    name = (code or "").strip()
    if status == 401:
        return (
            " Re-authenticate: the refresh token was rejected even after a "
            "refresh, so a new Outlook OAuth consent is needed."
        )
    if status == 403:
        return (
            " The token is valid but lacks permission - re-consent with the "
            "Mail.ReadWrite / Mail.Send scopes (a shared or delegated mailbox "
            "also needs an explicit grant)."
        )
    if name == "InefficientFilter":
        return (
            " Graph refuses this $filter next to $orderby; drop the filter and "
            "narrow the result locally."
        )
    if status == 404 or name in ("ErrorItemNotFound", "ResourceNotFound"):
        return (
            " Ids are per-mailbox and change when an item moves - list the "
            "folder again and use the fresh id."
        )
    if status == 413 or name == "RequestEntityTooLarge":
        return " Too large for one request; attach a smaller file."
    return ""


class GraphBackend(GraphCalendarMixin, GraphContactsMixin, MailBackend):
    kind = "graph"

    def __init__(self, account: MailAccount) -> None:
        super().__init__(account.email)
        self._account = account
        self._client: Optional[httpx.Client] = None
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._refresh_token: Optional[str] = account.get("refresh_token") or None
        self._folder_id_cache: Dict[str, str] = {}

    # --- config ---------------------------------------------------------
    def _base_url(self) -> str:
        return (self._account.get("graph_base_url") or _DEFAULT_GRAPH_BASE).rstrip("/")

    def _token_url(self) -> str:
        override = self._account.get("graph_token_url")
        if override:
            return str(override)
        tenant = self._account.get("tenant_id") or "common"
        return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

    def _auth_mode(self) -> str:
        if self._refresh_token:
            return "delegated"
        return "app-only"

    def _resource_prefix(self) -> str:
        if self._auth_mode() == "delegated":
            return "/me"
        return f"/users/{urllib.parse.quote(self.email)}"

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(60.0, connect=60.0),
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # --- auth -----------------------------------------------------------
    def _ensure_token(self) -> str:
        if self._access_token and time.time() < (self._token_expires_at - 60):
            return self._access_token
        self._fetch_token()
        if not self._access_token:
            raise MailBackendError("Graph token refresh returned no access_token")
        return self._access_token

    def _fetch_token(self) -> None:
        client_id = self._account.get("client_id")
        if not client_id:
            raise MailBackendError("Graph mailbox needs client_id")
        secret = self._account.get("client_secret")
        data: Dict[str, str] = {"client_id": str(client_id)}
        if self._refresh_token:
            data["grant_type"] = "refresh_token"
            data["refresh_token"] = str(self._refresh_token)
            data["scope"] = str(self._account.get("graph_scope") or _DEFAULT_SCOPE)
            if secret:
                data["client_secret"] = str(secret)
        elif secret:
            data["grant_type"] = "client_credentials"
            data["client_secret"] = str(secret)
            data["scope"] = _APP_SCOPE
        else:
            raise MailBackendError(
                "Graph mailbox needs either refresh_token (delegated) "
                "or client_secret (app-only)"
            )
        url = self._token_url()
        _log.debug("Graph token POST %s grant=%s", url, data.get("grant_type"))
        try:
            resp = self._get_client().post(url, data=data)
        except httpx.HTTPError as exc:
            raise MailBackendError(f"Graph token request failed: {exc}") from exc
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        if resp.status_code >= 400 or "access_token" not in payload:
            err = payload.get("error") if isinstance(payload, dict) else None
            desc = payload.get("error_description") if isinstance(payload, dict) else None
            raise MailBackendError(
                f"Graph token failed HTTP {resp.status_code}: {err}: {desc}"
                + (
                    " The stored refresh token is dead (revoked, expired or "
                    "used from a different client id) - run the Outlook OAuth "
                    "consent again and update OUTLOOK_REFRESH_TOKEN."
                    if err in ("invalid_grant", "invalid_client")
                    else ""
                )
            )
        self._access_token = payload["access_token"]
        expires_in = int(payload.get("expires_in") or 3600)
        self._token_expires_at = time.time() + expires_in
        new_rt = payload.get("refresh_token")
        if new_rt:
            self._refresh_token = new_rt

    # --- HTTP -----------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Any = None,
        allow_404: bool = False,
        request_headers: Optional[Dict[str, str]] = None,
        _retried_401: bool = False,
        _retries: int = 0,
    ) -> Any:
        token = self._ensure_token()
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            if not path.startswith("/"):
                path = "/" + path
            url = f"{self._base_url()}/v1.0{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if request_headers:
            headers.update(request_headers)
        if json is not None:
            headers["Content-Type"] = "application/json"
        try:
            resp = self._get_client().request(
                method, url, params=params, json=json, headers=headers
            )
        except httpx.HTTPError as exc:
            raise MailBackendError(
                f"Graph request failed: {exc}",
                details={"submission_uncertain": True},
            ) from exc

        # Replaying a POST can duplicate drafts/uploads or submit mail twice.
        retryable_method = method in {"GET", "HEAD", "PATCH", "DELETE"}
        if resp.status_code in (429, 503) and _retries < 3 and retryable_method:
            wait = 1.0
            ra = resp.headers.get("Retry-After")
            if ra:
                try:
                    wait = min(float(ra), 10.0)
                except ValueError:
                    wait = 1.0
            wait = min(max(wait, 0.0), 10.0)
            time.sleep(wait)
            return self._request(
                method,
                path,
                params=params,
                json=json,
                allow_404=allow_404,
                request_headers=request_headers,
                _retried_401=_retried_401,
                _retries=_retries + 1,
            )
        if resp.status_code == 401 and not _retried_401:
            self._access_token = None
            self._fetch_token()
            return self._request(
                method,
                path,
                params=params,
                json=json,
                allow_404=allow_404,
                request_headers=request_headers,
                _retried_401=True,
                _retries=_retries,
            )
        if resp.status_code == 404 and allow_404:
            return None
        try:
            payload = resp.json()
        except Exception:
            payload = None
        if resp.status_code >= 400:
            code = message = None
            if isinstance(payload, dict):
                err = payload.get("error") or {}
                if isinstance(err, dict):
                    code = err.get("code")
                    message = err.get("message")
            raise MailBackendError(
                f"Graph HTTP {resp.status_code} {code}: {message}"
                + _graph_remedy(resp.status_code, code),
                details={"submission_uncertain": resp.status_code >= 500 or resp.status_code == 408},
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return payload

    def _request_raw(self, method: str, path: str, _retried_401: bool = False) -> bytes:
        """Fetch a non-JSON Graph resource (``/$value``: MIME, attachment bytes)."""
        token = self._ensure_token()
        if not path.startswith("http"):
            if not path.startswith("/"):
                path = "/" + path
            path = f"{self._base_url()}/v1.0{path}"
        try:
            resp = self._get_client().request(
                method, path, headers={"Authorization": f"Bearer {token}", "Accept": "*/*"}
            )
        except httpx.HTTPError as exc:
            raise MailBackendError(f"Graph request failed: {exc}") from exc
        if resp.status_code == 401 and not _retried_401:
            self._access_token = None
            self._fetch_token()
            return self._request_raw(method, path, _retried_401=True)
        if resp.status_code >= 400:
            raise MailBackendError(
                f"Graph HTTP {resp.status_code} on {path.rsplit('/', 1)[-1]}: "
                f"{resp.text[:200]}"
            )
        return resp.content

    def _paginate(self, path: str, params: Optional[Dict[str, Any]] = None) -> List[dict]:
        items: List[dict] = []
        data = self._request("GET", path, params=params)
        while data:
            items.extend(data.get("value") or [])
            nxt = data.get("@odata.nextLink")
            if not nxt:
                break
            data = self._request("GET", nxt)
        return items

    # --- folders --------------------------------------------------------
    def _looks_like_graph_id(self, token: str) -> bool:
        return bool(_GRAPH_ID_RE.match(token)) and "/" not in token and " " not in token

    def _resolve_folder(self, folder: str) -> str:
        token = (folder or "inbox").strip()
        if not token:
            token = "inbox"
        cached = self._folder_id_cache.get(token.lower())
        if cached:
            return cached
        well = _WELL_KNOWN_GRAPH.get(token.lower())
        if well:
            self._folder_id_cache[token.lower()] = well
            return well
        if self._looks_like_graph_id(token):
            return token
        fid = self._walk_folder_path(token)
        self._folder_id_cache[token.lower()] = fid
        return fid

    def _walk_folder_path(self, path: str) -> str:
        parts = [p for p in path.split("/") if p]
        if not parts:
            raise MailBackendError("empty folder path")
        available: List[str] = []
        parent_id: Optional[str] = None
        current_id: Optional[str] = None
        for name in parts:
            if parent_id is None:
                rows = self._paginate(
                    f"{self._resource_prefix()}/mailFolders",
                    params={"$top": "100", "$select": "id,displayName"},
                )
            else:
                rows = self._paginate(
                    f"{self._resource_prefix()}/mailFolders/"
                    f"{urllib.parse.quote(parent_id, safe='')}/childFolders",
                    params={"$top": "100", "$select": "id,displayName"},
                )
            match = None
            names = []
            for row in rows:
                dn = row.get("displayName") or ""
                names.append(dn)
                if dn.lower() == name.lower():
                    match = row
            available.extend(names)
            if not match:
                raise MailBackendError(
                    f"folder {path!r} not found; available: "
                    f"{', '.join(sorted(set(available))) or '(none)'}"
                )
            current_id = str(match["id"])
            parent_id = current_id
        if current_id is None:  # pragma: no cover - guarded by parts check
            raise MailBackendError(f"folder {path!r} not found")
        return current_id

    def _folder_well_known(self, display_name: str, fid: str) -> Optional[str]:
        low = (display_name or "").lower()
        alias = _DISPLAY_ALIASES.get(low)
        if alias:
            return alias
        return _GRAPH_TO_ALIAS.get(fid.lower())

    # --- mapping --------------------------------------------------------
    def _addr_from(self, rec: Any) -> str:
        if not isinstance(rec, dict):
            return ""
        ea = rec.get("emailAddress") or rec
        if isinstance(ea, dict):
            return str(ea.get("address") or "")
        return ""

    def _name_from(self, rec: Any) -> str:
        if not isinstance(rec, dict):
            return ""
        ea = rec.get("emailAddress") or rec
        if isinstance(ea, dict):
            return str(ea.get("name") or "")
        return ""

    def _addrs(self, recs: Any) -> List[str]:
        out: List[str] = []
        if not recs:
            return out
        for rec in recs:
            a = self._addr_from(rec)
            if a:
                out.append(a)
        return out

    def _is_flagged(self, item: dict) -> bool:
        flag = item.get("flag") or {}
        if isinstance(flag, dict):
            return str(flag.get("flagStatus") or "").lower() == "flagged"
        return False

    def _message_from_item(
        self,
        item: dict,
        folder: str,
        *,
        include_body: bool = False,
        body_format: str = "markdown",
        attachments: Optional[List[MailAttachmentInfo]] = None,
    ) -> MailMessage:
        frm = item.get("from") or {}
        html = plain = None
        body_val = None
        actual_fmt = None
        if include_body:
            body = item.get("body") or {}
            content = body.get("content") or ""
            ctype = str(body.get("contentType") or "").lower()
            if ctype == "html":
                html = content
            else:
                plain = content
            body_val, actual_fmt = render_body(html, plain, body_format)
        preview = preview_text(item.get("bodyPreview") or "")
        return MailMessage(
            id=str(item.get("id") or ""),
            folder=folder,
            subject=item.get("subject") or "",
            from_address=self._addr_from(frm),
            from_name=self._name_from(frm),
            to=self._addrs(item.get("toRecipients")),
            cc=self._addrs(item.get("ccRecipients")),
            bcc=self._addrs(item.get("bccRecipients")),
            reply_to=self._addrs(item.get("replyTo")),
            received=item.get("receivedDateTime"),
            sent=item.get("sentDateTime"),
            is_read=bool(item.get("isRead")),
            is_flagged=self._is_flagged(item),
            has_attachments=bool(item.get("hasAttachments")),
            importance=item.get("importance"),
            internet_message_id=item.get("internetMessageId"),
            conversation_id=item.get("conversationId"),
            preview=preview,
            body=body_val,
            body_format=actual_fmt,
            attachments=attachments or [],
        )

    def _recipient_payloads(self, addrs: List[str]) -> List[dict]:
        return [{"emailAddress": {"address": a}} for a in addrs if a]

    def _filter_eq(self, field: str, value: str) -> str:
        esc = (value or "").replace("'", "''")
        return f"{field} eq '{esc}'"

    def _filter_contains(self, field: str, value: str) -> str:
        esc = (value or "").replace("'", "''")
        return f"contains({field},'{esc}')"

    @staticmethod
    def _iso(value: str) -> str:
        """Normalise a caller date to a Graph DateTimeOffset literal.

        Graph rejects a bare ``2026-01-01`` in a ``receivedDateTime`` filter, so
        a date-only value is widened to midnight UTC.
        """
        v = (value or "").strip()
        if not v:
            return v
        if "T" not in v:
            return f"{v}T00:00:00Z"
        if v.endswith(("Z", "z")) or re.search(r"[+-]\d{2}:?\d{2}$", v):
            return v
        return f"{v}Z"

    def _client_match(
        self,
        item: dict,
        *,
        from_address: Optional[str],
        subject: Optional[str],
        since: Optional[str],
        before: Optional[str],
        unread_only: bool,
        has_attachments: Optional[bool],
    ) -> bool:
        if from_address:
            got = self._addr_from(item.get("from") or {}).lower()
            if got != from_address.lower():
                return False
        if subject:
            if subject.lower() not in (item.get("subject") or "").lower():
                return False
        recv = item.get("receivedDateTime") or ""
        if since and recv < self._iso(since):
            return False
        if before and not (recv < self._iso(before)):
            return False
        if unread_only and item.get("isRead"):
            return False
        if has_attachments is not None and bool(item.get("hasAttachments")) != has_attachments:
            return False
        return True

    # --- public API -----------------------------------------------------
    def test_connection(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {
            "connected": False,
            "backend": "graph",
            "email": self.email,
            "auth": self._auth_mode(),
        }
        try:
            self._request("GET", f"{self._resource_prefix()}/mailFolders/inbox")
            info["connected"] = True
        except Exception as exc:
            info["error"] = str(exc)
        return info

    def list_folders(self) -> List[MailFolder]:
        prefix = self._resource_prefix()
        params = {
            "$top": "100",
            "$select": "id,displayName,totalItemCount,unreadItemCount",
            "$expand": "childFolders($select=id,displayName,totalItemCount,unreadItemCount)",
        }
        rows = self._paginate(f"{prefix}/mailFolders", params=params)
        out: List[MailFolder] = []

        def emit(row: dict, parent_path: Optional[str] = None) -> None:
            name = row.get("displayName") or ""
            fid = str(row.get("id") or "")
            path = f"{parent_path}/{name}" if parent_path else name
            out.append(
                MailFolder(
                    id=fid,
                    name=name,
                    path=path,
                    well_known=self._folder_well_known(name, fid),
                    total_count=row.get("totalItemCount"),
                    unread_count=row.get("unreadItemCount"),
                )
            )
            for child in row.get("childFolders") or []:
                emit(child, path)

        for row in rows:
            emit(row)
        return out

    def list_messages(
        self,
        folder: str = "inbox",
        limit: int = 25,
        offset: int = 0,
        unread_only: bool = False,
    ) -> List[MailMessage]:
        fid = self._resolve_folder(folder)
        prefix = self._resource_prefix()
        params: Dict[str, Any] = {
            "$top": str(limit),
            "$skip": str(offset),
            "$orderby": "receivedDateTime desc",
            "$select": _MSG_SELECT,
        }
        if unread_only:
            params["$filter"] = "isRead eq false"
        data = self._request(
            "GET",
            f"{prefix}/mailFolders/{urllib.parse.quote(fid, safe='')}/messages",
            params=params,
        )
        items = (data or {}).get("value") or []
        return [self._message_from_item(it, folder) for it in items]

    def get_message(
        self,
        message_id: str,
        folder: str = "inbox",
        body_format: str = "markdown",
    ) -> MailMessage:
        prefix = self._resource_prefix()
        mid = urllib.parse.quote(message_id, safe="")
        item = self._request(
            "GET",
            f"{prefix}/messages/{mid}",
            params={"$select": _MSG_GET_SELECT},
        )
        attachments: List[MailAttachmentInfo] = []
        try:
            attachments = self.list_attachments(message_id, folder)
        except MailBackendError:
            attachments = []
        return self._message_from_item(
            item or {},
            folder,
            include_body=True,
            body_format=body_format,
            attachments=attachments,
        )

    # --- attachments ----------------------------------------------------
    def list_attachments(
        self,
        message_id: str,
        folder: str = "inbox",
        include_inline: bool = True,
    ) -> List[MailAttachmentInfo]:
        prefix = self._resource_prefix()
        mid = urllib.parse.quote(message_id, safe="")
        # contentId lives on microsoft.graph.fileAttachment, not on the base
        # attachment type, so $select must not name it: Graph answers a
        # 400 BadRequest ("Could not find a property named 'contentId'").
        rows = self._paginate(
            f"{prefix}/messages/{mid}/attachments",
            params={"$select": "id,name,contentType,size,isInline"},
        )
        out: List[MailAttachmentInfo] = []
        for a in rows:
            is_inline = bool(a.get("isInline"))
            if is_inline and not include_inline:
                continue
            out.append(
                MailAttachmentInfo(
                    name=a.get("name") or "",
                    content_type=a.get("contentType"),
                    size=a.get("size"),
                    attachment_id=a.get("id"),
                    is_inline=is_inline,
                    content_id=a.get("contentId"),
                )
            )
        return out

    def get_attachment(
        self,
        message_id: str,
        folder: str = "inbox",
        attachment_id: Optional[str] = None,
        attachment_name: Optional[str] = None,
    ) -> MailAttachmentContent:
        chosen = select_attachment(
            self.list_attachments(message_id, folder),
            attachment_id,
            attachment_name,
        )
        prefix = self._resource_prefix()
        mid = urllib.parse.quote(message_id, safe="")
        aid = urllib.parse.quote(str(chosen.attachment_id or ""), safe="")
        payload = self._request("GET", f"{prefix}/messages/{mid}/attachments/{aid}")
        raw = (payload or {}).get("contentBytes")
        if raw:
            content = base64.b64decode(raw)
        else:
            # itemAttachment / referenceAttachment carry no contentBytes;
            # $value hands back the raw bytes (an .eml for item attachments).
            content = self._request_raw(
                "GET", f"{prefix}/messages/{mid}/attachments/{aid}/$value"
            )
        entity = payload or {}
        return MailAttachmentContent(
            name=entity.get("name") or chosen.name,
            content=content,
            content_type=entity.get("contentType") or chosen.content_type,
            attachment_id=chosen.attachment_id,
            is_inline=bool(entity.get("isInline", chosen.is_inline)),
            content_id=entity.get("contentId") or chosen.content_id,
        )

    def _require_draft(self, message_id: str, operation: str) -> None:
        """Graph only lets attachments change on a draft — say so up front."""
        prefix = self._resource_prefix()
        mid = urllib.parse.quote(message_id, safe="")
        item = self._request("GET", f"{prefix}/messages/{mid}", params={"$select": "isDraft"})
        if not bool((item or {}).get("isDraft")):
            raise self._unsupported(
                operation,
                "Microsoft Graph only allows attachment changes on a draft "
                "message. Create a draft (or send a new mail with the file) "
                "instead of modifying delivered/sent mail.",
            )

    def add_attachment(
        self,
        message_id: str,
        name: str,
        content: bytes,
        folder: str = "drafts",
        content_type: Optional[str] = None,
        is_inline: bool = False,
        content_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._require_draft(message_id, "add attachments")
        prefix = self._resource_prefix()
        mid = urllib.parse.quote(message_id, safe="")
        body: Dict[str, Any] = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": name,
            "contentType": content_type or "application/octet-stream",
            "contentBytes": base64.b64encode(content).decode("ascii"),
            "isInline": bool(is_inline),
        }
        if content_id:
            body["contentId"] = content_id
        created = self._request(
            "POST", f"{prefix}/messages/{mid}/attachments", json=body
        )
        result = {
            "added": True,
            "backend": self.kind,
            "id": message_id,
            "folder": folder,
            "attachment_id": (created or {}).get("id"),
            "attachment_name": name,
            "attachment_size": len(content),
            "is_inline": bool(is_inline),
        }
        # The caller needs the stored cid verbatim to write <img src="cid:...">.
        if content_id:
            result["content_id"] = content_id
        return result

    def delete_attachment(
        self,
        message_id: str,
        folder: str = "drafts",
        attachment_id: Optional[str] = None,
        attachment_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        chosen = select_attachment(
            self.list_attachments(message_id, folder),
            attachment_id,
            attachment_name,
        )
        self._require_draft(message_id, "delete attachments")
        prefix = self._resource_prefix()
        mid = urllib.parse.quote(message_id, safe="")
        aid = urllib.parse.quote(str(chosen.attachment_id or ""), safe="")
        self._request("DELETE", f"{prefix}/messages/{mid}/attachments/{aid}")
        return {
            "deleted": True,
            "backend": self.kind,
            "id": message_id,
            "folder": folder,
            "attachment_id": chosen.attachment_id,
            "attachment_name": chosen.name,
        }

    def get_mime(self, message_id: str, folder: str = "inbox") -> bytes:
        prefix = self._resource_prefix()
        mid = urllib.parse.quote(message_id, safe="")
        return self._request_raw("GET", f"{prefix}/messages/{mid}/$value")

    def search_messages(
        self,
        folder: str = "inbox",
        query: Optional[str] = None,
        from_address: Optional[str] = None,
        subject: Optional[str] = None,
        since: Optional[str] = None,
        before: Optional[str] = None,
        unread_only: bool = False,
        has_attachments: Optional[bool] = None,
        limit: int = 25,
    ) -> List[MailMessage]:
        fid = self._resolve_folder(folder)
        prefix = self._resource_prefix()
        params: Dict[str, Any] = {"$select": _MSG_SELECT}
        use_search = bool(query)
        # Two predicates cannot go to the server: $search takes no $filter at
        # all, and "hasAttachments eq true" next to $orderby is refused with
        # 400 InefficientFilter. Both are applied locally, which only keeps
        # its recall if we over-fetch and keep following @odata.nextLink -
        # a single $top=limit page silently returned a fraction of the hits.
        post_filter = not use_search and has_attachments is not None
        local_filter = use_search and any(
            (from_address, subject, since, before, unread_only, has_attachments is not None)
        )
        over_fetch = post_filter or local_filter
        params["$top"] = str(min(limit * 5, 200) if over_fetch else limit)
        if use_search:
            params["$search"] = f'"{query}"'
        else:
            params["$orderby"] = "receivedDateTime desc"
            clauses: List[str] = []
            if from_address:
                clauses.append(self._filter_eq("from/emailAddress/address", from_address))
            if subject:
                clauses.append(self._filter_contains("subject", subject))
            if since:
                clauses.append(f"receivedDateTime ge {self._iso(since)}")
            if before:
                clauses.append(f"receivedDateTime lt {self._iso(before)}")
            if unread_only:
                clauses.append("isRead eq false")
            if clauses:
                params["$filter"] = " and ".join(clauses)
        url = f"{prefix}/mailFolders/{urllib.parse.quote(fid, safe='')}/messages"
        request_params: Optional[Dict[str, Any]] = params
        items: List[dict] = []
        for _page in range(_MAX_SEARCH_PAGES):
            data = self._request("GET", url, params=request_params) or {}
            rows = data.get("value") or []
            if local_filter:
                rows = [
                    it
                    for it in rows
                    if self._client_match(
                        it,
                        from_address=from_address,
                        subject=subject,
                        since=since,
                        before=before,
                        unread_only=unread_only,
                        has_attachments=has_attachments,
                    )
                ]
            elif post_filter:
                rows = [it for it in rows if bool(it.get("hasAttachments")) == has_attachments]
            items.extend(rows)
            next_link = data.get("@odata.nextLink")
            if len(items) >= limit or not next_link:
                break
            url, request_params = next_link, None
        return [self._message_from_item(it, folder) for it in items[:limit]]

    def _draft_path(self, draft_id: str) -> str:
        return f"{self._resource_prefix()}/messages/{urllib.parse.quote(draft_id, safe='')}"

    def _draft_failure(
        self, exc: MailBackendError, draft_id: Optional[str], stage: str,
        *, cleanup: bool = False, folder: str = "drafts",
    ) -> MailBackendError:
        uncertain = stage == "send" and bool(exc.details.get("submission_uncertain"))
        cleanup_result: Dict[str, Any] = {
            "requested": cleanup, "attempted": False, "deleted": False,
        }
        retained: Optional[bool] = bool(draft_id)
        if uncertain or (stage == "create" and exc.details.get("submission_uncertain")):
            retained = None
        if cleanup and draft_id and not uncertain:
            cleanup_result["attempted"] = True
            try:
                self._request("DELETE", self._draft_path(draft_id))
                cleanup_result["deleted"] = True
                retained = False
            except MailBackendError as cleanup_exc:
                cleanup_result["error"] = str(cleanup_exc)
                if cleanup_exc.details.get("submission_uncertain"):
                    retained = None
        elif cleanup:
            cleanup_result["reason"] = (
                "Submission outcome unknown; do not delete potentially submitted mail."
                if uncertain else "No authoritative draft ID was returned."
            )
        if uncertain:
            recovery = (
                "Submission outcome unknown. Check Sent and Drafts before any retry; "
                "retrying blindly may send a duplicate."
            )
        elif cleanup_result["deleted"]:
            recovery = "Draft deleted by requested cleanup."
        elif draft_id:
            recovery = "Inspect the retained draft before continuing."
        elif stage == "create" and retained is False:
            recovery = "Graph rejected creation; no draft was created."
        else:
            recovery = "No draft ID is available. Inspect Drafts and Sent before retrying."
        return MailBackendError(
            f"{exc} {recovery}",
            details={
                "draft_id": draft_id, "folder": folder, "backend": self.kind,
                "stage": stage, "sent": False,
                "accepted_by_mail_server": None if uncertain else False,
                "delivery_confirmed": False, "submission_uncertain": uncertain,
                "retry_safe": False, "draft_retained": retained,
                "cleanup": cleanup_result, "recovery": recovery,
            },
        )

    def _draft_result(self, draft_id: str, folder: str = "drafts") -> Dict[str, Any]:
        return {
            "id": draft_id, "folder": folder, "backend": self.kind,
            "attachments": [a.to_dict() for a in self.list_attachments(draft_id, folder)],
        }

    def _outgoing_payload(self, message: OutgoingMessage) -> Dict[str, Any]:
        html, _actual = compose_body(message.body, message.body_format)
        payload: Dict[str, Any] = {
            "subject": message.subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": self._recipient_payloads(message.to),
        }
        if message.cc:
            payload["ccRecipients"] = self._recipient_payloads(message.cc)
        if message.bcc:
            payload["bccRecipients"] = self._recipient_payloads(message.bcc)
        if message.tracking_id:
            payload["internetMessageHeaders"] = [
                {"name": OUTBOUND_TRACKING_HEADER, "value": message.tracking_id},
            ]
        return payload

    def create_draft(self, message: OutgoingMessage) -> Dict[str, Any]:
        return self._create_draft(message)

    def create_reply_draft(
        self, message_id: str, message: OutgoingMessage, folder: str = "inbox",
    ) -> Dict[str, Any]:
        action = "createReplyAll" if message.reply_all else "createReply"
        return self._create_draft(message, message_id, action)

    def create_forward_draft(
        self, message_id: str, message: OutgoingMessage, folder: str = "inbox",
    ) -> Dict[str, Any]:
        return self._create_draft(message, message_id, "createForward")

    def _create_draft(
        self, message: OutgoingMessage, source_id: Optional[str] = None,
        action: Optional[str] = None,
    ) -> Dict[str, Any]:
        # Validate and encode all new files before creating anything remotely.
        files = self._file_attachment_payloads(message.attachments)
        prelude = compose_body(message.body, message.body_format)[0] if action else ""
        if action:
            inner: Dict[str, Any] = {}
            for field, recipients in (("toRecipients", message.to),
                                      ("ccRecipients", message.cc),
                                      ("bccRecipients", message.bcc)):
                if recipients or (field == "toRecipients" and action == "createForward"):
                    inner[field] = self._recipient_payloads(recipients)
            path = f"{self._draft_path(source_id or '')}/{action}"
            payload = {"message": inner}
        else:
            path = f"{self._resource_prefix()}/messages"
            payload = self._outgoing_payload(message)
        try:
            draft = self._request("POST", path, json=payload)
        except MailBackendError as exc:
            raise self._draft_failure(
                exc, None, "create", cleanup=message.cleanup_on_failure,
            ) from exc
        draft_id = (draft or {}).get("id")
        if not isinstance(draft_id, str) or not draft_id:
            raise self._draft_failure(
                MailBackendError(
                    "Graph creation returned no draft id.",
                    details={"submission_uncertain": True},
                ),
                None, "create", cleanup=message.cleanup_on_failure,
            )
        draft_path = self._draft_path(draft_id)
        stage = "patch"
        try:
            if action and message.body:
                original = draft.get("body")
                if not isinstance(original, dict) or "content" not in original:
                    stored = self._request("GET", draft_path, params={"$select": "body"})
                    original = (stored or {}).get("body")
                if not isinstance(original, dict) or "content" not in original:
                    raise MailBackendError(
                        "Graph draft returned no body; refusing to discard original history"
                    )
                content = original["content"] or ""
                if str(original.get("contentType", "")).lower() != "html":
                    content, _actual = compose_body(content, "text")
                body_tag = re.search(r"<body\b[^>]*>", content, re.IGNORECASE)
                offset = body_tag.end() if body_tag else 0
                content = content[:offset] + prelude + content[offset:]
                self._request("PATCH", draft_path, json={
                    "body": {"contentType": "HTML", "content": content},
                })
            stage = "attachments"
            for item in files:
                self._request("POST", f"{draft_path}/attachments", json=item)
            stage = "metadata"
            result = self._draft_result(draft_id)
        except MailBackendError as exc:
            raise self._draft_failure(
                exc, draft_id, stage, cleanup=message.cleanup_on_failure,
            ) from exc
        result["tracking_id"] = None if action else message.tracking_id
        return result

    def update_draft(
        self, draft_id: str, subject: Optional[str] = None,
        body: Optional[str] = None, body_format: str = "markdown",
        folder: str = "drafts",
    ) -> Dict[str, Any]:
        if not isinstance(draft_id, str) or not draft_id.strip():
            raise MailBackendError("draft_id must be a nonblank string")
        path = self._draft_path(draft_id)
        item = self._request("GET", path, params={"$select": "isDraft"})
        if not item or item.get("isDraft") is not True:
            raise MailBackendError("Only a saved draft can be updated")
        patch: Dict[str, Any] = {}
        if subject is not None:
            patch["subject"] = subject
        if body is not None:
            html, _actual = compose_body(body, body_format)
            patch["body"] = {"contentType": "HTML", "content": html if body else ""}
        stage = "patch"
        try:
            if patch:
                self._request("PATCH", path, json=patch)
            stage = "metadata"
            return self._draft_result(draft_id, folder)
        except MailBackendError as exc:
            raise self._draft_failure(exc, draft_id, stage, folder=folder) from exc

    def _submit_created_draft(self, draft_id: str, message: OutgoingMessage) -> None:
        try:
            self._request("POST", f"{self._draft_path(draft_id)}/send")
        except MailBackendError as exc:
            raise self._draft_failure(
                exc, draft_id, "send", cleanup=message.cleanup_on_failure,
            ) from exc

    def send_draft(
        self, draft_id: str, folder: str = "drafts", dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Submit the stored message without rewriting its content or attachments."""
        if not isinstance(draft_id, str) or not draft_id.strip():
            raise MailBackendError("draft_id must be a nonblank string")
        prefix = self._resource_prefix()
        mid = urllib.parse.quote(draft_id, safe="")
        path = f"{prefix}/messages/{mid}"
        item = self._request(
            "GET",
            path,
            params={
                "$select": (
                    "isDraft,subject,toRecipients,ccRecipients,bccRecipients,bodyPreview"
                ),
            },
        )
        if not item or item.get("isDraft") is not True:
            raise MailBackendError("Only a saved draft can be sent with draft_id")
        result: Dict[str, Any] = {
            "draft_id": draft_id,
            "backend": self.kind,
            "sent": False,
            "accepted_by_mail_server": False,
            "delivery_confirmed": False,
        }
        if dry_run:
            result.update({
                "dry_run": True,
                "would_send_to": self._addrs(item.get("toRecipients")),
                "would_cc": self._addrs(item.get("ccRecipients")),
                "would_bcc": self._addrs(item.get("bccRecipients")),
                "subject": item.get("subject") or "",
                "body_preview": item.get("bodyPreview") or "",
            })
            return result
        try:
            self._request("POST", f"{path}/send")
        except MailBackendError as exc:
            raise self._draft_failure(exc, draft_id, "send", folder=folder) from exc
        result.update({"sent": True, "accepted_by_mail_server": True})
        return result

    def send_message(self, message: OutgoingMessage) -> Dict[str, Any]:
        """Submit new mail or a native reply, retaining recoverable drafts on failure."""
        draft_id = None
        if message.attachments or (message.reply_to_message_id and message.body):
            draft = (
                self.create_reply_draft(message.reply_to_message_id, message)
                if message.reply_to_message_id else self.create_draft(message)
            )
            draft_id = draft["id"]
            self._submit_created_draft(draft_id, message)
        else:
            if message.reply_to_message_id:
                action = "replyAll" if message.reply_all else "reply"
                path = f"{self._draft_path(message.reply_to_message_id)}/{action}"
                inner: Dict[str, Any] = {}
                for field, recipients in (("toRecipients", message.to),
                                          ("ccRecipients", message.cc),
                                          ("bccRecipients", message.bcc)):
                    if recipients:
                        inner[field] = self._recipient_payloads(recipients)
                payload = {"message": inner}
            else:
                path = f"{self._resource_prefix()}/sendMail"
                payload = {"message": self._outgoing_payload(message), "saveToSentItems": True}
            try:
                self._request("POST", path, json=payload)
            except MailBackendError as exc:
                raise self._draft_failure(exc, None, "send", cleanup=message.cleanup_on_failure) from exc
        result = {
            "sent": True, "backend": self.kind, "to": list(message.to),
            "subject": message.subject, "replied_to": message.reply_to_message_id,
            "tracking_id": None if message.reply_to_message_id else message.tracking_id,
            "accepted_by_mail_server": True, "delivery_confirmed": False,
        }
        if draft_id:
            result["draft_id"] = draft_id
        return result

    def forward_message(
        self, message_id: str, message: OutgoingMessage, folder: str = "inbox",
    ) -> Dict[str, Any]:
        """Forward natively, preserving history and inherited attachments."""
        draft_id = None
        if message.body or message.attachments:
            draft = self.create_forward_draft(message_id, message, folder)
            draft_id = draft["id"]
            self._submit_created_draft(draft_id, message)
        else:
            inner: Dict[str, Any] = {"toRecipients": self._recipient_payloads(message.to)}
            if message.cc:
                inner["ccRecipients"] = self._recipient_payloads(message.cc)
            if message.bcc:
                inner["bccRecipients"] = self._recipient_payloads(message.bcc)
            try:
                self._request(
                    "POST", f"{self._draft_path(message_id)}/forward", json={"message": inner},
                )
            except MailBackendError as exc:
                raise self._draft_failure(exc, None, "send", cleanup=message.cleanup_on_failure) from exc
        result = {
            "sent": True, "backend": self.kind, "to": list(message.to),
            "forwarded_from": message_id, "tracking_id": None,
            "accepted_by_mail_server": True, "delivery_confirmed": False,
        }
        if draft_id:
            result["draft_id"] = draft_id
        return result

    def _file_attachment_payloads(
        self, attachments: List[OutgoingAttachment]
    ) -> List[Dict[str, Any]]:
        """``#microsoft.graph.fileAttachment`` rows, or a clear oversize error.

        This implementation caps newly supplied files for send/reply/forward
        at 3 MiB total raw bytes to leave room for base64 encoding. Larger
        content needs an externally hosted download link in the message body.
        """
        if not attachments:
            return []
        total = sum(len(att.content) for att in attachments)
        if total > _GRAPH_MAX_INLINE_ATTACHMENT_BYTES:
            raise MailBackendError(
                f"attachments total {total} bytes, over this implementation's "
                f"{_GRAPH_MAX_INLINE_ATTACHMENT_BYTES}-byte (3 MiB) limit for "
                "new files in Graph send/reply/forward. Put an externally hosted "
                "download link in the message body instead."
            )
        payloads: List[Dict[str, Any]] = []
        for att in attachments:
            item: Dict[str, Any] = {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": att.name,
                "contentType": att.content_type or "application/octet-stream",
                "contentBytes": base64.b64encode(att.content).decode("ascii"),
                "isInline": bool(att.is_inline),
            }
            if att.content_id:
                item["contentId"] = att.content_id
            payloads.append(item)
        return payloads

    def move_message(
        self, message_id: str, destination: str, folder: str = "inbox",
    ) -> Dict[str, Any]:
        dest = self._resolve_folder(destination)
        prefix = self._resource_prefix()
        data = self._request(
            "POST",
            f"{prefix}/messages/{urllib.parse.quote(message_id, safe='')}/move",
            json={"destinationId": dest},
        )
        new_id = (data or {}).get("id")
        if not new_id:
            raise MailBackendError(
                "Graph moved the message but returned no destination ID; inspect the destination.",
                details={"id": None, "previous_id": message_id, "folder": destination, "moved": True},
            )
        return {
            "moved": True,
            "backend": self.kind,
            "id": new_id,
            "previous_id": message_id,
            "folder": destination,
            "source": folder,
            "destination": destination,
        }

    def delete_message(
        self, message_id: str, folder: str = "inbox", hard_delete: bool = False,
    ) -> Dict[str, Any]:
        prefix = self._resource_prefix()
        mid = urllib.parse.quote(message_id, safe="")
        trash_id = self._resolve_folder("trash")
        current = None
        try:
            current = self._resolve_folder(folder)
        except MailBackendError:
            current = None
        already_trash = (
            (current and current.lower() == trash_id.lower())
            or folder.lower() in ("trash", "deleteditems")
        )
        did_hard = hard_delete or already_trash
        new_id = message_id
        if did_hard:
            self._request("DELETE", f"{prefix}/messages/{mid}")
        else:
            try:
                data = self._request(
                    "POST",
                    f"{prefix}/messages/{mid}/move",
                    json={"destinationId": trash_id},
                )
                new_id = (data or {}).get("id")
                if not new_id:
                    raise MailBackendError(
                        "Graph moved the message to Trash but returned no destination ID.",
                        details={"id": None, "previous_id": message_id, "folder": "trash", "moved": True},
                    )
            except MailBackendError as exc:
                # already in deleted items -> hard delete
                if "ErrorItemNotFound" in str(exc) or "already" in str(exc).lower():
                    self._request("DELETE", f"{prefix}/messages/{mid}")
                    did_hard = True
                else:
                    raise
        return {
            "deleted": True,
            "hard_delete": did_hard,
            "backend": self.kind,
            "id": new_id,
            "previous_id": message_id,
            "folder": folder if did_hard else "trash",
        }

    def mark_message(
        self,
        message_id: str,
        folder: str = "inbox",
        is_read: Optional[bool] = None,
        is_flagged: Optional[bool] = None,
    ) -> Dict[str, Any]:
        if is_read is None and is_flagged is None:
            return {"changed": False, "backend": self.kind, "id": message_id}
        body: Dict[str, Any] = {}
        if is_read is not None:
            body["isRead"] = bool(is_read)
        if is_flagged is not None:
            body["flag"] = {"flagStatus": "flagged" if is_flagged else "notFlagged"}
        prefix = self._resource_prefix()
        self._request(
            "PATCH",
            f"{prefix}/messages/{urllib.parse.quote(message_id, safe='')}",
            json=body,
        )
        out: Dict[str, Any] = {
            "changed": True,
            "backend": self.kind,
            "id": message_id,
            "folder": folder,
        }
        if is_read is not None:
            out["is_read"] = bool(is_read)
        if is_flagged is not None:
            out["is_flagged"] = bool(is_flagged)
        return out
