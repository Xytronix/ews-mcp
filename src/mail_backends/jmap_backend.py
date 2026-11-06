"""JMAP mailbox backend (RFC 8620 core, RFC 8621 mail and submission).

Serves Fastmail, Stalwart and any other JMAP server over one HTTPS API with
``httpx``; CalDAV/CardDAV come from :class:`~.dav.DAVMixin` when configured.

Notes on the protocol choices:

* Email ids are account-wide and survive moves, so unlike IMAP UIDs they are
  not scoped to a folder. ``folder`` arguments are informational for reads and
  mutations; only listing and searching use them.
* Emails are immutable apart from their keywords and mailboxes. Changing a
  draft (subject, body, attachments) creates a replacement and then destroys
  the original, so the result carries a new ``id`` plus ``previous_id``.
* Sending creates the message in Drafts with ``$draft`` and submits it with
  ``EmailSubmission/set`` in the same request; ``onSuccessUpdateEmail`` files
  it in Sent and drops ``$draft``. The server strips Bcc on delivery
  (RFC 8621 section 7.5), so the Sent copy keeps the blind-copy list privately.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from html import escape
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, urljoin

import httpx

from ..body_format import compose_body, html_to_text, render_body
from ..utils import outbound_tracking_header
from .accounts import MailAccount
from .base import (
    ALL_FOLDERS_EXCLUDED,
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
from .dav import DAVMixin
from .imap_backend import _NAME_HINTS, _decode_header_value

_CORE = "urn:ietf:params:jmap:core"
_MAIL = "urn:ietf:params:jmap:mail"
_SUBMISSION = "urn:ietf:params:jmap:submission"

# Mailbox role (RFC 8621 section 2) -> our well-known alias. "all" is the
# Gmail-style everything mailbox, which stands in for a missing archive.
_ROLE_ALIASES = {
    "inbox": "inbox",
    "sent": "sent",
    "drafts": "drafts",
    "trash": "trash",
    "junk": "junk",
    "archive": "archive",
    "all": "archive",
}

_SUMMARY_PROPERTIES = [
    "id", "threadId", "mailboxIds", "keywords", "size", "receivedAt", "sentAt",
    "messageId", "from", "to", "cc", "replyTo", "subject", "hasAttachment",
    "preview", "header:Importance:asText", "header:X-Priority:asText",
]
_PART_PROPERTIES = ["textBody", "htmlBody", "attachments"]
_BODY_PROPERTIES = _PART_PROPERTIES + ["bodyValues"]
_DETAIL_PROPERTIES = _SUMMARY_PROPERTIES + ["bcc"] + _BODY_PROPERTIES
# Header fields the reply attribution and forward banner quote. The raw form
# is the only one RFC 8621 allows for all of them (From has no text form).
_QUOTED_HEADERS = {name: f"header:{name}" for name in ("From", "Date", "Subject", "To", "Cc")}
_SOURCE_PROPERTIES = [
    "from", "to", "cc", "replyTo", "subject", "messageId", "inReplyTo",
    "references", *_QUOTED_HEADERS.values(), *_BODY_PROPERTIES,
]
_DRAFT_PROPERTIES = ["keywords", "mailboxIds", "headers", *_BODY_PROPERTIES]

_TEXT_TYPES = ("text/plain", "text/html")


def _ref(call_id: str, name: str, path: str) -> Dict[str, str]:
    """A result reference (RFC 8620 section 3.7) to an earlier call's output."""
    return {"resultOf": call_id, "name": name, "path": path}


def _addresses(values: Any) -> List[str]:
    return [str(item["email"]) for item in values or () if isinstance(item, dict) and item.get("email")]


def _recipients(addresses: Sequence[str]) -> List[Dict[str, str]]:
    return [{"email": address} for address in addresses]


def _unique(addresses: Sequence[str]) -> List[str]:
    out: List[str] = []
    seen: set = set()
    for address in addresses:
        if address and address.lower() not in seen:
            out.append(address)
            seen.add(address.lower())
    return out


def _utc_date(value: str) -> str:
    """An ISO date/datetime as a JMAP UTCDate; a bare date means midnight UTC."""
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MailBackendError(f"could not parse date {value!r}; use YYYY-MM-DD") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _set_error(method: str, action: str, error: Any) -> MailBackendError:
    """A /set SetError as a MailBackendError carrying the server's description."""
    if not isinstance(error, dict):
        return MailBackendError(f"JMAP {method} did not {action} and gave no reason")
    kind = str(error.get("type") or "unknown")
    text = f"JMAP {method} could not {action}: {kind}"
    if error.get("description"):
        text += f": {error['description']}"
    if error.get("properties"):
        text += f" (properties: {', '.join(str(p) for p in error['properties'])})"
    return MailBackendError(text, details={"jmap_error": kind})


def _created(method: str, result: Dict[str, Any], key: str, action: str) -> Dict[str, Any]:
    created = (result.get("created") or {}).get(key)
    if isinstance(created, dict) and created.get("id"):
        return created
    raise _set_error(method, action, (result.get("notCreated") or {}).get(key))


def _updated(method: str, result: Dict[str, Any], ident: str, action: str) -> None:
    if ident in (result.get("updated") or {}):
        return
    raise _set_error(method, action, (result.get("notUpdated") or {}).get(ident))


def _destroyed(method: str, result: Dict[str, Any], ident: str, action: str) -> None:
    if ident in (result.get("destroyed") or ()):
        return
    raise _set_error(method, action, (result.get("notDestroyed") or {}).get(ident))


def _header_text(item: Dict[str, Any], name: str) -> str:
    """A quoted header's raw value as display text: unfolded and RFC 2047 decoded."""
    raw = item.get(_QUOTED_HEADERS[name]) or ""
    return _decode_header_value(re.sub(r"\r?\n[ \t]+", " ", str(raw)))


def _joined_text(item: Dict[str, Any], parts_key: str, content_type: str) -> Optional[str]:
    """Decoded text of the ``content_type`` parts in ``parts_key``; None if there are none."""
    values = item.get("bodyValues") or {}
    texts = [
        str((values.get(part.get("partId")) or {}).get("value") or "")
        for part in item.get(parts_key) or ()
        if str(part.get("type") or "").lower() == content_type
    ]
    return "\n".join(texts) if texts else None


def _file_parts(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every non-body part: the attachments plus inline media JMAP lists as body."""
    parts: List[Dict[str, Any]] = []
    seen: set = set()
    candidates = list(item.get("attachments") or ())
    candidates += [
        part for key in ("textBody", "htmlBody") for part in item.get(key) or ()
        if str(part.get("type") or "").lower() not in _TEXT_TYPES
    ]
    for part in candidates:
        ident = part.get("partId") or part.get("blobId")
        if ident and ident not in seen:
            seen.add(ident)
            parts.append(part)
    return parts


def _part_reference(part: Dict[str, Any]) -> Dict[str, Any]:
    """An existing part re-used by blob id in a new Email."""
    ref: Dict[str, Any] = {
        "blobId": part["blobId"],
        "type": part.get("type") or "application/octet-stream",
    }
    for key in ("name", "disposition", "cid", "charset", "language", "location"):
        if part.get(key):
            ref[key] = part[key]
    return ref


def _attachment_info(part: Dict[str, Any]) -> MailAttachmentInfo:
    content_type = str(part.get("type") or "application/octet-stream").lower()
    return MailAttachmentInfo(
        name=str(part.get("name") or f"part.{content_type.split('/')[-1]}"),
        content_type=content_type,
        size=part.get("size"),
        attachment_id=str(part.get("partId") or part.get("blobId") or ""),
        is_inline=str(part.get("disposition") or "").lower() == "inline",
        content_id=str(part.get("cid") or "").strip("<>") or None,
    )


class JMAPBackend(DAVMixin, MailBackend):
    """Mail operations over JMAP plus optional CalDAV/CardDAV."""

    kind = "jmap"
    # JMAP keywords carry $flagged only, so there is no Complete state.
    flag_statuses = ("NotFlagged", "Flagged")
    supports_all_folder_search = True
    search_filters = ("to_address", "body", "is_read")

    def __init__(self, account: MailAccount) -> None:
        super().__init__(account.email)
        self.account = account
        self._lock = threading.RLock()
        self._http: Optional[httpx.Client] = None
        self._session: Optional[Dict[str, Any]] = None
        self._folders: Optional[List[MailFolder]] = None
        self._identities: Optional[List[Dict[str, Any]]] = None

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------
    @property
    def _username(self) -> str:
        return str(self.account.get("jmap_username") or self.email)

    @property
    def _dav_username(self) -> str:
        return self._username

    def _http_client(self) -> httpx.Client:
        with self._lock:
            if self._http is None:
                token = self.account.get("api_token")
                auth: Dict[str, Any] = {}
                if token:
                    auth["headers"] = {"Authorization": f"Bearer {token}"}
                elif self.account.password:
                    auth["auth"] = (self._username, self.account.password)
                else:
                    raise MailBackendError(f"{self.email}: no JMAP api_token or password configured")
                self._http = httpx.Client(
                    timeout=httpx.Timeout(60.0, connect=60.0), follow_redirects=True, **auth,
                )
            return self._http

    def close(self) -> None:
        with self._lock:
            for name in ("_dav_http", "_http"):
                client = getattr(self, name, None)
                if client is not None:
                    try:
                        client.close()
                    finally:
                        setattr(self, name, None)

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._http_client().request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise MailBackendError(
                f"JMAP request to {url} failed for {self.email}: {exc}",
                details={"submission_uncertain": True},
            ) from exc
        if response.status_code in (401, 403):
            raise MailBackendError(
                f"JMAP authentication failed for {self.email} (HTTP {response.status_code}). "
                "Check api_token, or jmap_username and password; Fastmail needs an API "
                "token with mail (and submission, to send) access."
            )
        if response.status_code >= 400:
            detail = response.text[:300]
            try:
                problem = response.json()
            except ValueError:
                problem = None
            if isinstance(problem, dict):
                parts = [str(problem.get(k)) for k in ("type", "limit", "detail", "title") if problem.get(k)]
                detail = ": ".join(parts) or detail
            raise MailBackendError(
                f"JMAP HTTP {response.status_code} from {url}: {detail}",
                details={"submission_uncertain": response.status_code >= 500},
            )
        return response

    def _session_data(self) -> Dict[str, Any]:
        with self._lock:
            if self._session is not None:
                return self._session
            url = str(self.account.get("session_url") or "")
            if not url:
                raise MailBackendError(f"{self.email}: no JMAP session_url configured")
            response = self._send("GET", url, headers={"Accept": "application/json"})
            try:
                session = response.json()
            except ValueError:
                session = None
            if not isinstance(session, dict) or not session.get("apiUrl"):
                raise MailBackendError(f"{url} did not return a JMAP session resource for {self.email}")
            if not (session.get("primaryAccounts") or {}).get(_MAIL):
                raise MailBackendError(
                    f"the JMAP session at {url} offers no mail account ({_MAIL}) for {self.email}"
                )
            # Relative URLs resolve against where the session really came from.
            base = str(response.url)
            for key in ("apiUrl", "downloadUrl", "uploadUrl"):
                if session.get(key):
                    session[key] = urljoin(base, str(session[key]))
            self._session = session
            return session

    @property
    def _account_id(self) -> str:
        return str(self._session_data()["primaryAccounts"][_MAIL])

    def _limit(self, name: str, default: int) -> int:
        core = (self._session_data().get("capabilities") or {}).get(_CORE) or {}
        try:
            return max(1, int(core.get(name) or default))
        except (TypeError, ValueError):
            return default

    def _api(self, calls: List[List[Any]]) -> List[List[Any]]:
        """POST one request of method calls and return its method responses."""
        session = self._session_data()
        max_calls = self._limit("maxCallsInRequest", 16)
        if len(calls) > max_calls:
            raise MailBackendError(
                f"{self.email}: this operation needs {len(calls)} JMAP method calls in one "
                f"request, but the server allows {max_calls} (maxCallsInRequest)"
            )
        capabilities = session.get("capabilities") or {}
        using = [_CORE, _MAIL] + ([_SUBMISSION] if _SUBMISSION in capabilities else [])
        response = self._send(
            "POST", str(session["apiUrl"]), json={"using": using, "methodCalls": calls},
        )
        try:
            payload = response.json()
        except ValueError:
            payload = None
        responses = payload.get("methodResponses") if isinstance(payload, dict) else None
        if not isinstance(responses, list):
            raise MailBackendError(f"JMAP API for {self.email} returned no methodResponses")
        state = payload.get("sessionState")
        if state and session.get("state") and state != session["state"]:
            with self._lock:
                self._session = None  # the session changed: fetch it again next time
        return responses

    @staticmethod
    def _result(responses: List[List[Any]], call_id: str, name: str) -> Dict[str, Any]:
        """The ``name`` response to ``call_id``; a method-level error raises."""
        for entry in responses:
            if not isinstance(entry, list) or len(entry) < 3 or entry[2] != call_id:
                continue
            response_name, args = entry[0], entry[1] if isinstance(entry[1], dict) else {}
            if response_name == "error":
                kind = str(args.get("type") or "serverFail")
                text = f"JMAP {name} failed: {kind}"
                if args.get("description"):
                    text += f": {args['description']}"
                raise MailBackendError(text, details={"jmap_error": kind})
            if response_name == name:
                return args
        raise MailBackendError(f"JMAP server sent no {name} response")

    def _call(self, method: str, args: Dict[str, Any]) -> Dict[str, Any]:
        call = [method, {"accountId": self._account_id, **args}, "0"]
        return self._result(self._api([call]), "0", method)

    def _upload(self, content: bytes, content_type: str) -> str:
        core = (self._session_data().get("capabilities") or {}).get(_CORE) or {}
        max_size = core.get("maxSizeUpload")
        if isinstance(max_size, int) and len(content) > max_size:
            raise MailBackendError(
                f"file of {len(content)} bytes exceeds this JMAP server's upload limit "
                f"of {max_size} bytes (maxSizeUpload)"
            )
        url = str(self._session_data().get("uploadUrl") or "")
        if not url:
            raise MailBackendError(f"the JMAP session for {self.email} has no uploadUrl")
        url = url.replace("{accountId}", quote(self._account_id, safe=""))
        response = self._send("POST", url, content=content, headers={"Content-Type": content_type})
        try:
            blob_id = response.json().get("blobId")
        except (ValueError, AttributeError):
            blob_id = None
        if not blob_id:
            raise MailBackendError(f"JMAP upload for {self.email} returned no blobId")
        return str(blob_id)

    def _download(self, blob_id: str, name: str, content_type: str) -> bytes:
        url = str(self._session_data().get("downloadUrl") or "")
        if not url:
            raise MailBackendError(f"the JMAP session for {self.email} has no downloadUrl")
        for key, value in (
            ("accountId", self._account_id), ("blobId", blob_id),
            ("name", name or "download"), ("type", content_type or "application/octet-stream"),
        ):
            url = url.replace("{" + key + "}", quote(value, safe=""))
        return self._send("GET", url).content

    def _query_get(
        self,
        type_name: str,
        query: Dict[str, Any],
        properties: Sequence[str],
        position: int = 0,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """``Foo/query`` plus ``Foo/get`` of its ids, one request per page.

        Each page asks for at most ``maxObjectsInGet`` ids so the back-referenced
        get stays inside the server's limit; a server that clamps the query
        limit lower is paged through until ``limit`` (None: all) ids are read.
        """
        account_id = self._account_id
        page_size = self._limit("maxObjectsInGet", 256)
        rows: List[Dict[str, Any]] = []
        consumed = 0
        while limit is None or consumed < limit:
            want = page_size if limit is None else min(limit - consumed, page_size)
            responses = self._api([
                [f"{type_name}/query", {
                    "accountId": account_id, **query,
                    "position": position + consumed, "limit": want,
                }, "q"],
                [f"{type_name}/get", {
                    "accountId": account_id,
                    "#ids": _ref("q", f"{type_name}/query", "/ids"),
                    "properties": list(properties),
                }, "g"],
            ])
            result = self._result(responses, "q", f"{type_name}/query")
            ids = [str(ident) for ident in result.get("ids") or ()]
            found = {
                str(row.get("id")): row
                for row in self._result(responses, "g", f"{type_name}/get").get("list") or ()
            }
            # An id the get no longer finds was deleted in between; skip it.
            rows.extend(found[ident] for ident in ids if ident in found)
            consumed += len(ids)
            try:
                served = int(result.get("limit") or want)
            except (TypeError, ValueError):
                served = want
            if not ids or len(ids) < min(want, served):
                break
        return rows

    # ------------------------------------------------------------------
    # mailboxes
    # ------------------------------------------------------------------
    def _load_folders(self, refresh: bool = False) -> List[MailFolder]:
        with self._lock:
            if self._folders is not None and not refresh:
                return self._folders
            properties = ["id", "name", "parentId", "role", "totalEmails", "unreadEmails"]
            try:
                rows = self._call("Mailbox/get", {"ids": None, "properties": properties}).get("list") or []
            except MailBackendError as exc:
                # ids=null is refused above maxObjectsInGet mailboxes.
                if exc.details.get("jmap_error") != "requestTooLarge":
                    raise
                rows = self._query_get("Mailbox", {}, properties)
            by_id = {str(row.get("id")): row for row in rows if row.get("id")}

            def path_of(row: Dict[str, Any]) -> str:
                names: List[str] = []
                seen: set = set()
                current: Optional[Dict[str, Any]] = row
                while current is not None and current.get("id") not in seen:
                    seen.add(current.get("id"))
                    names.append(str(current.get("name") or ""))
                    current = by_id.get(str(current.get("parentId"))) if current.get("parentId") else None
                return "/".join(reversed(names))

            folders = sorted(
                (
                    MailFolder(
                        id=ident,
                        name=str(row.get("name") or ""),
                        path=path_of(row),
                        total_count=row.get("totalEmails"),
                        unread_count=row.get("unreadEmails"),
                    )
                    for ident, row in by_id.items()
                ),
                key=lambda folder: folder.path.lower(),
            )
            taken: set = set()
            # Real roles first, then "all" for a missing archive, then names.
            for wanted_all in (False, True):
                for folder in folders:
                    role = str(by_id[folder.id].get("role") or "").lower()
                    alias = _ROLE_ALIASES.get(role)
                    if folder.well_known or not alias or (role == "all") != wanted_all or alias in taken:
                        continue
                    folder.well_known = alias
                    taken.add(alias)
            for folder in folders:
                if folder.well_known or by_id[folder.id].get("role"):
                    continue
                for alias, hints in _NAME_HINTS.items():
                    if alias not in taken and (folder.name.lower() in hints or folder.path.lower() in hints):
                        folder.well_known = alias
                        taken.add(alias)
                        break
            self._folders = folders
            return folders

    @staticmethod
    def _match_folder(folders: Sequence[MailFolder], token: str) -> Optional[MailFolder]:
        lowered = token.lower()
        if lowered in _NAME_HINTS:
            return next((f for f in folders if f.well_known == lowered), None)
        for matches in (
            lambda f: f.path == token or f.id == token,
            lambda f: f.path.lower() == lowered or f.name.lower() == lowered,
        ):
            found = next((f for f in folders if matches(f)), None)
            if found is not None:
                return found
        return None

    def _folder(self, token: Optional[str]) -> MailFolder:
        """Resolve a well-known alias, path, name or mailbox id; refresh once on a miss."""
        token = (token or "inbox").strip() or "inbox"
        for refresh in (False, True):
            folders = self._load_folders(refresh=refresh)
            found = self._match_folder(folders, token)
            if found is not None:
                return found
        available = ", ".join(sorted(f.path for f in folders)) or "<none>"
        if token.lower() in _NAME_HINTS:
            raise MailBackendError(
                f"{self.email}: no '{token.lower()}' folder on this JMAP server. "
                f"Available folders: {available}"
            )
        raise MailBackendError(f"{self.email}: folder {token!r} not found. Available folders: {available}")

    def _optional_folder(self, alias: str) -> Optional[MailFolder]:
        return self._match_folder(self._load_folders(), alias)

    def _folder_label(self, item: Dict[str, Any], requested: Optional[str]) -> str:
        """``requested`` if the message is in that folder, else the path of one it is in."""
        mailbox_ids = [ident for ident, on in (item.get("mailboxIds") or {}).items() if on]
        folders = self._load_folders()
        if requested:
            wanted = self._match_folder(folders, requested)
            if wanted is not None and wanted.id in mailbox_ids:
                return requested
        paths = sorted(f.path for f in folders if f.id in mailbox_ids)
        return paths[0] if paths else (requested or "")

    def list_folders(self) -> List[MailFolder]:
        return list(self._load_folders(refresh=True))

    # ------------------------------------------------------------------
    # messages
    # ------------------------------------------------------------------
    def _message(
        self, item: Dict[str, Any], folder: str, body_format: Optional[str] = None,
    ) -> MailMessage:
        keywords = item.get("keywords") or {}
        sender = next(iter(item.get("from") or ()), None) or {}
        flagged = bool(keywords.get("$flagged"))
        message_ids = item.get("messageId") or []
        importance = str(
            item.get("header:Importance:asText") or item.get("header:X-Priority:asText") or ""
        ).strip()
        message = MailMessage(
            id=str(item.get("id") or ""),
            folder=folder,
            subject=str(item.get("subject") or ""),
            from_address=str(sender.get("email") or ""),
            from_name=str(sender.get("name") or ""),
            to=_addresses(item.get("to")),
            cc=_addresses(item.get("cc")),
            bcc=_addresses(item.get("bcc")),
            reply_to=_addresses(item.get("replyTo")),
            received=item.get("receivedAt"),
            sent=item.get("sentAt"),
            is_read=bool(keywords.get("$seen")),
            is_flagged=flagged,
            flag_status="Flagged" if flagged else "NotFlagged",
            has_attachments=bool(item.get("hasAttachment")),
            importance=importance or None,
            internet_message_id=f"<{message_ids[0]}>" if message_ids else None,
            conversation_id=item.get("threadId"),
            preview=preview_text(str(item.get("preview") or "")),
            size=item.get("size"),
        )
        if body_format is not None:
            html = _joined_text(item, "htmlBody", "text/html") or ""
            text = _joined_text(item, "textBody", "text/plain") or ""
            body, actual = render_body(html, text, body_format)
            message.body = body
            message.body_format = actual
            message.preview = preview_text(text or body)
            message.attachments = [_attachment_info(part) for part in _file_parts(item)]
            message.has_attachments = bool(message.attachments)
        return message

    def _email(
        self, message_id: str, properties: Sequence[str], bodies: bool = False,
    ) -> Dict[str, Any]:
        args: Dict[str, Any] = {"ids": [message_id], "properties": list(properties)}
        if bodies:
            args.update(fetchTextBodyValues=True, fetchHTMLBodyValues=True)
        for item in self._call("Email/get", args).get("list") or ():
            if str(item.get("id")) == message_id:
                return item
        raise MailBackendError(
            f"message {message_id!r} not found for {self.email}; JMAP ids are "
            f"mailbox-wide, so list or search again for a current id"
        )

    def _filter(
        self,
        *,
        query: Optional[str],
        from_address: Optional[str],
        subject: Optional[str],
        since: Optional[str],
        before: Optional[str],
        unread_only: bool,
        has_attachments: Optional[bool],
        flag_status: Optional[str],
        to_address: Optional[str],
        body: Optional[str],
        is_read: Optional[bool],
        mailbox: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if flag_status and flag_status not in self.flag_statuses:
            raise MailBackendError(
                f"flag_status={flag_status!r}: JMAP has only the $flagged keyword, "
                f"so it can search {' or '.join(self.flag_statuses)} only"
            )
        conditions: List[Dict[str, Any]] = [mailbox] if mailbox else []
        for key, value in (
            ("text", query), ("from", from_address), ("to", to_address),
            ("subject", subject), ("body", body),
        ):
            if value:
                conditions.append({key: value})
        if since:
            conditions.append({"after": _utc_date(since)})
        if before:
            conditions.append({"before": _utc_date(before)})
        seen = is_read if is_read is not None else (False if unread_only else None)
        if seen is not None:
            conditions.append({"hasKeyword" if seen else "notKeyword": "$seen"})
        if flag_status:
            conditions.append({"hasKeyword" if flag_status == "Flagged" else "notKeyword": "$flagged"})
        if has_attachments is not None:
            conditions.append({"hasAttachment": bool(has_attachments)})
        if len(conditions) == 1:
            return conditions[0]
        return {"operator": "AND", "conditions": conditions} if conditions else {}

    def _emails(
        self, condition: Dict[str, Any], limit: int, offset: int = 0,
    ) -> List[Dict[str, Any]]:
        query: Dict[str, Any] = {"sort": [{"property": "receivedAt", "isAscending": False}]}
        if condition:
            query["filter"] = condition
        return self._query_get("Email", query, _SUMMARY_PROPERTIES, offset, max(1, limit))

    def test_connection(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {
            "connected": False,
            "backend": self.kind,
            "email": self.email,
            "session_url": self.account.get("session_url"),
            "auth": "bearer" if self.account.get("api_token") else "basic",
        }
        try:
            folders = self._load_folders(refresh=True)
            inbox = self._match_folder(folders, "inbox")
            info.update(
                connected=True,
                account_id=self._account_id,
                folder_count=len(folders),
                inbox_total=inbox.total_count if inbox else None,
                can_send=_SUBMISSION in (self._session_data().get("capabilities") or {}),
            )
        except MailBackendError as exc:
            info["error"] = str(exc)
        return info

    def list_messages(
        self,
        folder: str = "inbox",
        limit: int = 25,
        offset: int = 0,
        unread_only: bool = False,
    ) -> List[MailMessage]:
        condition = self._filter(
            query=None, from_address=None, subject=None, since=None, before=None,
            unread_only=unread_only, has_attachments=None, flag_status=None,
            to_address=None, body=None, is_read=None,
            mailbox={"inMailbox": self._folder(folder).id},
        )
        return [self._message(item, folder) for item in self._emails(condition, limit, offset)]

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
        flag_status: Optional[str] = None,
        *,
        to_address: Optional[str] = None,
        body: Optional[str] = None,
        is_read: Optional[bool] = None,
    ) -> List[MailMessage]:
        condition = self._filter(
            query=query, from_address=from_address, subject=subject, since=since,
            before=before, unread_only=unread_only, has_attachments=has_attachments,
            flag_status=flag_status, to_address=to_address, body=body, is_read=is_read,
            mailbox={"inMailbox": self._folder(folder).id},
        )
        return [self._message(item, folder) for item in self._emails(condition, limit)]

    def search_all_folders(
        self,
        include: Sequence[str] = (),
        query: Optional[str] = None,
        from_address: Optional[str] = None,
        subject: Optional[str] = None,
        since: Optional[str] = None,
        before: Optional[str] = None,
        unread_only: bool = False,
        has_attachments: Optional[bool] = None,
        limit: int = 25,
        flag_status: Optional[str] = None,
        *,
        to_address: Optional[str] = None,
        body: Optional[str] = None,
        is_read: Optional[bool] = None,
    ) -> Tuple[List[MailMessage], List[str]]:
        # One Email/query over the whole account: inMailboxOtherThan keeps a
        # message that is in at least one covered mailbox.
        folders = self._load_folders(refresh=True)
        roots = [
            f.path for f in folders
            if f.well_known in ALL_FOLDERS_EXCLUDED and f.well_known not in include
        ]
        excluded = {
            f.id for f in folders
            if any(f.path == root or f.path.startswith(root + "/") for root in roots)
        }
        covered = [f for f in folders if f.id not in excluded]
        condition = self._filter(
            query=query, from_address=from_address, subject=subject, since=since,
            before=before, unread_only=unread_only, has_attachments=has_attachments,
            flag_status=flag_status, to_address=to_address, body=body, is_read=is_read,
            mailbox={"inMailboxOtherThan": sorted(excluded)} if excluded else None,
        )
        paths = {f.id: f.path for f in covered}
        messages = []
        for item in self._emails(condition, limit):
            mailbox_ids = [ident for ident, on in (item.get("mailboxIds") or {}).items() if on]
            folder = next((paths[ident] for ident in paths if ident in mailbox_ids), "")
            messages.append(self._message(item, folder))
        return messages, [f.path for f in covered]

    def get_message(
        self,
        message_id: str,
        folder: str = "inbox",
        body_format: str = "markdown",
    ) -> MailMessage:
        item = self._email(message_id, _DETAIL_PROPERTIES, bodies=True)
        return self._message(item, self._folder_label(item, folder), body_format)

    # ------------------------------------------------------------------
    # attachments
    # ------------------------------------------------------------------
    def list_attachments(
        self,
        message_id: str,
        folder: str = "inbox",
        include_inline: bool = True,
    ) -> List[MailAttachmentInfo]:
        item = self._email(message_id, _PART_PROPERTIES)
        items = [_attachment_info(part) for part in _file_parts(item)]
        return items if include_inline else [i for i in items if not i.is_inline]

    def get_attachment(
        self,
        message_id: str,
        folder: str = "inbox",
        attachment_id: Optional[str] = None,
        attachment_name: Optional[str] = None,
    ) -> MailAttachmentContent:
        parts = _file_parts(self._email(message_id, _PART_PROPERTIES))
        chosen = select_attachment([_attachment_info(p) for p in parts], attachment_id, attachment_name)
        part = next(p for p in parts if _attachment_info(p).attachment_id == chosen.attachment_id)
        content_type = chosen.content_type or "application/octet-stream"
        return MailAttachmentContent(
            name=chosen.name,
            content=self._download(str(part["blobId"]), chosen.name, content_type),
            content_type=content_type,
            attachment_id=chosen.attachment_id,
            is_inline=chosen.is_inline,
            content_id=chosen.content_id,
        )

    def get_mime(self, message_id: str, folder: str = "inbox") -> bytes:
        item = self._email(message_id, ["blobId"])
        return self._download(str(item["blobId"]), "message.eml", "message/rfc822")

    # ------------------------------------------------------------------
    # keywords and mailboxes
    # ------------------------------------------------------------------
    def _update(self, message_id: str, patch: Dict[str, Any], action: str) -> None:
        result = self._call("Email/set", {"update": {message_id: patch}})
        _updated("Email/set", result, message_id, f"{action} message {message_id!r}")

    def _destroy(self, message_id: str) -> None:
        result = self._call("Email/set", {"destroy": [message_id]})
        _destroyed("Email/set", result, message_id, f"delete message {message_id!r}")

    def mark_message(
        self,
        message_id: str,
        folder: str = "inbox",
        is_read: Optional[bool] = None,
        is_flagged: Optional[bool] = None,
    ) -> Dict[str, Any]:
        if is_read is None and is_flagged is None:
            return {"changed": False, "backend": self.kind, "id": message_id, "folder": folder}
        patch: Dict[str, Any] = {}
        changed: Dict[str, Any] = {}
        for keyword, value, key in (("$seen", is_read, "is_read"), ("$flagged", is_flagged, "is_flagged")):
            if value is not None:
                patch[f"keywords/{keyword}"] = True if value else None
                changed[key] = bool(value)
        self._update(message_id, patch, "mark")
        return {"changed": True, "backend": self.kind, "id": message_id, "folder": folder, **changed}

    def move_message(
        self, message_id: str, destination: str, folder: str = "inbox",
    ) -> Dict[str, Any]:
        target = self._folder(destination)
        # A move leaves the message in the destination only (RFC 8621 4.6).
        self._update(message_id, {"mailboxIds": {target.id: True}}, "move")
        return {
            "moved": True,
            "backend": self.kind,
            "id": message_id,
            "previous_id": message_id,
            "folder": destination,
            "source": folder,
            "destination": destination,
            "note": "JMAP message ids are mailbox-wide, so the id is unchanged",
        }

    def delete_message(
        self, message_id: str, folder: str = "inbox", hard_delete: bool = False,
    ) -> Dict[str, Any]:
        if hard_delete:
            self._destroy(message_id)
            return {
                "deleted": True, "hard_delete": True, "backend": self.kind,
                "id": message_id, "folder": folder,
            }
        trash = self._folder("trash")
        current = self._email(message_id, ["mailboxIds"]).get("mailboxIds") or {}
        if [ident for ident, on in current.items() if on] == [trash.id]:
            self._destroy(message_id)
            return {
                "deleted": True, "hard_delete": True, "backend": self.kind,
                "id": message_id, "folder": folder,
                "note": "message was already in Trash - destroyed",
            }
        self._update(message_id, {"mailboxIds": {trash.id: True}}, "move to Trash")
        return {
            "deleted": True,
            "hard_delete": False,
            "backend": self.kind,
            "id": message_id,
            "previous_id": message_id,
            "folder": "trash",
            "destination": trash.path,
            "note": "JMAP message ids are mailbox-wide, so the id is unchanged",
        }

    # ------------------------------------------------------------------
    # composing
    # ------------------------------------------------------------------
    @staticmethod
    def _bodies(body: str, body_format: str) -> Tuple[str, str]:
        """``(text, html)`` for a caller body, composed as the IMAP backend does."""
        body_format = body_format or "markdown"
        html, _ = compose_body(body or "", body_format)
        text = html_to_text(html) if body_format == "html" else (body or "")
        return text, html

    @staticmethod
    def _tracking(message: OutgoingMessage) -> Tuple[Optional[str], Dict[str, Any]]:
        """The tracking id put on the wire and its header property; None while off."""
        header = outbound_tracking_header()
        if not header or not message.tracking_id:
            return None, {}
        return message.tracking_id, {f"header:{header}:asText": message.tracking_id}

    def _upload_files(self, files: Sequence[OutgoingAttachment]) -> List[Dict[str, Any]]:
        """Upload every new file before anything is created, so a failure creates nothing."""
        if not files:
            return []
        account = (self._session_data().get("accounts") or {}).get(self._account_id) or {}
        mail = (account.get("accountCapabilities") or {}).get(_MAIL) or {}
        total = sum(len(f.content) for f in files)
        cap = mail.get("maxSizeAttachmentsPerEmail")
        if isinstance(cap, int) and total > cap:
            raise MailBackendError(
                f"attachments total {total} bytes, over this JMAP account's "
                f"{cap}-byte limit per message (maxSizeAttachmentsPerEmail)"
            )
        parts = []
        for attachment in files:
            content_type = attachment.content_type or "application/octet-stream"
            part: Dict[str, Any] = {
                "blobId": self._upload(attachment.content, content_type),
                "type": content_type,
                "name": attachment.name,
                "disposition": "inline" if attachment.is_inline else "attachment",
            }
            if attachment.content_id:
                part["cid"] = attachment.content_id.strip("<>")
            parts.append(part)
        return parts

    def _new_email(
        self,
        *,
        to: Sequence[str],
        cc: Sequence[str],
        bcc: Sequence[str],
        subject: str,
        text: str,
        html: str,
        files: Sequence[Dict[str, Any]],
        extra: Dict[str, Any],
    ) -> Dict[str, Any]:
        """An Email to create in Drafts with ``$draft``, From the mailbox address."""
        email: Dict[str, Any] = {
            "mailboxIds": {self._folder("drafts").id: True},
            "keywords": {"$draft": True, "$seen": True},
            "from": [{"email": self.email}],
            "subject": subject,
            "textBody": [{"partId": "text", "type": "text/plain"}],
            "htmlBody": [{"partId": "html", "type": "text/html"}],
            "bodyValues": {"text": {"value": text}, "html": {"value": html}},
            **extra,
        }
        for key, addresses in (("to", to), ("cc", cc), ("bcc", bcc)):
            if addresses:
                email[key] = _recipients(addresses)
        if files:
            email["attachments"] = list(files)
        return email

    def _compose(self, message: OutgoingMessage) -> Tuple[Dict[str, Any], Optional[str]]:
        tracking_id, extra = self._tracking(message)
        text, html = self._bodies(message.body, message.body_format)
        email = self._new_email(
            to=[a for a in message.to if a], cc=[a for a in message.cc if a],
            bcc=[a for a in message.bcc if a], subject=message.subject or "",
            text=text, html=html, files=self._upload_files(message.attachments), extra=extra,
        )
        return email, tracking_id

    def _reply_recipients(
        self, original: Dict[str, Any], message: OutgoingMessage,
    ) -> Tuple[List[str], List[str], List[str]]:
        """The IMAP reply rules, answering Reply-To when the original has one.

        The sender (plus the original To on reply-all) go in To, the caller's
        Cc (plus the original Cc on reply-all) in Cc. Nobody appears twice and
        Bcc recipients stay blind. Reply-all never mails us; a plain reply
        keeps its sender even when that is us.
        """
        blind = _unique(message.bcc)
        seen = {address.lower() for address in blind}
        to_sources = [a for a in message.to if a] or (
            _addresses(original.get("replyTo")) or _addresses(original.get("from"))
        )
        cc_sources = [a for a in message.cc if a]
        if message.reply_all:
            seen.add(self.email.lower())
            to_sources = to_sources + _addresses(original.get("to"))
            cc_sources = cc_sources + _addresses(original.get("cc"))
        to: List[str] = []
        cc: List[str] = []
        for target, sources in ((to, to_sources), (cc, cc_sources)):
            for address in sources:
                if address and address.lower() not in seen:
                    target.append(address)
                    seen.add(address.lower())
        return to, cc, blind

    def _compose_reply(
        self, message_id: str, message: OutgoingMessage,
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """A threaded reply quoting the original, like the IMAP reply draft."""
        original = self._email(message_id, _SOURCE_PROPERTIES, bodies=True)
        to, cc, bcc = self._reply_recipients(original, message)
        base = str(original.get("subject") or "")
        subject = message.subject or (base if base.lower().startswith("re:") else f"Re: {base}".strip())
        original_text = _joined_text(original, "textBody", "text/plain")
        if original_text is None:
            original_text = html_to_text(_joined_text(original, "htmlBody", "text/html") or "")
        attribution = (
            f"On {_header_text(original, 'Date') or 'the original message'}, "
            f"{_header_text(original, 'From') or 'the sender'} wrote:"
        )
        text, html = self._bodies(message.body, message.body_format)
        text = (
            text.rstrip() + "\n\n" + attribution + "\n"
            + "\n".join("> " + line for line in original_text.splitlines())
        )
        html += f"<p>{escape(attribution)}</p><blockquote><pre>{escape(original_text)}</pre></blockquote>"
        tracking_id, extra = self._tracking(message)
        message_ids = [str(i) for i in original.get("messageId") or ()]
        if message_ids:
            references = [str(i) for i in original.get("references") or original.get("inReplyTo") or ()]
            if message_ids[0] not in references:
                references.append(message_ids[0])
            extra.update(inReplyTo=message_ids[:1], references=references)
        email = self._new_email(
            to=to, cc=cc, bcc=bcc, subject=subject, text=text, html=html,
            files=self._upload_files(message.attachments), extra=extra,
        )
        return email, tracking_id

    def _compose_forward(
        self, message_id: str, message: OutgoingMessage,
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """A forward carrying the original bodies and every original file by blob."""
        original = self._email(message_id, _SOURCE_PROPERTIES, bodies=True)
        original_subject = str(original.get("subject") or "")
        subject = message.subject or (
            original_subject if original_subject.lower().startswith(("fwd:", "fw:"))
            else f"Fwd: {original_subject}".strip()
        )
        forwarded = "\n".join(
            f"{name}: {_header_text(original, name)}"
            for name in _QUOTED_HEADERS if _header_text(original, name)
        )
        text, html = self._bodies(message.body, message.body_format)
        plain_prefix = f"{text}\n\n---------- Forwarded message ----------\n{forwarded}\n\n"
        html_prefix = f"{html}<hr><pre>{escape(forwarded)}</pre>"
        original_plain = _joined_text(original, "textBody", "text/plain") or ""
        original_html = _joined_text(original, "htmlBody", "text/html")
        if original_html:
            body_tag = re.search(r"<body\b[^>]*>", original_html, flags=re.IGNORECASE)
            offset = body_tag.end() if body_tag else 0
            html = original_html[:offset] + html_prefix + original_html[offset:]
        elif original_plain:
            html = html_prefix + f"<pre>{escape(original_plain)}</pre>"
        else:
            html = html_prefix
        tracking_id, extra = self._tracking(message)
        files = [_part_reference(part) for part in _file_parts(original)]
        email = self._new_email(
            to=[a for a in message.to if a], cc=[a for a in message.cc if a],
            bcc=[a for a in message.bcc if a], subject=subject,
            text=plain_prefix + original_plain, html=html,
            files=files + self._upload_files(message.attachments), extra=extra,
        )
        return email, tracking_id

    # ------------------------------------------------------------------
    # drafts
    # ------------------------------------------------------------------
    def _save_draft(
        self, email: Dict[str, Any], tracking_id: Optional[str], cleanup_on_failure: bool,
    ) -> Dict[str, Any]:
        details: Dict[str, Any] = {
            "draft_id": None, "folder": "drafts", "backend": self.kind, "stage": "create",
            "sent": False, "cleanup_attempted": False, "draft_removed": False,
        }
        try:
            created = _created(
                "Email/set", self._call("Email/set", {"create": {"draft": email}}),
                "draft", "create the draft",
            )
            details["draft_id"] = draft_id = str(created["id"])
            details["stage"] = "attachments"
            attachments = self.list_attachments(draft_id, "drafts")
        except MailBackendError as exc:
            if cleanup_on_failure and details["draft_id"]:
                details["cleanup_attempted"] = True
                try:
                    self._destroy(details["draft_id"])
                    details["draft_removed"] = True
                except MailBackendError as cleanup_exc:
                    details["cleanup_error"] = str(cleanup_exc)
            if details["draft_removed"]:
                details["recovery"] = "The new draft was removed; no message was sent."
            elif details["draft_id"] or exc.details.get("submission_uncertain"):
                details["recovery"] = (
                    "No message was sent. Inspect the drafts folder (draft_id when "
                    "given) before retrying; a saved draft may remain."
                )
            else:
                details["recovery"] = "The server refused the draft; nothing was saved or sent."
            raise MailBackendError(str(exc), details=details) from exc
        return {
            "id": draft_id, "draft_id": draft_id, "folder": "drafts",
            "backend": self.kind, "sent": False,
            "attachments": [item.to_dict() for item in attachments],
            "attachments_verified": True, "cleanup_attempted": False,
            "draft_removed": False, "tracking_id": tracking_id,
        }

    def create_draft(self, message: OutgoingMessage) -> Dict[str, Any]:
        email, tracking_id = self._compose(message)
        return self._save_draft(email, tracking_id, message.cleanup_on_failure)

    def create_reply_draft(
        self, message_id: str, message: OutgoingMessage, folder: str = "inbox",
    ) -> Dict[str, Any]:
        email, tracking_id = self._compose_reply(message_id, message)
        return self._save_draft(email, tracking_id, message.cleanup_on_failure)

    def create_forward_draft(
        self, message_id: str, message: OutgoingMessage, folder: str = "inbox",
    ) -> Dict[str, Any]:
        email, tracking_id = self._compose_forward(message_id, message)
        return self._save_draft(email, tracking_id, message.cleanup_on_failure)

    def _stored_draft(self, draft_id: str, properties: Sequence[str], operation: str) -> Dict[str, Any]:
        item = self._email(draft_id, properties, bodies=True)
        if not (item.get("keywords") or {}).get("$draft"):
            raise MailBackendError(
                f"{operation} requires a saved draft (an Email with the $draft keyword); "
                f"{draft_id!r} is not one"
            )
        return item

    def _rewrite_draft(
        self,
        draft_id: str,
        folder: str,
        operation: str,
        *,
        subject: Optional[str] = None,
        bodies: Optional[Tuple[str, str]] = None,
        edit_files: Any = None,
    ) -> Tuple[str, List[MailAttachmentInfo], Dict[str, Any]]:
        """Replace a draft and return its new identity, attachments and cleanup outcome.

        The replacement keeps every header of the original (raw, so Message-ID,
        threading and tracking headers survive), its keywords, mailboxes and
        files by blob id; only ``subject``, ``bodies`` and ``edit_files`` change
        it. The original is destroyed only once the replacement exists.
        """
        old = self._stored_draft(draft_id, _DRAFT_PROPERTIES, operation)
        headers: Dict[str, Tuple[str, List[str]]] = {}
        for header in old.get("headers") or ():
            name = str(header.get("name") or "")
            lowered = name.lower()
            if name and not lowered.startswith("content-") and lowered != "mime-version":
                headers.setdefault(lowered, (name, []))[1].append(str(header.get("value") or ""))
        email: Dict[str, Any] = {
            "mailboxIds": old.get("mailboxIds") or {self._folder("drafts").id: True},
            "keywords": old.get("keywords") or {"$draft": True},
        }
        if subject is not None:
            headers.pop("subject", None)
            email["subject"] = subject
        email.update({f"header:{name}:all": values for name, values in headers.values()})
        text = _joined_text(old, "textBody", "text/plain")
        html = _joined_text(old, "htmlBody", "text/html")
        if bodies is not None:
            text, html = bodies
        values: Dict[str, Any] = {}
        for key, value, part in (("text", text, "textBody"), ("html", html, "htmlBody")):
            if value is not None:
                email[part] = [{"partId": key, "type": f"text/{'plain' if key == 'text' else 'html'}"}]
                values[key] = {"value": value}
        if values:
            email["bodyValues"] = values
        # edit_files gets the original parts (with partIds) and returns the parts to keep.
        parts = _file_parts(old)
        files = [_part_reference(part) for part in (edit_files(parts) if edit_files else parts)]
        if files:
            email["attachments"] = files

        try:
            new_id = str(_created(
                "Email/set", self._call("Email/set", {"create": {"draft": email}}),
                "draft", "create the replacement draft",
            )["id"])
        except MailBackendError as exc:
            raise MailBackendError(
                f"{self.email}: cannot rewrite draft {draft_id!r}: {exc} The original draft is unchanged.",
                details={
                    "draft_id": None, "previous_id": draft_id, "folder": folder,
                    "stage": "create", "sent": False, "original_removed": False,
                },
            ) from exc

        cleanup: Dict[str, Any] = {"original_removed": False, "attachments_verified": False}
        warnings: List[str] = []
        account_id = self._account_id
        try:
            responses = self._api([
                ["Email/set", {"accountId": account_id, "destroy": [draft_id]}, "d"],
                ["Email/get", {
                    "accountId": account_id, "ids": [new_id], "properties": _PART_PROPERTIES,
                }, "g"],
            ])
        except MailBackendError as exc:
            responses = []
            warnings.append(
                f"Replacement draft {new_id} was saved, but the follow-up request failed: {exc}. "
                "Use the replacement ID and do not repeat the mutation; the original may remain."
            )
        attachments = [
            MailAttachmentInfo(name=str(f.get("name") or ""), content_type=f.get("type"))
            for f in files
        ]
        if responses:
            try:
                _destroyed("Email/set", self._result(responses, "d", "Email/set"), draft_id,
                           f"delete the original draft {draft_id!r}")
                cleanup["original_removed"] = True
            except MailBackendError as exc:
                warnings.append(
                    f"Replacement draft {new_id} was saved, but cleanup of original {draft_id} "
                    f"was not confirmed: {exc}. Use the replacement ID and do not repeat the "
                    "mutation; the original may remain."
                )
            try:
                rows = self._result(responses, "g", "Email/get").get("list") or []
                attachments = [_attachment_info(part) for part in _file_parts(rows[0])]
                cleanup["attachments_verified"] = True
            except (MailBackendError, IndexError) as exc:
                warnings.append(
                    f"Replacement draft {new_id} was saved, but rereading it failed: {exc}. "
                    "Attachment metadata is from the request and has no ids."
                )
        if warnings:
            cleanup["warnings"] = warnings
        return new_id, attachments, cleanup

    def update_draft(
        self, draft_id: str, subject: Optional[str] = None,
        body: Optional[str] = None, body_format: str = "markdown",
        folder: str = "drafts",
    ) -> Dict[str, Any]:
        bodies = None
        if body is not None:
            text, html = self._bodies(body, body_format)
            bodies = (text, html if body else "")
        new_id, attachments, cleanup = self._rewrite_draft(
            draft_id, folder, "update_draft", subject=subject, bodies=bodies,
        )
        return {
            "id": new_id, "draft_id": new_id, "previous_id": draft_id,
            "folder": folder, "backend": self.kind, "sent": False,
            "attachments": [item.to_dict() for item in attachments],
            "note": "JMAP messages are immutable, so the draft was replaced and its id changed",
            **cleanup,
        }

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
        self._stored_draft(message_id, ["keywords"], "add attachments")
        cid = content_id.strip("<>") if is_inline and content_id else None
        (part,) = self._upload_files([OutgoingAttachment(
            name=name, content=content, content_type=content_type or "application/octet-stream",
            is_inline=is_inline, content_id=cid,
        )])
        new_id, attachments, cleanup = self._rewrite_draft(
            message_id, folder, "add attachments", edit_files=lambda parts: parts + [part],
        )
        added = next((a for a in reversed(attachments) if a.name == name), None)
        result = {
            "added": True,
            "backend": self.kind,
            "id": new_id,
            "previous_id": message_id,
            "folder": folder,
            "attachment_name": name,
            "attachment_size": len(content),
            "attachment_id": added.attachment_id if added else None,
            "attachments": [item.to_dict() for item in attachments],
            "is_inline": bool(is_inline),
            "note": "JMAP messages are immutable, so the draft was replaced and its id changed",
            **cleanup,
        }
        if cid:
            result["content_id"] = cid
        return result

    def delete_attachment(
        self,
        message_id: str,
        folder: str = "drafts",
        attachment_id: Optional[str] = None,
        attachment_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        removed: Dict[str, Any] = {}

        def drop(parts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            infos = [_attachment_info(p) for p in parts]
            chosen = select_attachment(infos, attachment_id, attachment_name)
            removed.update(name=chosen.name, attachment_id=chosen.attachment_id)
            return [p for p, info in zip(parts, infos) if info.attachment_id != chosen.attachment_id]

        new_id, attachments, cleanup = self._rewrite_draft(
            message_id, folder, "delete attachments", edit_files=drop,
        )
        return {
            "deleted": True,
            "backend": self.kind,
            "id": new_id,
            "previous_id": message_id,
            "folder": folder,
            "attachment_name": removed.get("name"),
            "removed_attachment_id": removed.get("attachment_id"),
            "attachments": [item.to_dict() for item in attachments],
            "note": "JMAP messages are immutable, so the draft was replaced and its id changed",
            **cleanup,
        }

    # ------------------------------------------------------------------
    # sending
    # ------------------------------------------------------------------
    def _require_submission(self) -> None:
        session = self._session_data()
        account = (session.get("accounts") or {}).get(self._account_id) or {}
        account_caps = account.get("accountCapabilities")
        if _SUBMISSION not in (session.get("capabilities") or {}) or (
            isinstance(account_caps, dict) and account_caps and _SUBMISSION not in account_caps
        ):
            raise self._unsupported(
                "send mail",
                f"the JMAP session offers no {_SUBMISSION} for this account "
                "(a Fastmail API token needs submission access); drafts can still be saved",
            )

    def _identity(self, address: str) -> Dict[str, Any]:
        """The sending Identity for ``address``: exact match first, then ``*@domain``."""
        self._require_submission()
        wanted = address.lower()
        domain = wanted.rpartition("@")[2]
        for refresh in (False, True):
            with self._lock:
                if self._identities is None or refresh:
                    self._identities = list(self._call("Identity/get", {"ids": None}).get("list") or [])
                identities = self._identities
            for pattern in (wanted, f"*@{domain}"):
                for identity in identities:
                    if str(identity.get("email") or "").lower() == pattern:
                        return identity
        known = ", ".join(sorted(str(i.get("email")) for i in identities)) or "<none>"
        raise MailBackendError(
            f"{self.email}: no JMAP sending identity for {address}; identities on this "
            f"account: {known}. Create one for this address on the server."
        )

    def _failure(
        self, exc: MailBackendError, *, draft_id: Optional[str], stage: str,
        cleanup: bool = False, uncertain: bool = False,
    ) -> MailBackendError:
        """A send failure that says what happened to the draft and how to recover."""
        cleanup_result: Dict[str, Any] = {"requested": cleanup, "attempted": False, "deleted": False}
        retained: Optional[bool] = None if uncertain else bool(draft_id)
        if cleanup and draft_id and not uncertain:
            cleanup_result["attempted"] = True
            try:
                self._destroy(draft_id)
                cleanup_result["deleted"] = True
                retained = False
            except MailBackendError as cleanup_exc:
                cleanup_result["error"] = str(cleanup_exc)
        if uncertain:
            recovery = (
                "Submission outcome unknown. Check Sent and Drafts before any retry; "
                "retrying blindly may send a duplicate."
            )
        elif cleanup_result["deleted"]:
            recovery = "No message was sent; the new draft was removed."
        elif draft_id:
            recovery = "No message was sent. Inspect the retained draft before retrying."
        else:
            recovery = "No message was sent and no draft was created."
        return MailBackendError(
            f"{exc} {recovery}",
            details={
                "draft_id": draft_id, "folder": "drafts", "backend": self.kind,
                "stage": stage, "sent": False,
                "accepted_by_mail_server": None if uncertain else False,
                "delivery_confirmed": False, "submission_uncertain": uncertain,
                "retry_safe": False, "draft_retained": retained,
                "cleanup": cleanup_result, "recovery": recovery,
            },
        )

    def _submit(
        self,
        recipients: Sequence[str],
        *,
        sender: Optional[str] = None,
        draft_id: Optional[str] = None,
        create: Optional[Dict[str, Any]] = None,
        cleanup: bool = False,
    ) -> Dict[str, Any]:
        """Submit ``draft_id``, or create ``create`` and submit it in the same request.

        On success the Email moves to Sent without ``$draft``. The envelope is
        explicit, so the accepted recipients are exactly ``recipients``.
        """
        sender = sender or self.email
        identity = self._identity(sender)
        sent = self._optional_folder("sent")
        filed: Dict[str, Any] = {"keywords/$draft": None}
        if sent is not None:
            filed["mailboxIds"] = {sent.id: True}
        account_id = self._account_id
        calls: List[List[Any]] = []
        if create is not None:
            calls.append(["Email/set", {"accountId": account_id, "create": {"draft": create}}, "c"])
        calls.append(["EmailSubmission/set", {
            "accountId": account_id,
            "create": {"send": {
                "identityId": identity["id"],
                "emailId": "#draft" if create is not None else draft_id,
                "envelope": {
                    "mailFrom": {"email": sender},
                    "rcptTo": _recipients(recipients),
                },
            }},
            "onSuccessUpdateEmail": {"#send": filed},
        }, "s"])
        try:
            responses = self._api(calls)
        except MailBackendError as exc:
            raise self._failure(
                exc, draft_id=draft_id, stage="send",
                uncertain=bool(exc.details.get("submission_uncertain")),
            ) from exc
        if create is not None:
            try:
                draft_id = str(_created(
                    "Email/set", self._result(responses, "c", "Email/set"), "draft", "create the message",
                )["id"])
            except MailBackendError as exc:
                raise self._failure(exc, draft_id=None, stage="create") from exc
        try:
            submission = _created(
                "EmailSubmission/set", self._result(responses, "s", "EmailSubmission/set"),
                "send", "submit the message",
            )
        except MailBackendError as exc:
            raise self._failure(exc, draft_id=draft_id, stage="send", cleanup=cleanup) from exc
        warnings: List[str] = []
        saved_to_sent = None
        filed_ok = False
        try:
            _updated("Email/set", self._result(responses, "s", "Email/set"), str(draft_id),
                     "file the sent message")
            filed_ok = True
            saved_to_sent = sent.path if sent is not None else None
        except MailBackendError as exc:
            warnings.append(
                f"The server accepted the message, but filing its Sent copy failed: {exc}. "
                "Do not resend it."
            )
        if sent is None:
            warnings.append("No Sent mailbox exists, so the sent copy stays in Drafts without $draft.")
        result: Dict[str, Any] = {
            "sent": True,
            "accepted_by_mail_server": True,
            "delivery_confirmed": False,
            "accepted_recipients": list(recipients),
            "partial_failure": False,
            "refused_recipients": {},
            "backend": self.kind,
            "saved_to_sent": saved_to_sent,
            "sent_copy_id": draft_id,
            # The draft itself became the sent copy; it is no longer a draft.
            "draft_removed": filed_ok,
            "submission_id": submission.get("id"),
            "identity": identity.get("email"),
        }
        for key, name in (("undoStatus", "undo_status"), ("deliveryStatus", "delivery_status")):
            if submission.get(key):
                result[name] = submission[key]
        if warnings:
            result["warnings"] = warnings
        return result

    def _send_new(
        self, email: Dict[str, Any], message: OutgoingMessage, tracking_id: Optional[str],
    ) -> Dict[str, Any]:
        to, cc, bcc = (_addresses(email.get(key)) for key in ("to", "cc", "bcc"))
        if not to:
            raise MailBackendError("no recipients: 'to' is required")
        result = self._submit(
            _unique(to + cc + bcc), create=email, cleanup=message.cleanup_on_failure,
        )
        result.update(
            to=to, cc=cc, bcc=bcc, subject=email.get("subject") or "", tracking_id=tracking_id,
        )
        return result

    def send_message(self, message: OutgoingMessage) -> Dict[str, Any]:
        self._identity(self.email)  # refuse before uploading or creating anything
        if message.reply_to_message_id:
            email, tracking_id = self._compose_reply(message.reply_to_message_id, message)
        else:
            if not [a for a in message.to if a]:
                raise MailBackendError("no recipients: 'to' is required")
            email, tracking_id = self._compose(message)
        result = self._send_new(email, message, tracking_id)
        result["replied_to"] = message.reply_to_message_id
        return result

    def forward_message(
        self, message_id: str, message: OutgoingMessage, folder: str = "inbox",
    ) -> Dict[str, Any]:
        if not [a for a in message.to if a]:
            raise MailBackendError("no recipients: 'to' is required")
        self._identity(self.email)
        email, tracking_id = self._compose_forward(message_id, message)
        result = self._send_new(email, message, tracking_id)
        result["forwarded_from"] = message_id
        return result

    def send_draft(
        self, draft_id: str, folder: str = "drafts", dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Submit the stored draft unchanged; dry_run only reads it."""
        item = self._stored_draft(
            draft_id, ["keywords", "from", "to", "cc", "bcc", "subject", *_BODY_PROPERTIES], "send_draft",
        )
        to, cc, bcc = (_addresses(item.get(key)) for key in ("to", "cc", "bcc"))
        envelope = _unique(to + cc + bcc)
        if not envelope:
            raise MailBackendError("no recipients in stored draft")
        subject = str(item.get("subject") or "")
        result: Dict[str, Any] = {
            "draft_id": draft_id,
            "backend": self.kind,
            "sent": False,
            "accepted_by_mail_server": False,
            "delivery_confirmed": False,
            "subject": subject,
        }
        if dry_run:
            html = _joined_text(item, "htmlBody", "text/html") or ""
            text = _joined_text(item, "textBody", "text/plain") or ""
            body, _ = render_body(html, text, "text")
            result.update(
                dry_run=True, would_send_to=to, would_cc=cc, would_bcc=bcc,
                body_preview=preview_text(text or body),
                attachments_count=len(_file_parts(item)),
            )
            return result
        sender = next(iter(_addresses(item.get("from"))), None)
        result.update(self._submit(envelope, sender=sender, draft_id=draft_id))
        result.update(to=to, cc=cc, bcc=bcc)
        return result
