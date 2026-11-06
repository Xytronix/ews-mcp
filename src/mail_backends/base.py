"""Provider-agnostic mailbox backend contract.

The Exchange (EWS) tools in ``src/tools/`` talk to ``exchangelib.Account``
objects directly and stay exactly as they are. This module defines a much
smaller, provider-neutral mail surface so *additional* mailboxes can be served
by other protocols (IMAP for iCloud, Microsoft Graph for Outlook.com /
Microsoft 365) through one set of tools.

Every backend is synchronous — the MCP tools call it via
``asyncio.to_thread`` so blocking IMAP/HTTP work never stalls the event loop.

Folder addressing
-----------------
Folder names differ wildly between providers ("Deleted Items" on SmarterMail,
"Trash" on iCloud, "deleteditems" on Graph). Callers therefore use the
well-known aliases in :data:`WELL_KNOWN_FOLDERS`; each backend resolves them to
its own native name. Any other string is passed through as a literal folder
path so custom folders still work.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Provider-neutral folder aliases understood by every backend.
WELL_KNOWN_FOLDERS = ("inbox", "sent", "drafts", "trash", "junk", "archive")

#: Folders a whole-mailbox search (``search_scope=["all"]``) leaves out, with
#: their subfolders, unless the caller names them next to ``"all"``.
ALL_FOLDERS_EXCLUDED = ("trash", "junk", "drafts", "outbox")

# Body formats accepted/returned by the unified tools (same vocabulary as
# ``src/body_format.py`` so responses look identical to the EWS tools).
BODY_FORMATS = ("markdown", "html", "text")


class MailBackendError(Exception):
    """Any backend-level failure (connection, auth, protocol, not-found)."""

    def __init__(self, message: str, *, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


def normalize_folder(name: Optional[str]) -> str:
    """Return a canonical folder token: a well-known alias or a literal path."""
    if not name:
        return "inbox"
    token = name.strip()
    lowered = token.lower()
    aliases = {
        "inbox": "inbox",
        "sent": "sent",
        "sent items": "sent",
        "sentitems": "sent",
        "drafts": "drafts",
        "draft": "drafts",
        "trash": "trash",
        "deleted": "trash",
        "deleted items": "trash",
        "deleteditems": "trash",
        "bin": "trash",
        "junk": "junk",
        "spam": "junk",
        "junk email": "junk",
        "junk e-mail": "junk",
        "junkemail": "junk",
        "archive": "archive",
    }
    return aliases.get(lowered, token)


def preview_text(text: str, limit: int = 200) -> str:
    """Collapse whitespace and clip to ``limit`` chars for list previews."""
    collapsed = re.sub(r"\s+", " ", text or "").strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "\u2026"


@dataclass
class MailFolder:
    """One folder in a mailbox."""

    id: str
    name: str
    path: str
    well_known: Optional[str] = None
    total_count: Optional[int] = None
    unread_count: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class MailAttachmentInfo:
    """Attachment metadata (content is never inlined in list/get responses)."""

    name: str
    content_type: Optional[str] = None
    size: Optional[int] = None
    attachment_id: Optional[str] = None
    is_inline: bool = False
    content_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class MailAttachmentContent:
    """One attachment's metadata *and* bytes."""

    name: str
    content: bytes
    content_type: Optional[str] = None
    attachment_id: Optional[str] = None
    is_inline: bool = False
    content_id: Optional[str] = None

    @property
    def size(self) -> int:
        return len(self.content)

    def info(self) -> MailAttachmentInfo:
        return MailAttachmentInfo(
            name=self.name,
            content_type=self.content_type,
            size=self.size,
            attachment_id=self.attachment_id,
            is_inline=self.is_inline,
            content_id=self.content_id,
        )


def select_attachment(
    items: List[MailAttachmentInfo],
    attachment_id: Optional[str] = None,
    attachment_name: Optional[str] = None,
) -> MailAttachmentInfo:
    """Pick one attachment by id (preferred) or exact case-insensitive name.

    Raises :class:`MailBackendError` when nothing matches or a name is
    ambiguous — the same contract the EWS tools already expose.
    """
    if attachment_id:
        wanted = str(attachment_id).strip()
        for item in items:
            if str(item.attachment_id or "") == wanted:
                return item
    if attachment_name:
        wanted_name = str(attachment_name).strip().lower()
        matches = [i for i in items if (i.name or "").strip().lower() == wanted_name]
        if len(matches) > 1:
            raise MailBackendError(
                f"multiple attachments named {attachment_name!r} on this "
                f"message; use attachment_id to disambiguate"
            )
        if matches:
            return matches[0]
    ident = attachment_id or attachment_name
    available = ", ".join(i.name for i in items) or "(none)"
    raise MailBackendError(
        f"attachment {ident!r} not found; attachments on this message: {available}"
    )


@dataclass
class MailMessage:
    """One message. ``body`` is populated only by :meth:`MailBackend.get_message`.

    ``id`` is the backend's own opaque handle (IMAP UID, Graph message id, EWS
    ItemId) and is only valid for the account+folder it came from — which is why
    ``folder`` travels with it.
    """

    id: str
    folder: str
    subject: str = ""
    from_address: str = ""
    from_name: str = ""
    to: List[str] = field(default_factory=list)
    cc: List[str] = field(default_factory=list)
    bcc: List[str] = field(default_factory=list)
    reply_to: List[str] = field(default_factory=list)
    received: Optional[str] = None          # ISO-8601
    sent: Optional[str] = None              # ISO-8601
    is_read: bool = False
    is_flagged: bool = False
    #: "NotFlagged" | "Flagged" | "Complete" (Exchange vocabulary); None when
    #: the provider did not report the flag
    flag_status: Optional[str] = None
    has_attachments: bool = False
    importance: Optional[str] = None
    internet_message_id: Optional[str] = None
    conversation_id: Optional[str] = None
    preview: str = ""
    body: Optional[str] = None
    body_format: Optional[str] = None
    attachments: List[MailAttachmentInfo] = field(default_factory=list)
    size: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.id,
            "folder": self.folder,
            "subject": self.subject,
            "from": self.from_address,
            "is_read": self.is_read,
            "has_attachments": self.has_attachments,
        }
        if self.from_name:
            out["from_name"] = self.from_name
        for key, value in (
            ("to", self.to),
            ("cc", self.cc),
            ("bcc", self.bcc),
            ("reply_to", self.reply_to),
        ):
            if value:
                out[key] = value
        for key, value in (
            ("received_time", self.received),
            ("sent_time", self.sent),
            ("importance", self.importance),
            ("internet_message_id", self.internet_message_id),
            ("conversation_id", self.conversation_id),
            ("size", self.size),
        ):
            if value is not None:
                out[key] = value
        if self.is_flagged:
            out["is_flagged"] = True
        if self.flag_status is not None:
            out["flag_status"] = self.flag_status
        if self.preview:
            out["preview"] = self.preview
        if self.body is not None:
            out["body"] = self.body
            out["body_format"] = self.body_format
        if self.attachments:
            out["attachments"] = [a.to_dict() for a in self.attachments]
        return out

@dataclass
class OutgoingAttachment:
    """One file to send with an :class:`OutgoingMessage`.

    ``content`` is the raw bytes; backends encode (base64, MIME) themselves.
    ``content_id`` is the cid the HTML body cites (no angle brackets).
    """

    name: str
    content: bytes
    content_type: str = "application/octet-stream"
    is_inline: bool = False
    content_id: Optional[str] = None


@dataclass
class OutgoingMessage:
    """A message to send."""

    to: List[str]
    subject: str = ""
    body: str = ""
    body_format: str = "markdown"
    cc: List[str] = field(default_factory=list)
    bcc: List[str] = field(default_factory=list)
    reply_to_message_id: Optional[str] = None
    reply_to_folder: Optional[str] = None
    reply_all: bool = False
    #: correlation ID the backend must publish in the header named by
    #: :func:`src.utils.outbound_tracking_header`. A backend that cannot set
    #: the header, or runs with tracking off, reports ``tracking_id: None``
    #: in its send result rather than claiming an ID that is not on the wire.
    tracking_id: Optional[str] = None
    #: Files to send with the message. A backend that cannot carry them
    #: MUST raise rather than deliver a message with the files dropped —
    #: a mail that arrives without its attachment is worse than a refusal.
    attachments: List[OutgoingAttachment] = field(default_factory=list)
    #: Best-effort removal of a newly-created Graph or IMAP draft after a
    #: failed multi-step operation. Never applies to a caller-supplied draft.
    cleanup_on_failure: bool = False


class MailBackend(ABC):
    """Uniform mail operations for one mailbox on one provider."""

    #: short provider token, e.g. ``"imap"`` / ``"graph"`` / ``"ews"``
    kind: str = "unknown"
    #: ``flag_status`` values :meth:`search_messages` can filter on
    flag_statuses: Tuple[str, ...] = ("NotFlagged", "Flagged")
    #: whether :meth:`search_all_folders` is implemented
    supports_all_folder_search: bool = False
    #: optional keyword criteria :meth:`search_messages` and
    #: :meth:`search_all_folders` also accept: ``"to_address"`` (To header
    #: text), ``"body"`` (body text) and ``"is_read"`` (True: read mail only,
    #: False: unread only). The tools pass only the ones named here.
    search_filters: Tuple[str, ...] = ()

    def __init__(self, email: str) -> None:
        self.email = email

    # --- discovery ------------------------------------------------------
    @abstractmethod
    def test_connection(self) -> Dict[str, Any]:
        """Authenticate and return ``{"connected": bool, ...}``. Never raises."""

    @abstractmethod
    def list_folders(self) -> List[MailFolder]:
        """Return every mail folder in the mailbox."""

    # --- read -----------------------------------------------------------
    @abstractmethod
    def list_messages(
        self,
        folder: str = "inbox",
        limit: int = 25,
        offset: int = 0,
        unread_only: bool = False,
    ) -> List[MailMessage]:
        """Newest-first page of message summaries (no ``body``)."""

    @abstractmethod
    def get_message(
        self,
        message_id: str,
        folder: str = "inbox",
        body_format: str = "markdown",
    ) -> MailMessage:
        """One message including ``body`` and attachment metadata."""

    @abstractmethod
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
        """Server-side search. ``query`` matches subject/body/participants.

        ``flag_status`` is one of :attr:`flag_statuses`.
        """

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
    ) -> Tuple[List[MailMessage], List[str]]:
        """Search every mail folder at once, newest first.

        Returns the hits, each with its folder path in ``folder``, and the
        folder paths covered. :data:`ALL_FOLDERS_EXCLUDED` folders and their
        subfolders stay out unless ``include`` names them.
        """
        raise self._unsupported("search every folder at once", "search one folder per call")

    # --- write ----------------------------------------------------------
    @abstractmethod
    def send_message(self, message: OutgoingMessage) -> Dict[str, Any]:
        """Send (or reply to) a message. Returns a small result dict.

        ``message.attachments`` must either all be delivered or the call
        must raise. Silently dropping files is forbidden: a mail that
        arrives without its attachment is worse than a refusal.
        """

    def create_draft(self, message: OutgoingMessage) -> Dict[str, Any]:
        """Save a complete draft, returning its current identity and folder."""
        raise self._unsupported("create drafts")

    def create_reply_draft(
        self, message_id: str, message: OutgoingMessage, folder: str = "inbox",
    ) -> Dict[str, Any]:
        """Save a threaded reply for review, without submitting it."""
        raise self._unsupported("create reply drafts")

    def create_forward_draft(
        self, message_id: str, message: OutgoingMessage, folder: str = "inbox",
    ) -> Dict[str, Any]:
        """Save a forward preserving original content and attachments."""
        raise self._unsupported("create forward drafts")

    def update_draft(
        self, draft_id: str, subject: Optional[str] = None,
        body: Optional[str] = None, body_format: str = "markdown",
        folder: str = "drafts",
    ) -> Dict[str, Any]:
        """Update supplied fields only: None preserves, an empty string clears."""
        raise self._unsupported("update drafts")

    def send_draft(
        self, draft_id: str, folder: str = "drafts", dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Submit a stored draft without recomposing; dry_run never mutates.

        Exchange uses its existing exchangelib tool path, like attachments.
        Backend results distinguish submission acceptance from final delivery.
        """
        raise self._unsupported("send drafts")

    def forward_message(
        self, message_id: str, message: OutgoingMessage, folder: str = "inbox",
    ) -> Dict[str, Any]:
        """Forward original content and attachments plus additional files."""
        raise self._unsupported("forward messages")

    @abstractmethod
    def move_message(
        self, message_id: str, destination: str, folder: str = "inbox",
    ) -> Dict[str, Any]:
        """Move one message to ``destination``. Returns the new id when known."""

    @abstractmethod
    def delete_message(
        self, message_id: str, folder: str = "inbox", hard_delete: bool = False,
    ) -> Dict[str, Any]:
        """Move to trash, or permanently delete when ``hard_delete``."""

    @abstractmethod
    def mark_message(
        self,
        message_id: str,
        folder: str = "inbox",
        is_read: Optional[bool] = None,
        is_flagged: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Set read and/or flagged state. ``None`` leaves a flag untouched."""


    # --- calendar -------------------------------------------------------
    # Defaults raise instead of being abstract so providers can opt into
    # calendar/contact support independently of mail.
    def create_appointment(self, **fields: Any) -> Dict[str, Any]:
        raise self._unsupported("create appointments")

    def get_calendar(self, **fields: Any) -> Dict[str, Any]:
        raise self._unsupported("read calendar")

    def update_appointment(self, item_id: str, **fields: Any) -> Dict[str, Any]:
        raise self._unsupported("update appointments")

    def delete_appointment(self, item_id: str, **fields: Any) -> Dict[str, Any]:
        raise self._unsupported("delete appointments")

    def respond_to_meeting(self, item_id: str, **fields: Any) -> Dict[str, Any]:
        raise self._unsupported("respond to meeting invitations")

    def check_availability(self, **fields: Any) -> Dict[str, Any]:
        raise self._unsupported("check availability")

    def find_meeting_times(self, **fields: Any) -> Dict[str, Any]:
        raise self._unsupported("find meeting times")

    # --- contacts -------------------------------------------------------
    def create_contact(self, **fields: Any) -> Dict[str, Any]:
        raise self._unsupported("create contacts")

    def update_contact(self, item_id: str, **fields: Any) -> Dict[str, Any]:
        raise self._unsupported("update contacts")

    def delete_contact(self, item_id: str) -> Dict[str, Any]:
        raise self._unsupported("delete contacts")

    def find_person(self, **fields: Any) -> Dict[str, Any]:
        raise self._unsupported("search contacts")
    # --- attachments ----------------------------------------------------
    # Defaults raise instead of being abstract so the EWS backend — whose
    # attachment support lives in the exchangelib tools — stays untouched.
    def _unsupported(self, operation: str, detail: str = "") -> MailBackendError:
        """Clear, immediate refusal — the tool layer prefixes the mailbox."""
        message = f"{self.kind} backend cannot {operation}"
        return MailBackendError(f"{message} — {detail}" if detail else message)

    def list_attachments(
        self,
        message_id: str,
        folder: str = "inbox",
        include_inline: bool = True,
    ) -> List[MailAttachmentInfo]:
        """Attachment metadata for one message (no content)."""
        raise self._unsupported("list attachments")

    def get_attachment(
        self,
        message_id: str,
        folder: str = "inbox",
        attachment_id: Optional[str] = None,
        attachment_name: Optional[str] = None,
    ) -> MailAttachmentContent:
        """One attachment's bytes, selected by id or exact name."""
        raise self._unsupported("download attachments")

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
        """Attach a file to a *draft*. Stored mail is immutable elsewhere."""
        raise self._unsupported("add attachments")

    def delete_attachment(
        self,
        message_id: str,
        folder: str = "drafts",
        attachment_id: Optional[str] = None,
        attachment_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Remove an attachment from a *draft*."""
        raise self._unsupported("delete attachments")

    def get_mime(self, message_id: str, folder: str = "inbox") -> bytes:
        """Raw RFC-822 bytes for one message."""
        raise self._unsupported("export MIME")

    # --- lifecycle ------------------------------------------------------
    def close(self) -> None:  # pragma: no cover - optional override
        """Release sockets/sessions. Safe to call repeatedly."""
