"""IMAP + SMTP mailbox backend.

Serves iCloud Mail (``imap.mail.me.com`` / ``smtp.mail.me.com``, app-specific
password) and any other plain IMAP4rev1 + SMTP server. Accounts with an OAuth
``refresh_token`` (Gmail) sign in with SASL XOAUTH2 instead (see
``google_oauth``). Uses only the stdlib (``imaplib``, ``smtplib``, ``email``)
plus httpx for the token refresh.

Notes on the protocol choices:

* Message ids are IMAP **UIDs** prefixed with the folder's ``UIDVALIDITY``
  (``"<uidvalidity>:<uid>"``) so a stale id from a rebuilt mailbox is rejected
  instead of silently addressing the wrong message.
* Listing uses ``UID SEARCH ALL`` and then fetches a window of the *highest*
  UIDs, which is the cheap approximation of "newest first" that every IMAP
  server supports (``SORT`` is an optional extension iCloud does not offer).
* Bodies are parsed with ``email.parser.BytesParser`` from a single
  ``BODY.PEEK[]`` fetch, so reading a message never marks it as read.
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import imaplib
import logging
import re
import smtplib
import socket
import ssl
import threading
import uuid
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from functools import wraps
from html import escape, unescape
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..body_format import compose_body, render_body
from ..utils import outbound_tracking_header
from .accounts import MailAccount
from .dav import DAVMixin
from .google_oauth import OAuthTokenSource, sign_in_hint, xoauth2_string
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

_log = logging.getLogger(__name__)

# IMAP \Special-Use attribute (RFC 6154) -> our well-known alias. Servers that
# advertise SPECIAL-USE (iCloud and SmarterMail both do) let us map folders
# without guessing names.
_SPECIAL_USE = {
    "\\inbox": "inbox",
    "\\sent": "sent",
    "\\drafts": "drafts",
    "\\trash": "trash",
    "\\junk": "junk",
    "\\archive": "archive",
    "\\all": "archive",
}

# Fallback name matching for servers without SPECIAL-USE.
_NAME_HINTS: Dict[str, Tuple[str, ...]] = {
    "inbox": ("inbox",),
    "sent": ("sent", "sent items", "sent messages", "sentmail"),
    "drafts": ("drafts", "draft"),
    "trash": ("trash", "deleted items", "deleted messages", "bin"),
    "junk": ("junk", "spam", "junk e-mail", "junk email"),
    "archive": ("archive", "archives", "all mail"),
}

# Response line of LIST: (attrs) "delimiter" "name"
_LIST_RE = re.compile(rb'^\((?P<attrs>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.*)$')

_FETCH_SUMMARY = (
    "(UID FLAGS RFC822.SIZE INTERNALDATE "
    "BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO CC DATE MESSAGE-ID "
    "REPLY-TO IMPORTANCE X-PRIORITY CONTENT-TYPE)])"
)

# IMAP knows only the \Flagged system flag, so there is no Complete state.
_FLAG_SEARCH_KEYS = {"NotFlagged": "UNFLAGGED", "Flagged": "FLAGGED"}


def _flag_status(flags: Sequence[str]) -> str:
    """``flag_status`` from lower-cased FETCH FLAGS."""
    return "Flagged" if "\\flagged" in flags else "NotFlagged"


def _decode_header_value(raw: Optional[str]) -> str:
    """RFC 2047 -> str, tolerating malformed encoded words."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:  # pragma: no cover - defensive
        return raw.strip()


def _addresses(raw: Optional[str]) -> List[str]:
    """Split a header into plain e-mail addresses."""
    if not raw:
        return []
    out: List[str] = []
    for _name, addr in email.utils.getaddresses([_decode_header_value(raw)]):
        if addr:
            out.append(addr)
    return out


def _sender(raw: Optional[str]) -> Tuple[str, str]:
    """Return ``(address, display_name)`` for a From-style header."""
    if not raw:
        return "", ""
    name, addr = email.utils.parseaddr(_decode_header_value(raw))
    return addr, name


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _header_date(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    try:
        return _iso(email.utils.parsedate_to_datetime(raw))
    except (TypeError, ValueError):
        return None


def _internaldate_iso(raw: str) -> Optional[str]:
    """Parse an IMAP INTERNALDATE (``dd-Mon-yyyy HH:MM:SS +ZZZZ``) to ISO-8601.

    ``imaplib.Internaldate2tuple`` returns a *local-time* struct_time, which
    silently shifts every timestamp by the container's TZ offset — so parse the
    string directly and keep the zone the server sent.
    """
    text = (raw or "").strip()
    for fmt in ("%d-%b-%Y %H:%M:%S %z", "%d-%b-%Y %H:%M:%S"):
        try:
            return _iso(datetime.strptime(text, fmt))
        except ValueError:
            continue
    return None


def _quote_mailbox(name: str) -> str:
    """Quote a mailbox name for an IMAP command."""
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _imap_literal(value: str) -> str:
    """Quote a SEARCH string argument."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _imap_date(value: str) -> str:
    """Convert an ISO date/datetime to IMAP's ``dd-Mon-yyyy``."""
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError as exc:
            raise MailBackendError(
                f"could not parse date {value!r}; use YYYY-MM-DD"
            ) from exc
    return dt.strftime("%d-%b-%Y")


def _split_message_id(message_id: str) -> Tuple[Optional[str], str]:
    """``"uidvalidity:uid"`` -> ``(uidvalidity, uid)``; bare uid is accepted."""
    text = str(message_id).strip()
    if ":" in text:
        validity, _, uid = text.rpartition(":")
        validity = validity.strip()
        uid = uid.strip()
    else:
        validity, uid = None, text
    if not uid.isdigit():
        raise MailBackendError(
            f"invalid IMAP message id {message_id!r}; expected '<uidvalidity>:<uid>'"
        )
    return (validity or None), uid


def _stamp_tracking_header(outgoing: Message, tracking_id: Optional[str]) -> Optional[str]:
    """Set the configured tracking header; return the ID now on the message.

    None while tracking is off: without a header name there is nowhere to
    publish an ID, so none is reported either.
    """
    header = outbound_tracking_header()
    if not header or not tracking_id:
        return None
    outgoing[header] = tracking_id
    return tracking_id


def _detach_part(root: Message, target: Message) -> bool:
    """Drop ``target`` from its multipart container. False if it has none."""
    for container in root.walk():
        if container.get_content_maintype() != "multipart":
            continue
        payload = container.get_payload()
        if isinstance(payload, list) and any(p is target for p in payload):
            container.set_payload([p for p in payload if p is not target])
            return True
    return False


def _mailbox_operation(method):
    """Keep the cached connection and its selected folder in one transaction."""
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


class IMAPBackend(DAVMixin, MailBackend):
    """Mail operations over IMAP4/SMTP plus optional CalDAV/CardDAV."""

    kind = "imap"

    def __init__(self, account: MailAccount) -> None:
        super().__init__(account.email)
        self.account = account
        self._imap: Optional[imaplib.IMAP4] = None
        self._lock = threading.RLock()
        self._selected: Optional[str] = None
        self._selected_readonly = True
        self._folders: Optional[List[MailFolder]] = None
        self._delimiter = "/"
        # Set when the account signs in with XOAUTH2 instead of a password.
        self._oauth = OAuthTokenSource(account) if account.uses_oauth else None

    # ------------------------------------------------------------------
    # connection
    # ------------------------------------------------------------------
    @property
    def _imap_host(self) -> str:
        return str(self.account.get("imap_host") or "")

    @property
    def _imap_port(self) -> int:
        return int(self.account.get("imap_port") or 993)

    @property
    def _imap_username(self) -> str:
        return str(self.account.get("imap_username") or self.email)

    def _connect(self) -> imaplib.IMAP4:
        with self._lock:
            if self._imap is not None:
                try:
                    self._imap.noop()
                    return self._imap
                except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError):
                    self._drop_connection()

            host, port = self._imap_host, self._imap_port
            if not host:
                raise MailBackendError(f"{self.email}: no imap_host configured")
            password = self.account.password
            if self._oauth is None and not password:
                raise MailBackendError(f"{self.email}: no IMAP password configured")

            try:
                if self.account.get("imap_ssl", True):
                    conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(
                        host, port, timeout=60, ssl_context=ssl.create_default_context()
                    )
                else:
                    conn = imaplib.IMAP4(host, port, timeout=60)
                    try:
                        conn.starttls(ssl.create_default_context())
                    except (imaplib.IMAP4.error, imaplib.IMAP4.abort):
                        _log.warning(
                            "IMAP STARTTLS unavailable on %s:%s — continuing in the "
                            "clear because imap_ssl=false was requested", host, port,
                        )
            except (OSError, ssl.SSLError, socket.timeout) as exc:
                raise MailBackendError(
                    f"cannot reach IMAP {host}:{port} for {self.email}: {exc}"
                ) from exc

            if self._oauth is not None:
                self._imap_xoauth2(conn, host)
            else:
                try:
                    conn.login(self._imap_username, password)
                except imaplib.IMAP4.error as exc:
                    try:
                        conn.logout()
                    except Exception:
                        pass
                    raise MailBackendError(
                        f"IMAP login failed for {self.email} on {host}: {exc}. "
                        f"iCloud and Outlook require an app-specific password, not the "
                        f"account password."
                    ) from exc

            self._imap = conn
            self._selected = None
            return conn

    def _xoauth2_sign_in(
        self, user: str, attempt: Callable[[str], Any], rejected: type,
    ) -> None:
        """Run ``attempt`` with an XOAUTH2 response; renew a refused cached token once."""
        token, cached = self._oauth.access_token()
        try:
            attempt(xoauth2_string(user, token))
            return
        except rejected:
            if not cached:
                raise
        attempt(xoauth2_string(user, self._oauth.renew(token)))

    def _xoauth2_refused(self, protocol: str, where: str, user: str, exc: Exception) -> str:
        return (
            f"{protocol} XOAUTH2 sign-in failed for {self.email} on {where}: {exc}. "
            f"The server refused a freshly refreshed access token for user {user!r}: "
            f"check that the refresh token was granted the https://mail.google.com/ "
            f"scope for this address, or sign in again with {sign_in_hint(self.email)}."
        )

    def _imap_xoauth2(self, conn: imaplib.IMAP4, host: str) -> None:
        def attempt(sasl: str) -> None:
            try:
                # An error challenge (non-empty) must be answered with an empty line.
                conn.authenticate("XOAUTH2", lambda challenge: b"" if challenge else sasl.encode())
            except imaplib.IMAP4.abort as exc:
                raise MailBackendError(
                    f"IMAP {host} closed the connection during XOAUTH2 sign-in "
                    f"for {self.email}: {exc}"
                ) from exc

        try:
            self._xoauth2_sign_in(self._imap_username, attempt, imaplib.IMAP4.error)
        except (imaplib.IMAP4.error, MailBackendError) as exc:
            try:
                conn.logout()
            except Exception:
                pass
            if isinstance(exc, MailBackendError):
                raise
            raise MailBackendError(
                self._xoauth2_refused("IMAP", host, self._imap_username, exc)
            ) from exc

    def _drop_connection(self) -> None:
        conn, self._imap = self._imap, None
        self._selected = None
        if conn is None:
            return
        for closer in (conn.logout, conn.shutdown):
            try:
                closer()
                return
            except Exception:
                continue

    def close(self) -> None:
        with self._lock:
            dav_client = getattr(self, "_dav_http", None)
            if dav_client is not None:
                try:
                    dav_client.close()
                finally:
                    self._dav_http = None
            self._drop_connection()

    # ------------------------------------------------------------------
    # low-level helpers
    # ------------------------------------------------------------------
    @_mailbox_operation
    def _uid_cmd(self, command: str, *args: str) -> List[Any]:
        """Run ``UID <command>``, raising MailBackendError on a NO/BAD reply."""
        conn = self._connect()
        try:
            typ, data = conn.uid(command, *args)
        except (imaplib.IMAP4.abort, OSError) as exc:
            self._drop_connection()
            raise MailBackendError(
                f"IMAP connection lost during UID {command}: {exc}"
            ) from exc
        except imaplib.IMAP4.error as exc:
            raise MailBackendError(f"IMAP UID {command} failed: {exc}") from exc
        if typ != "OK":
            raise MailBackendError(f"IMAP UID {command} returned {typ}: {data!r}")
        return list(data or [])

    def _expunge(self, uid: str) -> None:
        """Remove only this UID; ordinary EXPUNGE can destroy unrelated mail."""
        try:
            self._uid_cmd("EXPUNGE", uid)
        except MailBackendError as exc:
            raise MailBackendError(
                f"UID {uid} was marked Deleted but targeted expunge failed; "
                f"it may remain in the folder. No global EXPUNGE was attempted: {exc}"
            ) from exc

    @_mailbox_operation
    def _select(self, native: str, readonly: bool = True) -> Tuple[int, str]:
        """SELECT/EXAMINE ``native`` and return ``(message_count, uidvalidity)``."""
        conn = self._connect()
        with self._lock:
            try:
                typ, data = conn.select(_quote_mailbox(native), readonly=readonly)
            except (imaplib.IMAP4.abort, OSError) as exc:
                self._drop_connection()
                raise MailBackendError(
                    f"IMAP connection lost selecting {native!r}: {exc}"
                ) from exc
            except imaplib.IMAP4.error as exc:
                raise MailBackendError(f"cannot select folder {native!r}: {exc}") from exc
            if typ != "OK":
                raise MailBackendError(
                    f"cannot select folder {native!r}: {self._first(data)}"
                )
            self._selected = native
            self._selected_readonly = readonly
            count = 0
            try:
                count = int(self._first(data) or 0)
            except (TypeError, ValueError):
                count = 0
            uidvalidity = ""
            raw = conn.untagged_responses.get("UIDVALIDITY")
            if raw:
                uidvalidity = self._first(raw) or ""
            return count, uidvalidity

    @staticmethod
    def _first(data: Sequence[Any]) -> str:
        for item in data or ():
            if isinstance(item, bytes):
                return item.decode("utf-8", "replace")
            if isinstance(item, str):
                return item
        return ""

    # ------------------------------------------------------------------
    # folders
    # ------------------------------------------------------------------
    @_mailbox_operation
    def _load_folders(self, refresh: bool = False) -> List[MailFolder]:
        if self._folders is not None and not refresh:
            return self._folders

        conn = self._connect()
        try:
            typ, data = conn.list()
        except (imaplib.IMAP4.abort, OSError) as exc:
            self._drop_connection()
            raise MailBackendError(f"IMAP LIST failed for {self.email}: {exc}") from exc
        except imaplib.IMAP4.error as exc:
            raise MailBackendError(f"IMAP LIST failed for {self.email}: {exc}") from exc
        if typ != "OK":
            raise MailBackendError(f"IMAP LIST returned {typ} for {self.email}")

        folders: List[MailFolder] = []
        taken: set[str] = set()
        pending: List[Tuple[str, List[str]]] = []

        for line in data or ():
            if not isinstance(line, bytes):
                continue
            match = _LIST_RE.match(line.strip())
            if not match:
                continue
            attrs = [
                a.decode("ascii", "replace").lower()
                for a in match.group("attrs").split()
            ]
            if "\\noselect" in attrs:
                continue
            delim_raw = match.group("delim").decode("ascii", "replace")
            if delim_raw != "NIL":
                self._delimiter = delim_raw.strip('"') or "/"
            name_raw = match.group("name").decode("utf-8", "replace").strip()
            native = name_raw.strip('"')
            if not native:
                continue
            pending.append((native, attrs))

        # First pass: honour \Special-Use attributes.
        for native, attrs in pending:
            alias = None
            for attr in attrs:
                candidate = _SPECIAL_USE.get(attr)
                if candidate and candidate not in taken:
                    alias = candidate
                    break
            if native.upper() == "INBOX" and "inbox" not in taken:
                alias = "inbox"
            if alias:
                taken.add(alias)
            folders.append(
                MailFolder(
                    id=native,
                    name=native.split(self._delimiter)[-1],
                    path=native,
                    well_known=alias,
                )
            )

        # Second pass: name heuristics for anything still unclaimed.
        for folder in folders:
            if folder.well_known:
                continue
            lowered = folder.path.lower()
            leaf = folder.name.lower()
            for alias, hints in _NAME_HINTS.items():
                if alias in taken:
                    continue
                if leaf in hints or lowered in hints:
                    folder.well_known = alias
                    taken.add(alias)
                    break

        self._folders = folders
        return folders

    def _native_folder(self, token: str) -> str:
        """Map a well-known alias or literal path to the server's folder name."""
        token = (token or "inbox").strip()
        lowered = token.lower()
        folders = self._load_folders()

        if lowered in _NAME_HINTS:
            for folder in folders:
                if folder.well_known == lowered:
                    return folder.id
            if lowered == "inbox":
                return "INBOX"
            available = ", ".join(sorted(f.path for f in folders)) or "<none>"
            raise MailBackendError(
                f"{self.email}: no '{lowered}' folder on this IMAP server. "
                f"Available folders: {available}"
            )

        for folder in folders:
            if folder.path == token:
                return folder.id
        for folder in folders:
            if folder.path.lower() == lowered or folder.name.lower() == lowered:
                return folder.id
        # Accept a slash-delimited path on a server using another delimiter.
        if self._delimiter != "/" and "/" in token:
            translated = token.replace("/", self._delimiter)
            for folder in folders:
                if folder.path.lower() == translated.lower():
                    return folder.id
        available = ", ".join(sorted(f.path for f in folders)) or "<none>"
        raise MailBackendError(
            f"{self.email}: folder {token!r} not found. Available folders: {available}"
        )

    @_mailbox_operation
    def _status_counts(self, native: str) -> Tuple[Optional[int], Optional[int]]:
        """``(total, unread)`` via STATUS — one round trip, no SELECT churn."""
        conn = self._connect()
        try:
            typ, data = conn.status(_quote_mailbox(native), "(MESSAGES UNSEEN)")
        except (imaplib.IMAP4.abort, OSError) as exc:
            self._drop_connection()
            raise MailBackendError(f"IMAP STATUS failed for {native!r}: {exc}") from exc
        except imaplib.IMAP4.error as exc:
            _log.debug("STATUS unsupported/refused for %r: %s", native, exc)
            return None, None
        if typ != "OK":
            return None, None
        text = self._first(data)
        total = re.search(r"MESSAGES\s+(\d+)", text)
        unread = re.search(r"UNSEEN\s+(\d+)", text)
        return (
            int(total.group(1)) if total else None,
            int(unread.group(1)) if unread else None,
        )

    @_mailbox_operation
    def list_folders(self) -> List[MailFolder]:
        folders = self._load_folders(refresh=True)
        out: List[MailFolder] = []
        for folder in folders:
            try:
                total, unread = self._status_counts(folder.id)
            except MailBackendError as exc:
                _log.debug("skipping counts for %s: %s", folder.path, exc)
                total, unread = None, None
            out.append(
                MailFolder(
                    id=folder.id,
                    name=folder.name,
                    path=folder.path,
                    well_known=folder.well_known,
                    total_count=total,
                    unread_count=unread,
                )
            )
        return out

    # ------------------------------------------------------------------
    # search / fetch
    # ------------------------------------------------------------------
    def _uid_search(self, criteria: Sequence[str]) -> List[int]:
        args = list(criteria) or ["ALL"]
        data = self._uid_cmd("SEARCH", *args)
        uids: List[int] = []
        for chunk in data or ():
            if isinstance(chunk, bytes):
                uids.extend(int(x) for x in chunk.split() if x.isdigit())
        return sorted(uids)

    def _fetch(self, uids: Sequence[int], spec: str) -> Dict[int, Dict[str, Any]]:
        """UID FETCH ``spec`` for ``uids`` -> ``{uid: {"flags":…, "raw":…, …}}``."""
        if not uids:
            return {}
        uid_set = ",".join(str(u) for u in uids)
        data = self._uid_cmd("FETCH", uid_set, spec)
        parsed: Dict[int, Dict[str, Any]] = {}
        for item in data or ():
            if isinstance(item, tuple) and len(item) >= 2:
                head = item[0] if isinstance(item[0], bytes) else b""
                body = item[1] if isinstance(item[1], (bytes, bytearray)) else b""
                meta = self._parse_fetch_head(head)
                uid = meta.get("uid")
                if uid is None:
                    continue
                entry = parsed.setdefault(uid, {})
                entry.update(meta)
                entry["raw"] = bytes(body)
            elif isinstance(item, bytes):
                meta = self._parse_fetch_head(item)
                uid = meta.get("uid")
                if uid is not None:
                    parsed.setdefault(uid, {}).update(meta)
        return parsed

    @staticmethod
    def _parse_fetch_head(head: bytes) -> Dict[str, Any]:
        text = head.decode("utf-8", "replace")
        out: Dict[str, Any] = {}
        uid = re.search(r"UID\s+(\d+)", text)
        if uid:
            out["uid"] = int(uid.group(1))
        flags = re.search(r"FLAGS\s+\(([^)]*)\)", text)
        if flags:
            out["flags"] = flags.group(1).split()
        size = re.search(r"RFC822\.SIZE\s+(\d+)", text)
        if size:
            out["size"] = int(size.group(1))
        internal = re.search(r'INTERNALDATE\s+"([^"]+)"', text)
        if internal:
            out["internaldate"] = _internaldate_iso(internal.group(1))
        return out

    def _summary(
        self, uid: int, uidvalidity: str, folder_token: str, meta: Dict[str, Any],
    ) -> MailMessage:
        headers = email.message_from_bytes(meta.get("raw") or b"")
        flags = [f.lower() for f in meta.get("flags") or []]
        from_address, from_name = _sender(headers.get("From"))
        importance = headers.get("Importance") or headers.get("X-Priority")
        return MailMessage(
            id=f"{uidvalidity}:{uid}" if uidvalidity else str(uid),
            folder=folder_token,
            subject=_decode_header_value(headers.get("Subject")),
            from_address=from_address,
            from_name=from_name,
            to=_addresses(headers.get("To")),
            cc=_addresses(headers.get("Cc")),
            reply_to=_addresses(headers.get("Reply-To")),
            received=meta.get("internaldate") or _header_date(headers.get("Date")),
            sent=_header_date(headers.get("Date")),
            is_read="\\seen" in flags,
            is_flagged="\\flagged" in flags,
            flag_status=_flag_status(flags),
            has_attachments=self._headers_suggest_attachments(headers),
            importance=_decode_header_value(importance) or None,
            internet_message_id=(headers.get("Message-ID") or "").strip() or None,
            size=meta.get("size"),
        )

    @staticmethod
    def _headers_suggest_attachments(headers: Message) -> bool:
        """Best-effort attachment flag from the summary headers alone.

        The summary FETCH deliberately pulls headers only (a full body per
        row would make listing a folder unusable), so this reads the
        top-level Content-Type: every multipart shape that can hold a
        non-body part, plus a single-part message whose own body is a file.
        multipart/alternative (text+html) is the common false positive and is
        excluded. Callers that need certainty must open the message -
        get_message/list_attachments parse the real MIME tree.
        """
        content_type = (headers.get("Content-Type") or "").lower().strip()
        if not content_type:
            return False
        main_type = content_type.split(";", 1)[0].strip()
        if main_type.startswith("multipart/"):
            return main_type in (
                "multipart/mixed",
                "multipart/related",
                "multipart/signed",
                "multipart/encrypted",
                "multipart/report",
            )
        # A single-part message that is not text is itself the attachment.
        return not main_type.startswith("text/") and "/" in main_type

    def _page(
        self, folder_token: str, uids: Sequence[int], uidvalidity: str,
        limit: int, offset: int,
    ) -> List[MailMessage]:
        newest_first = list(reversed(list(uids)))
        window = newest_first[offset : offset + max(1, limit)]
        if not window:
            return []
        fetched = self._fetch(window, _FETCH_SUMMARY)
        out: List[MailMessage] = []
        for uid in window:
            meta = fetched.get(uid)
            if meta is None:
                continue
            out.append(self._summary(uid, uidvalidity, folder_token, meta))
        return out

    # ------------------------------------------------------------------
    # MailBackend implementation
    # ------------------------------------------------------------------
    @_mailbox_operation
    def test_connection(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {
            "connected": False,
            "backend": self.kind,
            "email": self.email,
            "imap": f"{self._imap_host}:{self._imap_port}",
        }
        try:
            folders = self._load_folders(refresh=True)
            total, _ = self._select(self._native_folder("inbox"), readonly=True)
            info.update(connected=True, folder_count=len(folders), inbox_total=total)
        except (MailBackendError, imaplib.IMAP4.error, OSError) as exc:
            info["error"] = str(exc)
        return info

    @_mailbox_operation
    def list_messages(
        self,
        folder: str = "inbox",
        limit: int = 25,
        offset: int = 0,
        unread_only: bool = False,
    ) -> List[MailMessage]:
        native = self._native_folder(folder)
        _, uidvalidity = self._select(native, readonly=True)
        uids = self._uid_search(["UNSEEN"] if unread_only else ["ALL"])
        return self._page(folder, uids, uidvalidity, limit, offset)

    @_mailbox_operation
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
    ) -> List[MailMessage]:
        criteria: List[str] = []
        if unread_only:
            criteria.append("UNSEEN")
        if flag_status:
            if flag_status not in _FLAG_SEARCH_KEYS:
                raise MailBackendError(
                    f"flag_status={flag_status!r}: IMAP has only the \\Flagged flag, "
                    f"so it can search {' or '.join(_FLAG_SEARCH_KEYS)} only"
                )
            criteria.append(_FLAG_SEARCH_KEYS[flag_status])
        if from_address:
            criteria += ["FROM", _imap_literal(from_address)]
        if subject:
            criteria += ["SUBJECT", _imap_literal(subject)]
        if since:
            criteria += ["SINCE", _imap_date(since)]
        if before:
            criteria += ["BEFORE", _imap_date(before)]
        if query:
            criteria += ["TEXT", _imap_literal(query)]
        if not criteria:
            criteria = ["ALL"]

        native = self._native_folder(folder)
        _, uidvalidity = self._select(native, readonly=True)
        uids = self._uid_search(criteria)
        results = self._page(folder, uids, uidvalidity, limit, 0)
        if has_attachments is not None:
            results = [m for m in results if m.has_attachments == has_attachments]
        return results

    @_mailbox_operation
    def _fetch_full(
        self, message_id: str, folder: str, writable: bool = False,
    ) -> Tuple[str, str, Dict[str, Any]]:
        """SELECT ``folder``, fetch the whole message, return ``(uidvalidity, uid, meta)``.

        ``meta["raw"]`` holds the full RFC-822 bytes (``BODY.PEEK[]`` — reading
        never sets ``\\Seen``).
        """
        want_validity, uid = _split_message_id(message_id)
        native = self._native_folder(folder)
        _, uidvalidity = self._select(native, readonly=not writable)
        if want_validity and uidvalidity and want_validity != uidvalidity:
            raise MailBackendError(
                f"message id {message_id!r} is stale: folder {folder!r} was rebuilt "
                f"(UIDVALIDITY {uidvalidity}); list the folder again"
            )
        fetched = self._fetch(
            [int(uid)], "(UID FLAGS RFC822.SIZE INTERNALDATE BODY.PEEK[])"
        )
        meta = fetched.get(int(uid))
        if not meta or not meta.get("raw"):
            raise MailBackendError(
                f"message {message_id!r} not found in folder {folder!r} for {self.email}"
            )
        return uidvalidity, uid, meta

    @_mailbox_operation
    def get_message(
        self,
        message_id: str,
        folder: str = "inbox",
        body_format: str = "markdown",
    ) -> MailMessage:
        uidvalidity, uid, meta = self._fetch_full(message_id, folder)
        parsed = email.message_from_bytes(meta["raw"])
        html_body, text_body, attachments = self._walk_parts(parsed)
        body, actual = render_body(html_body, text_body, body_format)

        flags = [f.lower() for f in meta.get("flags") or []]
        from_address, from_name = _sender(parsed.get("From"))
        importance = parsed.get("Importance") or parsed.get("X-Priority")
        return MailMessage(
            id=f"{uidvalidity}:{uid}" if uidvalidity else str(uid),
            folder=folder,
            subject=_decode_header_value(parsed.get("Subject")),
            from_address=from_address,
            from_name=from_name,
            to=_addresses(parsed.get("To")),
            cc=_addresses(parsed.get("Cc")),
            bcc=_addresses(parsed.get("Bcc")),
            reply_to=_addresses(parsed.get("Reply-To")),
            received=meta.get("internaldate") or _header_date(parsed.get("Date")),
            sent=_header_date(parsed.get("Date")),
            is_read="\\seen" in flags,
            is_flagged="\\flagged" in flags,
            flag_status=_flag_status(flags),
            has_attachments=bool(attachments),
            importance=_decode_header_value(importance) or None,
            internet_message_id=(parsed.get("Message-ID") or "").strip() or None,
            preview=preview_text(text_body or body),
            body=body,
            body_format=actual,
            attachments=attachments,
            size=meta.get("size"),
        )

    @staticmethod
    def _leaf_parts(msg: Message) -> List[Tuple[str, Message, bool]]:
        """Leaf MIME parts as ``(part_id, part, is_attachment)`` in walk order.

        ``part_id`` is the part's position in the message's MIME tree, which is
        what attachment ids are made of: stable for as long as the message
        exists (IMAP messages are immutable) and meaningful without a server
        round-trip.
        """
        out: List[Tuple[str, Message, bool]] = []
        enclosed: set[int] = set()
        for index, part in enumerate(msg.walk()):
            if id(part) in enclosed:
                continue
            disposition = (part.get("Content-Disposition") or "").lower()
            maintype = part.get_content_maintype()
            # Inline text and multipart containers still belong to the body,
            # even when a related root carries a Content-ID. A filename or
            # explicit attachment disposition remains authoritative.
            inline_file = (
                maintype != "multipart"
                and part.get_content_type() not in ("text/plain", "text/html")
                and (disposition.startswith("inline") or bool(part.get("Content-ID")))
            )
            is_attachment = (
                bool(part.get_filename()) or disposition.startswith("attachment")
                or maintype == "message" or inline_file
            )
            if is_attachment:
                enclosed.update(id(child) for child in part.walk() if child is not part)
            elif part.get_content_maintype() == "multipart":
                continue
            out.append((f"part-{index}", part, is_attachment))
        return out

    @staticmethod
    def _part_bytes(part: Message) -> bytes:
        if part.get_content_type() == "message/rfc822":
            payload = part.get_payload()
            if isinstance(payload, list):
                return b"\r\n".join(item.as_bytes(policy=email.policy.SMTP) for item in payload)
        return part.get_payload(decode=True) or b""

    @staticmethod
    def _attachment_info(part_id: str, part: Message) -> MailAttachmentInfo:
        disposition = (part.get("Content-Disposition") or "").lower()
        content_type = part.get_content_type()
        payload = IMAPBackend._part_bytes(part)
        content_id = (part.get("Content-ID") or "").strip().strip("<>") or None
        return MailAttachmentInfo(
            name=(
                _decode_header_value(part.get_filename())
                or f"part.{content_type.split('/')[-1]}"
            ),
            content_type=content_type,
            size=len(payload),
            attachment_id=part_id,
            is_inline="inline" in disposition,
            content_id=content_id,
        )

    @classmethod
    def _walk_parts(cls, msg: Message) -> Tuple[str, str, List[MailAttachmentInfo]]:
        """Split a parsed message into ``(html, text, attachments)``."""
        html_parts: List[str] = []
        text_parts: List[str] = []
        attachments: List[MailAttachmentInfo] = []

        for part_id, part, is_attachment in cls._leaf_parts(msg):
            if is_attachment:
                attachments.append(cls._attachment_info(part_id, part))
                continue

            content_type = part.get_content_type()
            try:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, "replace")
            except (LookupError, UnicodeDecodeError, TypeError):
                text = ""
            if content_type == "text/html":
                html_parts.append(text)
            elif content_type == "text/plain":
                text_parts.append(text)

        return "\n".join(html_parts), "\n".join(text_parts), attachments

    # ------------------------------------------------------------------
    # attachments
    # ------------------------------------------------------------------
    @_mailbox_operation
    def list_attachments(
        self,
        message_id: str,
        folder: str = "inbox",
        include_inline: bool = True,
    ) -> List[MailAttachmentInfo]:
        _validity, _uid, meta = self._fetch_full(message_id, folder)
        parsed = email.message_from_bytes(meta["raw"])
        items = [
            self._attachment_info(part_id, part)
            for part_id, part, is_attachment in self._leaf_parts(parsed)
            if is_attachment
        ]
        if not include_inline:
            items = [i for i in items if not i.is_inline]
        return items

    @_mailbox_operation
    def get_attachment(
        self,
        message_id: str,
        folder: str = "inbox",
        attachment_id: Optional[str] = None,
        attachment_name: Optional[str] = None,
    ) -> MailAttachmentContent:
        _validity, _uid, meta = self._fetch_full(message_id, folder)
        parsed = email.message_from_bytes(meta["raw"])
        parts = {
            part_id: part
            for part_id, part, is_attachment in self._leaf_parts(parsed)
            if is_attachment
        }
        infos = [self._attachment_info(pid, part) for pid, part in parts.items()]
        chosen = select_attachment(infos, attachment_id, attachment_name)
        part = parts[str(chosen.attachment_id)]
        return MailAttachmentContent(
            name=chosen.name,
            content=self._part_bytes(part),
            content_type=chosen.content_type,
            attachment_id=chosen.attachment_id,
            is_inline=chosen.is_inline,
            content_id=chosen.content_id,
        )

    @_mailbox_operation
    def get_mime(self, message_id: str, folder: str = "inbox") -> bytes:
        _validity, _uid, meta = self._fetch_full(message_id, folder)
        return bytes(meta["raw"])

    def _require_draft_folder(self, folder: str, operation: str) -> str:
        """IMAP cannot edit a stored message; only drafts can be replaced."""
        native = self._native_folder(folder)
        try:
            drafts = self._native_folder("drafts")
        except MailBackendError:
            drafts = None
        if drafts is None or native.lower() != drafts.lower():
            raise self._unsupported(
                operation,
                "IMAP messages are immutable once stored, so this only works "
                f"in the drafts folder (folder={folder!r} resolved to "
                f"{native!r}). Attach the file while sending instead.",
            )
        return native

    @_mailbox_operation
    def _rewrite_draft(
        self, message_id: str, folder: str, mutate: Any, operation: str,
    ) -> Tuple[str, List[MailAttachmentInfo], Dict[str, Any]]:
        """Replace a draft, returning its new identity, attachments and cleanup outcome.

        IMAP has no "modify message" verb: the new version is APPENDed first
        and the old UID is only deleted once the replacement's identity is
        known. A unique revision header allows safe lookup without APPENDUID.
        """
        native = self._require_draft_folder(folder, operation)
        validity, uid, meta = self._fetch_full(message_id, folder, writable=True)
        flags = {flag.lower() for flag in meta.get("flags") or []}
        if "\\draft" not in flags or "\\deleted" in flags:
            raise MailBackendError(f"{operation} requires an undeleted message flagged as a draft")
        try:
            draft = email.message_from_bytes(meta["raw"], policy=email.policy.default)
            mutate(draft)
            revision = uuid.uuid4().hex
            if "X-EWS-MCP-Draft-Revision" in draft:
                del draft["X-EWS-MCP-Draft-Revision"]
            draft["X-EWS-MCP-Draft-Revision"] = revision
            payload = draft.as_bytes()
        except (MailBackendError, ValueError, TypeError, KeyError) as exc:
            raise MailBackendError(
                f"{self.email}: cannot rewrite draft {message_id!r}: {exc}"
            ) from exc

        details: Dict[str, Any] = {
            "draft_id": None, "previous_id": message_id, "folder": folder,
            "stage": "append", "sent": False, "revision": revision,
            "original_removed": False, "cleanup_attempted": False,
        }
        try:
            new_uid = self._append_raw(native, payload)
            details["stage"] = "identify"
            _, uidvalidity = self._select(native, readonly=False)
            if not validity or uidvalidity != validity:
                raise MailBackendError(
                    "Draft was appended but folder UIDVALIDITY changed; original "
                    "was not deleted. List the drafts folder before further changes."
                )
            if not new_uid:
                matches = self._uid_search([
                    "HEADER", "X-EWS-MCP-Draft-Revision", _imap_literal(revision),
                ])
                if len(matches) == 1 and str(matches[0]) != uid:
                    new_uid = str(matches[0])
            if not new_uid or new_uid == uid:
                raise MailBackendError(
                    "Draft was appended but its new UID could not be identified "
                    "uniquely; original was not deleted. List the drafts folder "
                    "before retrying to avoid duplicate drafts."
                )
        except (MailBackendError, imaplib.IMAP4.error, OSError) as exc:
            raise MailBackendError(str(exc), details=details) from exc
        new_id = f"{uidvalidity}:{new_uid}"
        cleanup: Dict[str, Any] = {
            "original_removed": False,
            "attachments_verified": False,
        }
        try:
            attachments = self.list_attachments(new_id, folder)
            cleanup["attachments_verified"] = True
        except (MailBackendError, imaplib.IMAP4.error, OSError) as exc:
            # The APPEND succeeded and its identity is known. Do not discard
            # that identity or invite a duplicate rewrite because a read failed.
            attachments = [
                self._attachment_info(part_id, part)
                for part_id, part, is_attachment in self._leaf_parts(draft)
                if is_attachment
            ]
            cleanup["warnings"] = [
                f"Replacement draft {new_id} was saved, but rereading it failed: {exc}. "
                "Attachment metadata is from the appended MIME. Original was not "
                "removed; use the replacement ID and do not repeat the mutation."
            ]
            return new_id, attachments, cleanup
        try:
            # Relisting selected the folder read-only; restore writable state
            # and revalidate its generation before touching the old UID.
            self._resolve_for_write(f"{validity}:{uid}", native)
            self._store(uid, "+FLAGS", "(\\Deleted)")
            self._expunge(uid)
            cleanup["original_removed"] = True
        except (MailBackendError, imaplib.IMAP4.error, OSError) as exc:
            cleanup["warnings"] = [
                f"Replacement draft {new_id} was saved, but cleanup of original "
                f"{message_id} was not confirmed: {exc}. Use the replacement ID "
                "and do not repeat the mutation; the original may remain."
            ]
        return new_id, attachments, cleanup

    @staticmethod
    def _response_code(conn: imaplib.IMAP4, code: str, data: Sequence[Any]) -> str:
        """Read UIDPLUS codes from tagged text or imaplib's separate cache."""
        for chunk in data or ():
            text = chunk.decode("ascii", "replace") if isinstance(chunk, bytes) else str(chunk)
            match = re.search(rf"\b{code}\s+([0-9 ]+)", text, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        _, values = conn.response(code)
        return IMAPBackend._first(values or [])

    @_mailbox_operation
    def _append_raw(self, native: str, payload: bytes) -> Optional[str]:
        """APPEND ``payload`` to ``native`` as a draft; return its new UID."""
        conn = self._connect()
        conn.response("APPENDUID")  # discard a prior APPEND's response code
        try:
            typ, data = conn.append(
                _quote_mailbox(native),
                r"(\Seen \Draft)",
                imaplib.Time2Internaldate(datetime.now(tz=timezone.utc).timestamp()),
                payload,
            )
        except (imaplib.IMAP4.error, OSError) as exc:
            raise MailBackendError(
                f"{self.email}: APPEND to {native} failed: {exc}"
            ) from exc
        if typ != "OK":
            raise MailBackendError(
                f"{self.email}: APPEND to {native} returned {typ}: {data!r}"
            )
        code = self._response_code(conn, "APPENDUID", data)
        match = re.fullmatch(r"\d+\s+(\d+)", code)
        return match.group(1) if match else None

    @_mailbox_operation
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
        maintype, _, subtype = (
            content_type or "application/octet-stream"
        ).partition("/")

        def mutate(draft: EmailMessage) -> None:
            kwargs: Dict[str, Any] = {
                "maintype": maintype or "application",
                "subtype": subtype or "octet-stream",
                "filename": name,
            }
            if is_inline:
                kwargs["disposition"] = "inline"
                if content_id:
                    kwargs["cid"] = f"<{content_id.strip('<>')}>"
            draft.add_attachment(content, **kwargs)

        new_id, attachments, cleanup = self._rewrite_draft(
            message_id, folder, mutate, "add attachments"
        )
        result = {
            "added": True,
            "backend": self.kind,
            "id": new_id,
            "previous_id": message_id,
            "folder": folder,
            "attachment_name": name,
            "attachment_size": len(content),
            "attachment_id": attachments[-1].attachment_id if attachments else None,
            "attachments": [item.to_dict() for item in attachments],
            "is_inline": bool(is_inline),
            "note": "IMAP replaces the draft, so the message id changed",
            **cleanup,
        }
        # Report the cid without the angle brackets the MIME header needs, so
        # the caller can paste it straight into <img src="cid:...">.
        if is_inline and content_id:
            result["content_id"] = content_id.strip("<>")
        return result

    @_mailbox_operation
    def delete_attachment(
        self,
        message_id: str,
        folder: str = "drafts",
        attachment_id: Optional[str] = None,
        attachment_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        removed: Dict[str, Any] = {}

        def mutate(draft: EmailMessage) -> None:
            parts = {
                part_id: part
                for part_id, part, is_attachment in self._leaf_parts(draft)
                if is_attachment
            }
            infos = [self._attachment_info(pid, part) for pid, part in parts.items()]
            chosen = select_attachment(infos, attachment_id, attachment_name)
            target = parts[str(chosen.attachment_id)]
            if not _detach_part(draft, target):
                raise MailBackendError(
                    f"attachment {chosen.name!r} is the whole message body and "
                    f"cannot be removed on its own"
                )
            removed["name"] = chosen.name
            removed["attachment_id"] = chosen.attachment_id

        new_id, attachments, cleanup = self._rewrite_draft(
            message_id, folder, mutate, "delete attachments"
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
            "note": "IMAP replaces the draft, so the message id changed",
            **cleanup,
        }

    # ------------------------------------------------------------------
    # mutations
    # ------------------------------------------------------------------
    def _store(self, uid: str, command: str, flags: str) -> None:
        self._uid_cmd("STORE", uid, command, flags)

    def _resolve_for_write(self, message_id: str, folder: str) -> Tuple[str, str]:
        """SELECT ``folder`` writable and return ``(native_folder, uid)``."""
        want_validity, uid = _split_message_id(message_id)
        native = self._native_folder(folder)
        _, uidvalidity = self._select(native, readonly=False)
        if want_validity and uidvalidity and want_validity != uidvalidity:
            raise MailBackendError(
                f"message id {message_id!r} is stale: folder {folder!r} was rebuilt "
                f"(UIDVALIDITY {uidvalidity}); list the folder again"
            )
        return native, uid

    @_mailbox_operation
    def mark_message(
        self,
        message_id: str,
        folder: str = "inbox",
        is_read: Optional[bool] = None,
        is_flagged: Optional[bool] = None,
    ) -> Dict[str, Any]:
        if is_read is None and is_flagged is None:
            return {"changed": False, "backend": self.kind, "id": message_id, "folder": folder}
        _, uid = self._resolve_for_write(message_id, folder)
        changed: Dict[str, Any] = {}
        if is_read is not None:
            self._store(uid, "+FLAGS" if is_read else "-FLAGS", "(\\Seen)")
            changed["is_read"] = is_read
        if is_flagged is not None:
            self._store(uid, "+FLAGS" if is_flagged else "-FLAGS", "(\\Flagged)")
            changed["is_flagged"] = is_flagged
        return {
            "changed": True,
            "backend": self.kind,
            "id": message_id,
            "folder": folder,
            **changed,
        }

    @_mailbox_operation
    def move_message(
        self, message_id: str, destination: str, folder: str = "inbox",
    ) -> Dict[str, Any]:
        _, uid = self._resolve_for_write(message_id, folder)
        target = self._native_folder(destination)
        conn = self._connect()
        capabilities = {
            value.decode("ascii", "replace").upper() if isinstance(value, bytes) else value.upper()
            for value in conn.capabilities
        }
        conn.response("COPYUID")
        moved_natively = "MOVE" in capabilities
        response = self._uid_cmd(
            "MOVE" if moved_natively else "COPY", uid, _quote_mailbox(target),
        )
        code = self._response_code(conn, "COPYUID", response)
        match = re.fullmatch(r"(\d+)\s+(\d+)\s+(\d+)", code)
        new_id = None
        if match and match.group(2) == uid:
            new_id = f"{match.group(1)}:{match.group(3)}"
        if not moved_natively:
            if new_id is None:
                raise MailBackendError(
                    "Message was copied but its destination UID could not be identified; "
                    "original was not deleted. List the destination before retrying."
                )
            self._store(uid, "+FLAGS", "(\\Deleted)")
            self._expunge(uid)
        self._folders = None
        return {
            "moved": True,
            "backend": self.kind,
            "id": new_id,
            "previous_id": message_id,
            "folder": destination,
            "source": folder,
            "destination": destination,
            "note": (
                "IMAP assigned a new UID in the destination folder"
                if new_id else
                "Message moved, but the server did not return its destination UID; "
                "list the destination folder before further operations. Do not repeat the move."
            ),
        }

    @_mailbox_operation
    def delete_message(
        self, message_id: str, folder: str = "inbox", hard_delete: bool = False,
    ) -> Dict[str, Any]:
        _, uid = self._resolve_for_write(message_id, folder)
        if hard_delete:
            self._store(uid, "+FLAGS", "(\\Deleted)")
            self._expunge(uid)
            return {
                "deleted": True,
                "hard_delete": True,
                "backend": self.kind,
                "id": message_id,
                "folder": folder,
            }

        trash = self._native_folder("trash")
        if trash == self._native_folder(folder):
            self._store(uid, "+FLAGS", "(\\Deleted)")
            self._expunge(uid)
            return {
                "deleted": True,
                "hard_delete": True,
                "backend": self.kind,
                "id": message_id,
                "folder": folder,
                "note": "message was already in Trash — expunged",
            }
        moved = self.move_message(message_id, "trash", folder)
        return {
            "deleted": True,
            "hard_delete": False,
            "backend": self.kind,
            "id": moved["id"],
            "previous_id": message_id,
            "folder": moved["folder"],
            "note": moved["note"],
            "destination": trash,
        }

    # ------------------------------------------------------------------
    # sending
    # ------------------------------------------------------------------
    @_mailbox_operation
    def send_draft(
        self, draft_id: str, folder: str = "drafts", dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Submit stored MIME, removing only private blind-copy headers."""
        native = self._require_draft_folder(folder, "send_draft")
        validity, uid, meta = self._fetch_full(draft_id, folder, writable=not dry_run)
        flags = {flag.lower() for flag in meta.get("flags") or []}
        if "\\draft" not in flags or "\\deleted" in flags:
            raise MailBackendError("send_draft requires an undeleted message flagged as a draft")

        raw = meta["raw"]
        parsed = email.message_from_bytes(raw, policy=email.policy.default)
        recipients = {
            name: [
                address
                for header in parsed.get_all(name, [])
                for address in _addresses(str(header))
            ]
            for name in ("To", "Cc", "Bcc")
        }
        envelope = list(dict.fromkeys(
            recipients["To"] + recipients["Cc"] + recipients["Bcc"]
        ))
        if not envelope:
            raise MailBackendError("no recipients in stored draft")
        subject = _decode_header_value(parsed.get("Subject"))
        result: Dict[str, Any] = {
            "draft_id": draft_id,
            "backend": self.kind,
            "sent": False,
            "accepted_by_mail_server": False,
            "delivery_confirmed": False,
            "subject": subject,
        }
        if dry_run:
            html, text, attachments = self._walk_parts(parsed)
            body, _ = render_body(html, text, "text")
            result.update(
                dry_run=True,
                would_send_to=recipients["To"],
                would_cc=recipients["Cc"],
                would_bcc=recipients["Bcc"],
                body_preview=preview_text(text or body),
                attachments_count=len(attachments),
            )
            return result

        # Do not reserialize MIME: that can refold signed headers and alter
        # attachment encodings. SMTP.sendmail(bytes) does not fix line endings,
        # while draft rewrites can produce LF-only MIME. Normalize before
        # identifying complete blind-copy fields and their continuations.
        wire = re.sub(rb"(?<!\r)\n|\r(?!\n)", b"\r\n", raw)
        separator = re.search(rb"\r\n\r\n", wire)
        if separator is None:
            raise MailBackendError("stored draft has no MIME header/body separator")
        headers = re.sub(
            rb"(?im)^(?:Bcc|Resent-Bcc|X-EWS-MCP-Draft-Revision):[^\r\n]*(?:\r?\n[ \t][^\r\n]*)*\r?\n",
            b"",
            wire[:separator.end()],
        )
        outgoing = headers + wire[separator.end():]
        refused = self._smtp_send(outgoing, envelope)
        accepted = [address for address in envelope if address not in refused]
        if not accepted:
            raise MailBackendError("SMTP refused every stored draft recipient")

        warnings: List[str] = []
        result.update(
            sent=not bool(refused),
            accepted_by_mail_server=True,
            to=recipients["To"],
            cc=recipients["Cc"],
            bcc=recipients["Bcc"],
            accepted_recipients=accepted,
            partial_failure=bool(refused),
            refused_recipients={
                address: {
                    "code": code,
                    "message": reason.decode("utf-8", "replace") if isinstance(reason, bytes) else str(reason),
                }
                for address, (code, reason) in refused.items()
            },
            saved_to_sent=None,
            draft_removed=False,
        )
        if refused:
            warnings.append(
                "SMTP accepted only some recipients. Do not resend this draft to "
                "accepted recipients; retry only the refused recipients."
            )
        try:
            # The private Sent copy retains the stored Bcc recipient list.
            result["saved_to_sent"] = self._append_to_sent(raw)
            if result["saved_to_sent"] is None:
                warnings.append("SMTP accepted the message, but its Sent copy could not be saved.")
        except (MailBackendError, imaplib.IMAP4.error, OSError) as exc:
            warnings.append(f"SMTP accepted the message, but saving its Sent copy failed: {exc}")
        try:
            # SMTP may take long enough for IMAP to reconnect. Select again and
            # guard even bare UIDs using the validity observed before submission.
            self._resolve_for_write(f"{validity}:{uid}" if validity else uid, native)
            self._store(uid, "+FLAGS", "(\\Deleted)")
            # imaplib caches pre-login capabilities: UIDPLUS may be supported
            # without appearing there. Attempt the safe command directly.
            # NO/BAD is handled below; never fall back to global EXPUNGE.
            self._uid_cmd("EXPUNGE", uid)
            result["draft_removed"] = True
        except (MailBackendError, imaplib.IMAP4.error, OSError) as exc:
            warnings.append(
                f"SMTP accepted the message, but draft cleanup failed: {exc}. "
                "Do not resend the draft."
            )
        if warnings:
            result["warnings"] = warnings
        return result

    @_mailbox_operation
    def send_message(self, message: OutgoingMessage) -> Dict[str, Any]:
        recipients = [a for a in (message.to or []) if a]
        cc = [a for a in (message.cc or []) if a]
        bcc = [a for a in (message.bcc or []) if a]

        original: Optional[MailMessage] = None
        if message.reply_to_message_id:
            original = self.get_message(
                message.reply_to_message_id,
                message.reply_to_folder or "inbox",
                body_format="text",
            )
            if not recipients:
                recipients = [original.from_address] if original.from_address else []
            # Same split as create_reply_draft: the sender (plus the original
            # To on reply-all) go in To, the caller's Cc (plus the original Cc
            # on reply-all) in Cc. Nobody appears twice and Bcc recipients stay
            # blind. Reply-all never mails us; a plain reply keeps its sender
            # even when that is us.
            seen: set = set()
            blind: List[str] = []
            for address in bcc:
                if address.lower() not in seen:
                    blind.append(address)
                    seen.add(address.lower())
            to_sources, cc_sources = recipients, cc
            if message.reply_all:
                seen.add(self.email.lower())
                to_sources = recipients + list(original.to)
                cc_sources = cc + list(original.cc)
            to_all: List[str] = []
            cc_all: List[str] = []
            for target, addresses in ((to_all, to_sources), (cc_all, cc_sources)):
                for address in addresses:
                    if address and address.lower() not in seen:
                        target.append(address)
                        seen.add(address.lower())
            recipients, cc, bcc = to_all, cc_all, blind

        if not recipients:
            raise MailBackendError("no recipients: 'to' is required")

        subject = message.subject
        if original is not None and not subject:
            base = original.subject or ""
            subject = base if base.lower().startswith("re:") else f"Re: {base}".strip()

        html, _ = compose_body(message.body or "", message.body_format or "markdown")
        text_fallback = (
            message.body if (message.body_format or "markdown") != "html" else ""
        )

        outgoing = EmailMessage()
        outgoing["From"] = self.email
        outgoing["To"] = ", ".join(recipients)
        if cc:
            outgoing["Cc"] = ", ".join(cc)
        outgoing["Subject"] = subject or ""
        outgoing["Date"] = email.utils.formatdate(localtime=True)
        outgoing["Message-ID"] = email.utils.make_msgid(domain=self.email.split("@")[-1])
        if original is not None and original.internet_message_id:
            outgoing["In-Reply-To"] = original.internet_message_id
            outgoing["References"] = original.internet_message_id
        tracking_id = _stamp_tracking_header(outgoing, message.tracking_id)
        outgoing.set_content(text_fallback or re.sub(r"<[^>]+>", "", html))
        outgoing.add_alternative(html, subtype="html")
        self._add_files(outgoing, message.attachments)

        envelope = list(dict.fromkeys(recipients + cc + bcc))
        refused = self._smtp_send(outgoing, envelope)
        accepted = [address for address in envelope if address not in refused]
        if not accepted:
            raise MailBackendError("SMTP refused every recipient")
        result: Dict[str, Any] = {
            "sent": not bool(refused),
            "accepted_by_mail_server": True,
            "delivery_confirmed": False,
            "accepted_recipients": accepted,
            "partial_failure": bool(refused),
            "refused_recipients": {
                address: {
                    "code": code,
                    "message": reason.decode("utf-8", "replace") if isinstance(reason, bytes) else str(reason),
                }
                for address, (code, reason) in refused.items()
            },
            "backend": self.kind,
            "to": recipients,
            "cc": cc,
            "bcc": bcc,
            "subject": subject or "",
            "replied_to": message.reply_to_message_id,
            "saved_to_sent": None,
            "tracking_id": tracking_id,
        }
        warnings: List[str] = []
        if refused:
            warnings.append(
                "SMTP accepted only some recipients. Do not resend to accepted "
                "recipients; retry only the refused recipients."
            )
        try:
            result["saved_to_sent"] = self._append_to_sent(outgoing)
            if result["saved_to_sent"] is None:
                warnings.append("SMTP accepted the message, but its Sent copy could not be saved.")
        except (MailBackendError, imaplib.IMAP4.error, OSError) as exc:
            warnings.append(f"SMTP accepted the message, but saving its Sent copy failed: {exc}")
        if warnings:
            result["warnings"] = warnings
        return result

    @staticmethod
    def _replace_body_part(part: EmailMessage, content: str, subtype: str) -> None:
        retained = [
            (name, str(value)) for name, value in part.items()
            if name.lower() in ("content-id", "content-disposition", "content-location")
        ]
        part.set_content(content, subtype=subtype)
        for name, value in retained:
            part[name] = value

    @staticmethod
    def _add_files(outgoing: EmailMessage, attachments: Sequence[OutgoingAttachment]) -> None:
        for attachment in attachments:
            maintype, _, subtype = (
                attachment.content_type or "application/octet-stream"
            ).partition("/")
            kwargs: Dict[str, Any] = {
                "filename": attachment.name,
                "disposition": "inline" if attachment.is_inline else "attachment",
            }
            if attachment.content_id:
                kwargs["cid"] = f"<{attachment.content_id.strip('<>')}>"
            if (maintype.lower(), subtype.lower()) == ("message", "rfc822"):
                nested = email.message_from_bytes(
                    attachment.content, policy=email.policy.default,
                )
                outgoing.add_attachment(nested, **kwargs)
            else:
                outgoing.add_attachment(
                    attachment.content, maintype=maintype or "application",
                    subtype=subtype or "octet-stream", **kwargs,
                )

    def _build_draft(self, message: OutgoingMessage) -> EmailMessage:
        outgoing = EmailMessage()
        outgoing["From"] = self.email
        for name, addresses in (("To", message.to), ("Cc", message.cc), ("Bcc", message.bcc)):
            if addresses:
                outgoing[name] = ", ".join(addresses)
        outgoing["Subject"] = message.subject
        outgoing["Date"] = email.utils.formatdate(localtime=True)
        outgoing["Message-ID"] = email.utils.make_msgid(domain=self.email.split("@")[-1])
        _stamp_tracking_header(outgoing, message.tracking_id)
        html, _ = compose_body(message.body, message.body_format)
        text = message.body if message.body_format != "html" else unescape(re.sub(r"<[^>]+>", "", html))
        outgoing.set_content(text)
        outgoing.add_alternative(html, subtype="html")
        self._add_files(outgoing, message.attachments)
        return outgoing

    @_mailbox_operation
    def _save_draft(self, draft: EmailMessage, cleanup_on_failure: bool) -> Dict[str, Any]:
        folder = "drafts"
        native = self._native_folder(folder)
        _, validity = self._select(native, readonly=False)
        revision = uuid.uuid4().hex
        draft["X-EWS-MCP-Draft-Revision"] = revision
        details: Dict[str, Any] = {
            "draft_id": None, "folder": folder, "backend": self.kind,
            "stage": "append", "sent": False, "revision": revision,
            "cleanup_attempted": False, "draft_removed": False,
        }
        try:
            new_uid = self._append_raw(native, draft.as_bytes())
            details["stage"] = "identify"
            _, current_validity = self._select(native, readonly=False)
            if not validity or current_validity != validity:
                raise MailBackendError("Draft folder UIDVALIDITY changed after APPEND")
            if not new_uid:
                matches = self._uid_search([
                    "HEADER", "X-EWS-MCP-Draft-Revision", _imap_literal(revision),
                ])
                if len(matches) == 1:
                    new_uid = str(matches[0])
            if not new_uid:
                raise MailBackendError("Appended draft could not be identified uniquely")
            new_id = f"{current_validity}:{new_uid}"
            details["draft_id"] = new_id
            details["stage"] = "attachments"
            attachments = self.list_attachments(new_id, folder)
        except (MailBackendError, imaplib.IMAP4.error, OSError) as exc:
            if cleanup_on_failure and details["draft_id"]:
                details["cleanup_attempted"] = True
                try:
                    self._resolve_for_write(details["draft_id"], folder)
                    self._store(new_uid, "+FLAGS", "(\\Deleted)")
                    self._expunge(new_uid)
                    details["draft_removed"] = True
                except (MailBackendError, imaplib.IMAP4.error, OSError) as cleanup_exc:
                    details["cleanup_error"] = str(cleanup_exc)
            details["recovery"] = (
                "The new draft was removed; no message was sent."
                if details["draft_removed"] else
                "No message was sent. Inspect the drafts folder using draft_id or "
                "the X-EWS-MCP-Draft-Revision marker before retrying; an appended draft may remain."
            )
            raise MailBackendError(str(exc), details=details) from exc
        return {
            "id": new_id, "draft_id": new_id, "folder": folder,
            "backend": self.kind, "sent": False,
            "attachments": [item.to_dict() for item in attachments],
            "attachments_verified": True, "cleanup_attempted": False,
            "draft_removed": False,
        }

    @_mailbox_operation
    def create_draft(self, message: OutgoingMessage) -> Dict[str, Any]:
        return self._save_draft(self._build_draft(message), message.cleanup_on_failure)

    @_mailbox_operation
    def create_reply_draft(
        self, message_id: str, message: OutgoingMessage, folder: str = "inbox",
    ) -> Dict[str, Any]:
        _, _, meta = self._fetch_full(message_id, folder)
        original = email.message_from_bytes(meta["raw"], policy=email.policy.default)
        outgoing = self._build_draft(message)
        recipients = list(message.to) or [
            address for address in _addresses(original.get("Reply-To") or original.get("From"))
            if address.lower() != self.email.lower()
        ]
        cc = list(message.cc)
        seen = {address.lower() for address in recipients + cc + list(message.bcc)}
        seen.add(self.email.lower())
        if message.reply_all:
            for header, target in (("To", recipients), ("Cc", cc)):
                for address in _addresses(original.get(header)):
                    if address.lower() not in seen:
                        target.append(address)
                        seen.add(address.lower())
        for name, addresses in (("To", recipients), ("Cc", cc)):
            if name in outgoing:
                del outgoing[name]
            if addresses:
                outgoing[name] = ", ".join(addresses)
        if not message.subject:
            subject = _decode_header_value(original.get("Subject"))
            outgoing.replace_header("Subject", subject if subject.lower().startswith("re:") else f"Re: {subject}")
        internet_id = str(original.get("Message-ID") or "").strip()
        if internet_id:
            outgoing["In-Reply-To"] = internet_id
        references = str(original.get("References") or original.get("In-Reply-To") or "").split()
        if internet_id and internet_id not in references:
            references.append(internet_id)
        if references:
            outgoing["References"] = " ".join(references)
        plain_part = original.get_body(preferencelist=("plain",))
        html_part = original.get_body(preferencelist=("html",))
        original_text = (
            plain_part.get_content() if plain_part is not None else
            unescape(re.sub(r"<[^>]+>", "", html_part.get_content())) if html_part is not None else ""
        )
        attribution = f"On {original.get('Date') or 'the original message'}, {original.get('From') or 'the sender'} wrote:"
        plain = outgoing.get_body(preferencelist=("plain",))
        html = outgoing.get_body(preferencelist=("html",))
        self._replace_body_part(
            plain, plain.get_content().rstrip() + "\n\n" + attribution + "\n" +
            "\n".join("> " + line for line in original_text.splitlines()), "plain",
        )
        self._replace_body_part(
            html, html.get_content() + f"<p>{escape(attribution)}</p><blockquote><pre>{escape(original_text)}</pre></blockquote>",
            "html",
        )
        return self._save_draft(outgoing, message.cleanup_on_failure)

    @_mailbox_operation
    def create_forward_draft(
        self, message_id: str, message: OutgoingMessage, folder: str = "inbox",
    ) -> Dict[str, Any]:
        outgoing = self._build_forward(message_id, message, folder)
        _stamp_tracking_header(outgoing, message.tracking_id)
        if message.bcc:
            outgoing["Bcc"] = ", ".join(message.bcc)
        return self._save_draft(outgoing, message.cleanup_on_failure)

    @_mailbox_operation
    def update_draft(
        self, draft_id: str, subject: Optional[str] = None,
        body: Optional[str] = None, body_format: str = "markdown",
        folder: str = "drafts",
    ) -> Dict[str, Any]:
        def mutate(draft: EmailMessage) -> None:
            if subject is not None:
                if "Subject" in draft:
                    draft.replace_header("Subject", subject)
                else:
                    draft["Subject"] = subject
            if body is None:
                return
            html, _ = compose_body(body, body_format)
            text = body if body_format != "html" else unescape(re.sub(r"<[^>]+>", "", html))
            plain_part = draft.get_body(preferencelist=("plain",))
            html_part = draft.get_body(preferencelist=("html",))
            if plain_part is not None:
                self._replace_body_part(plain_part, text, "plain")
            if html_part is not None:
                self._replace_body_part(html_part, html, "html")
            if plain_part is not None and html_part is None:
                plain_part.make_alternative()
                plain_part.add_alternative(html, subtype="html")
            elif html_part is not None and plain_part is None:
                html_part.make_alternative()
                html_part.add_alternative(text, subtype="plain")
            elif plain_part is None and html_part is None:
                note = EmailMessage()
                note.set_content(text)
                note.add_alternative(html, subtype="html")
                if draft.get_content_type() != "multipart/mixed":
                    draft.make_mixed()
                draft.get_payload().insert(0, note)

        new_id, attachments, cleanup = self._rewrite_draft(
            draft_id, folder, mutate, "update_draft",
        )
        return {
            "id": new_id, "draft_id": new_id, "previous_id": draft_id,
            "folder": folder, "backend": self.kind, "sent": False,
            "attachments": [item.to_dict() for item in attachments],
            **cleanup,
        }

    def _build_forward(
        self, message_id: str, message: OutgoingMessage, folder: str,
    ) -> EmailMessage:
        """Build a forward without submitting or modifying its source."""
        recipients = [address for address in message.to if address]
        cc = [address for address in message.cc if address]
        _, _, meta = self._fetch_full(message_id, folder)
        outgoing = email.message_from_bytes(meta["raw"], policy=email.policy.default)
        original_subject = _decode_header_value(outgoing.get("Subject"))
        subject = message.subject or (
            original_subject if original_subject.lower().startswith(("fwd:", "fw:"))
            else f"Fwd: {original_subject}".strip()
        )
        forwarded_headers = "\n".join(
            f"{name}: {_decode_header_value(outgoing.get(name))}"
            for name in ("From", "Date", "Subject", "To", "Cc") if outgoing.get(name)
        )
        # MIME headers and the payload belong to the content being forwarded;
        # transport headers (especially Bcc/Resent-Bcc) belong only to its source.
        for name in list(outgoing.keys()):
            if name.lower() != "mime-version" and not name.lower().startswith("content-"):
                del outgoing[name]
        outgoing["From"] = self.email
        outgoing["To"] = ", ".join(recipients)
        if cc:
            outgoing["Cc"] = ", ".join(cc)
        outgoing["Subject"] = subject
        outgoing["Date"] = email.utils.formatdate(localtime=True)
        outgoing["Message-ID"] = email.utils.make_msgid(domain=self.email.split("@")[-1])

        html, _ = compose_body(message.body or "", message.body_format or "markdown")
        text = message.body if message.body_format != "html" else re.sub(r"<[^>]+>", "", html)
        plain_prefix = f"{text or ''}\n\n---------- Forwarded message ----------\n{forwarded_headers}\n\n"
        html_prefix = f"{html}<hr><pre>{escape(forwarded_headers)}</pre>"
        # get_body does not descend into attached messages. Walking every text
        # leaf would corrupt nested .eml files and text attachments.
        plain_part = outgoing.get_body(preferencelist=("plain",))
        html_part = outgoing.get_body(preferencelist=("html",))
        original_plain = plain_part.get_content() if plain_part is not None else ""

        replace_body = self._replace_body_part

        if plain_part is not None:
            replace_body(plain_part, plain_prefix + original_plain, "plain")
        if html_part is not None:
            original_html = html_part.get_content()
            body_tag = re.search(r"<body\b[^>]*>", original_html, flags=re.IGNORECASE)
            if body_tag:
                content = original_html[:body_tag.end()] + html_prefix + original_html[body_tag.end():]
            else:
                content = html_prefix + original_html
            replace_body(html_part, content, "html")
        elif plain_part is not None:
            plain_part.make_alternative()
            plain_part.add_alternative(
                html_prefix + f"<pre>{escape(original_plain)}</pre>", subtype="html",
            )
        else:
            # Non-text messages still need a visible forwarding note without
            # flattening or discarding their original MIME content.
            note = EmailMessage()
            note.set_content(plain_prefix)
            note.add_alternative(html_prefix, subtype="html")
            if outgoing.get_content_type() != "multipart/mixed":
                outgoing.make_mixed()
            outgoing.get_payload().insert(0, note)

        self._add_files(outgoing, message.attachments)
        return outgoing

    @_mailbox_operation
    def forward_message(
        self, message_id: str, message: OutgoingMessage, folder: str = "inbox",
    ) -> Dict[str, Any]:
        """Forward a copy of the original MIME tree, never rewriting the source."""
        recipients = [address for address in message.to if address]
        cc = [address for address in message.cc if address]
        bcc = [address for address in message.bcc if address]
        if not recipients:
            raise MailBackendError("no recipients: 'to' is required")
        outgoing = self._build_forward(message_id, message, folder)
        tracking_id = _stamp_tracking_header(outgoing, message.tracking_id)
        subject = str(outgoing["Subject"])
        envelope = recipients + cc + bcc
        refused = self._smtp_send(outgoing, envelope)
        accepted = [address for address in envelope if address not in refused]
        if not accepted:
            raise MailBackendError("SMTP refused every forwarding recipient")
        result: Dict[str, Any] = {
            "sent": not bool(refused),
            "accepted_by_mail_server": True,
            "delivery_confirmed": False,
            "backend": self.kind,
            "to": recipients,
            "cc": cc,
            "bcc": bcc,
            "subject": subject,
            "forwarded_from": message_id,
            "tracking_id": tracking_id,
            "accepted_recipients": accepted,
            "partial_failure": bool(refused),
            "refused_recipients": {
                address: {
                    "code": code,
                    "message": reason.decode("utf-8", "replace") if isinstance(reason, bytes) else str(reason),
                }
                for address, (code, reason) in refused.items()
            },
            "saved_to_sent": None,
        }
        warnings: List[str] = []
        if refused:
            warnings.append(
                "SMTP accepted only some recipients. Do not resend to accepted "
                "recipients; retry only the refused recipients."
            )
        try:
            result["saved_to_sent"] = self._append_to_sent(outgoing)
            if result["saved_to_sent"] is None:
                warnings.append("SMTP accepted the message, but its Sent copy could not be saved.")
        except (MailBackendError, imaplib.IMAP4.error, OSError) as exc:
            warnings.append(f"SMTP accepted the message, but saving its Sent copy failed: {exc}")
        if warnings:
            result["warnings"] = warnings
        return result

    def _smtp_send(
        self, outgoing: EmailMessage | bytes, envelope: Sequence[str],
    ) -> Dict[str, Tuple[int, bytes]]:
        host = str(self.account.get("smtp_host") or "")
        port = int(self.account.get("smtp_port") or 587)
        username = str(self.account.get("smtp_username") or self.email)
        password = self.account.password
        if not host:
            raise MailBackendError(f"{self.email}: no smtp_host configured")
        if self._oauth is None and not password:
            raise MailBackendError(f"{self.email}: no SMTP password configured")

        context = ssl.create_default_context()
        refused: Optional[Dict[str, Tuple[int, bytes]]] = None
        try:
            if self.account.get("smtp_ssl"):
                with smtplib.SMTP_SSL(host, port, timeout=60, context=context) as smtp:
                    self._smtp_sign_in(smtp, username, password)
                    refused = (
                        smtp.sendmail(self.email, list(envelope), outgoing)
                        if isinstance(outgoing, bytes)
                        else smtp.send_message(outgoing, to_addrs=list(envelope))
                    )
            else:
                with smtplib.SMTP(host, port, timeout=60) as smtp:
                    smtp.ehlo()
                    if self.account.get("smtp_starttls", True):
                        smtp.starttls(context=context)
                        smtp.ehlo()
                    self._smtp_sign_in(smtp, username, password)
                    refused = (
                        smtp.sendmail(self.email, list(envelope), outgoing)
                        if isinstance(outgoing, bytes)
                        else smtp.send_message(outgoing, to_addrs=list(envelope))
                    )
        except smtplib.SMTPAuthenticationError as exc:
            if self._oauth is not None:
                raise MailBackendError(
                    self._xoauth2_refused("SMTP", f"{host}:{port}", username, exc)
                ) from exc
            raise MailBackendError(
                f"SMTP login failed for {self.email} on {host}:{port}: {exc}. "
                f"iCloud requires an app-specific password."
            ) from exc
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            if refused is not None:
                # QUIT can fail after DATA was accepted; never invite a resend.
                _log.warning("SMTP accepted mail for %s but closing failed: %s", self.email, exc)
                return refused
            raise MailBackendError(
                f"SMTP send failed for {self.email} via {host}:{port}: {exc}"
            ) from exc
        return refused

    def _smtp_sign_in(self, smtp: smtplib.SMTP, username: str, password: Optional[str]) -> None:
        if self._oauth is None:
            smtp.login(username, password)
            return
        smtp.ehlo_or_helo_if_needed()
        self._xoauth2_sign_in(
            username,
            # smtplib asks for the initial response first, then answers each 334
            # challenge; an error challenge must get an empty reply.
            lambda sasl: smtp.auth(
                "XOAUTH2", lambda challenge=None: sasl if challenge is None else ""
            ),
            smtplib.SMTPAuthenticationError,
        )

    @_mailbox_operation
    def _append_to_sent(self, outgoing: EmailMessage | bytes) -> Optional[str]:
        """Copy the sent message into the Sent folder (best effort).

        iCloud's SMTP does not file sent mail for you, so we APPEND it. A
        failure here must not turn a delivered message into an error.
        """
        # Gmail files SMTP submissions into Sent itself; appending duplicates.
        if self.account.provider in ("gmail", "google"):
            return "auto (Gmail files SMTP mail in Sent)"
        try:
            target = self._native_folder(self.account.get("sent_folder") or "sent")
        except MailBackendError as exc:
            _log.info("no Sent folder for %s: %s", self.email, exc)
            return None
        try:
            conn = self._connect()
            typ, data = conn.append(
                _quote_mailbox(target),
                "(\\Seen)",
                imaplib.Time2Internaldate(datetime.now(tz=timezone.utc).timestamp()),
                outgoing if isinstance(outgoing, bytes) else outgoing.as_bytes(),
            )
            if typ != "OK":
                _log.warning("could not APPEND to %s for %s: %s %r", target, self.email, typ, data)
                return None
            return target
        except (imaplib.IMAP4.error, OSError) as exc:
            _log.warning("could not APPEND to %s for %s: %s", target, self.email, exc)
            return None
