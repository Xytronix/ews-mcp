"""SmarterMail mail capabilities that EWS cannot express.

Scheduled send, undo-send, bulk flag/read/category/delete, .eml export,
List-Unsubscribe, read receipts, and SpamFoo classification. All of these
ride the private JSON API the webmail uses. Payloads were read off the
webmail bundle and live-verified against a hosted SmarterMail server.

ID forms: every tool takes SmarterMail folder-scoped ``uid`` (int). An
EWS/webmail ``mid`` is accepted as a fallback and mapped through
``POST /api/v1/mail/mid-to-uid``. These are NOT Exchange item ids.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from uuid import UUID, uuid4

from exchangelib import HTMLBody, Mailbox, Message

from .smartermail_base import SmarterMailTool
from ..body_format import WRITE_FORMAT_SCHEMA, compose_body
from ..exceptions import ToolExecutionError, ValidationError
from ..smartermail import SCHEDULED_FOLDER, SmarterMailError
from ..utils import format_error_response, format_success_response, parse_datetime, find_message_for_account
from .outgoing_attachments import (
    FILES_SCHEMA, attach_outgoing_files, load_outgoing_files,
    save_draft_with_attachments,
)

# Webmail folder paths. The list endpoint is case-insensitive for the well-known
# names; we normalise aliases onto the paths the API returned live.
_FOLDER_ALIASES = {
    "inbox": "inbox",
    "sent": "sent items",
    "sent items": "sent items",
    "sentitems": "sent items",
    "drafts": "drafts",
    "draft": "drafts",
    "deleted": "deleted items",
    "deleted items": "deleted items",
    "trash": "deleted items",
    "junk": "junk e-mail",
    "junk e-mail": "junk e-mail",
    "junk email": "junk e-mail",
    "archive": "archive",
    "scheduled": SCHEDULED_FOLDER,
}

_CLASSIFICATIONS = (
    "primary",
    "transactions",
    "updates",
    "promotions",
    "unclassified",
    "automatic",
)

_UNSUB_METHODS = ("mailto", "oneclick", "link")

_BULK_ACTIONS = (
    "mark_read",
    "mark_unread",
    "flag",
    "unflag",
    "categorize",
    "clear_categories",
    "delete",
    "purge",
)

_META_ACTIONS = (
    "send_receipt",
    "reject_receipt",
    "classify",
    "counts",
)


class _MailTool(SmarterMailTool):
    """SmarterMailTool that runs the blocking HTTP hop off the event loop."""

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    async def execute(self, **kwargs) -> Dict[str, Any]:
        return await asyncio.to_thread(self._run, kwargs)

    def _run(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        client = self.client(kwargs.get("target_mailbox"))
        return self._dispatch(client, kwargs)

    def _download(self, client, method: str, path: str, **kwargs) -> bytes:
        try:
            return client.download(method, path, **kwargs)
        except SmarterMailError as exc:
            raise ToolExecutionError(str(exc)) from exc


def _action(kwargs: Dict[str, Any], allowed: Iterable[str]) -> str:
    action = (kwargs.get("action") or "").strip().lower().replace("-", "_")
    if action not in set(allowed):
        raise ValidationError(
            f"action must be one of {', '.join(allowed)}; got {action!r}"
        )
    return action


def _as_addr_str(value: Any) -> str:
    """Webmail ``to``/``cc``/``bcc`` are semicolon-separated strings."""
    if value is None or value == "":
        return ""
    if isinstance(value, (list, tuple)):
        return ";".join(str(v).strip() for v in value if v not in (None, ""))
    return str(value).strip()


def _uid_list(value: Any, *, required: bool = True) -> List[int]:
    if value is None or value == "":
        items: List[Any] = []
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = [value]
    out: List[int] = []
    for item in items:
        if item in (None, ""):
            continue
        try:
            out.append(int(item))
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                f"uid must be a SmarterMail folder uid (int), got {item!r}"
            ) from exc
    if required and not out:
        raise ValidationError("uids required")
    return out


def _folder(kwargs: Dict[str, Any], *, default: Optional[str] = None) -> str:
    raw = kwargs.get("folder")
    if raw in (None, ""):
        if default is None:
            raise ValidationError("folder required")
        raw = default
    key = str(raw).strip()
    mapped = _FOLDER_ALIASES.get(key.lower())
    return mapped if mapped is not None else key


def _owner(client, kwargs: Dict[str, Any]) -> str:
    return (kwargs.get("owner_email_address") or client.email or "").strip()


def _future_utc_z(value: Any) -> str:
    raw = (value or "").strip() if isinstance(value, str) else str(value or "").strip()
    if not raw:
        raise ValidationError("send_at required")
    dt = parse_datetime(raw)
    if dt is None:
        raise ValidationError(f"send_at is not a valid ISO-8601 datetime: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if dt <= datetime.now(timezone.utc):
        raise ValidationError("send_at must be in the future (UTC)")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _compose(body: str, body_format: str) -> str:
    try:
        html, _actual = compose_body(body or "", body_format or "html")
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return html


def _resolve_uid(tool: SmarterMailTool, client, kwargs: Dict[str, Any], folder: str) -> int:
    """Prefer ``uid``; map ``mid`` through mail/mid-to-uid when uid is absent."""
    if kwargs.get("uid") not in (None, ""):
        return _uid_list(kwargs.get("uid"))[0]
    mid = kwargs.get("mid") or kwargs.get("message_id")
    if mid in (None, ""):
        raise ValidationError(
            "uid required (SmarterMail folder uid). Pass mid to map an EWS/"
            "webmail message id via mail/mid-to-uid."
        )
    data = tool.call(
        client,
        "POST",
        "/api/v1/mail/mid-to-uid",
        json={
            "ownerEmailAddress": _owner(client, kwargs),
            "folder": folder,
            "mid": mid,
        },
    )
    uid = data.get("uid")
    if uid is None:
        raise ToolExecutionError(f"mid-to-uid returned no uid for mid={mid!r}")
    return int(uid)


def _slim_message(m: Dict[str, Any]) -> Dict[str, Any]:
    recips = m.get("recipients") or []
    emails: List[str] = []
    for r in recips:
        if isinstance(r, dict):
            email = (r.get("email") or "").strip()
        else:
            email = str(r).strip()
        if email:
            emails.append(email)
    from_addr = m.get("fromAddress")
    if not from_addr and isinstance(m.get("from"), dict):
        from_addr = m.get("from", {}).get("email")
    return {
        "uid": m.get("uid"),
        "mid": m.get("mid"),
        "subject": m.get("subject"),
        "from": from_addr,
        "recipients": emails,
        "date_sent": m.get("dateSent"),
        "internal_date": m.get("internalDate"),
        "folder": m.get("folder"),
        "is_draft": m.get("isDraft"),
        "is_seen": m.get("isSeen"),
        "is_flagged": m.get("isFlagged"),
        "categories": m.get("categories") or [],
        "classification": m.get("classification"),
        "size": m.get("size"),
    }


def _list_folder(tool: SmarterMailTool, client, folder: str, *, start: int = 0, count: int = 50,
                 owner: str = "") -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "folder": folder,
        "skip": int(start),
        "take": int(count),
        "sortType": "internalDate",
    }
    if owner:
        payload["ownerEmailAddress"] = owner
    return tool.call(client, "POST", "/api/v1/mail/messages", json=payload)


def _scheduled_contains(tool, client, uid: int) -> bool:
    """Verify membership across the queue; a malformed page is not absence."""
    start = 0
    seen = set()
    initial_total = None
    while True:
        data = _list_folder(tool, client, SCHEDULED_FOLDER, start=start,
                            count=50, owner=client.email)
        rows = data.get("results")
        if not isinstance(rows, list):
            raise ToolExecutionError("Cannot verify Scheduled queue: missing results")
        page_uids = []
        for row in rows:
            try:
                page_uids.append(int(row["uid"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ToolExecutionError("Cannot verify Scheduled queue: invalid uid") from exc
        if uid in page_uids:
            return True
        if any(value in seen for value in page_uids):
            raise ToolExecutionError("Cannot verify Scheduled queue: pagination changed or repeated")
        seen.update(page_uids)
        start += len(rows)
        total = data.get("totalCount", data.get("total"))
        if total is not None:
            try:
                total = int(total)
            except (TypeError, ValueError) as exc:
                raise ToolExecutionError("Cannot verify Scheduled queue: invalid total") from exc
            if total < 0 or (initial_total is not None and total != initial_total):
                raise ToolExecutionError("Cannot verify Scheduled queue: total changed during pagination")
            initial_total = total
            if start >= total:
                return False
            if not rows:
                raise ToolExecutionError("Cannot verify Scheduled queue: truncated page")
        elif not rows:
            return False



def _draft_mid(draft_id: str) -> int:
    """Decode only the live-verified SmarterMail EWS item-key layout."""
    try:
        raw = base64.b64decode(draft_id, validate=True)
        mailbox = UUID(raw[4:40].decode("ascii"))
        mid = int.from_bytes(raw[71:79], "little")
        if (len(raw) != 87 or raw[:4] != b"\x00\x03\x24\x00"
                or raw[40:47] != bytes.fromhex("002c0002000100")
                or raw[47:63] != mailbox.bytes_le
                or not int.from_bytes(raw[63:71], "little")
                or not mid or raw[79:] != bytes(8)):
            raise ValueError("unknown item-key layout")
        return mid
    except (ValueError, UnicodeError) as exc:
        raise ToolExecutionError(
            "Cannot safely map this draft_id to SmarterMail: unsupported EWS item-key layout"
        ) from exc


def _rest_draft(tool, client, draft):
    """Resolve authoritatively, then cross-check EWS and REST message identities."""
    mid = _draft_mid(draft.id)
    mapped = tool.call(client, "POST", "/api/v1/mail/mid-to-uid", json={
        "ownerEmailAddress": client.email, "folder": "drafts", "mid": mid,
    })
    uid = mapped.get("uid")
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        raise ToolExecutionError("Cannot verify saved draft identity: no SmarterMail UID")
    result = tool.call(client, "POST", "/api/v1/mail/message", json={
        "OwnerEmailAddress": client.email, "Folder": "drafts", "UID": uid,
    })
    message = result.get("messageData")
    if (not isinstance(message, dict) or message.get("isDraft") is not True
            or str(message.get("folder", "")).lower() != "drafts"
            or message.get("mid") != mid or message.get("uid") != uid
            or not draft.message_id or message.get("messageID") != draft.message_id):
        raise ToolExecutionError("Cannot verify saved draft identity across EWS and SmarterMail")
    return uid, message

# ---------------------------------------------------------------------------
# Scheduled send
# ---------------------------------------------------------------------------


class ScheduleEmailTool(_MailTool):
    """Park a message in the Scheduled folder until send_at (UTC)."""

    def get_schema(self) -> Dict[str, Any]:
        schema = self.schema(
            "schedule_email",
            "Schedule a SmarterMail email in its native cancellable Scheduled folder. "
            "Use draft_id to schedule a saved EWS draft: omit all compose fields "
            "(to/cc/bcc/subject/body/body_format) to preserve stored content. "
            "Optional files append to the draft, preserving existing attachments. "
            "On failure inspect the reported draft ID before retrying; appended files may remain. "
            "Or compose with to, subject, body and optional files. send_at is a future "
            "ISO-8601 UTC time. Not portable to Graph/IMAP; undo is not recall.",
            {
                "draft_id": {
                    "type": "string", "minLength": 1,
                    "description": "Saved SmarterMail EWS draft message_id; not a REST UID.",
                },
                "files": FILES_SCHEMA,
                "to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Recipient email addresses (string also accepted).",
                },
                "cc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CC recipients (optional).",
                },
                "bcc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "BCC recipients (optional).",
                },
                "subject": {"type": "string", "description": "Email subject."},
                "body": {
                    "type": "string",
                    "description": "Email body. Format set by body_format (default html).",
                },
                **WRITE_FORMAT_SCHEMA,
                "send_at": {
                    "type": "string",
                    "description": (
                        "When to send, ISO-8601. Naive timestamps are treated as "
                        "UTC. Must be in the future; stored as UTC ...Z."
                    ),
                },
            },
            required=["send_at"],
        )
        schema["inputSchema"]["oneOf"] = [
            {"required": ["draft_id"], "not": {"anyOf": [
                {"required": [field]} for field in self._COMPOSE_FIELDS
            ]}},
            {"required": ["to", "subject", "body"], "not": {"required": ["draft_id"]}},
        ]
        return schema

    _COMPOSE_FIELDS = ("to", "cc", "bcc", "subject", "body", "body_format")

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        send_at = _future_utc_z(kwargs.get("send_at"))
        draft_id = kwargs.get("draft_id")
        draft = None
        if "draft_id" in kwargs:
            if not isinstance(draft_id, str) or not draft_id.strip():
                raise ValidationError("draft_id must be a non-empty saved message ID")
            overrides = [key for key in self._COMPOSE_FIELDS if key in kwargs]
            if overrides:
                raise ValidationError("With draft_id omit compose fields: " + ", ".join(overrides))
            account = self.get_account(kwargs.get("target_mailbox"))
            draft = find_message_for_account(account, draft_id)
            if not isinstance(draft, Message) or draft.is_draft is not True:
                raise ValidationError("draft_id must identify an unsent draft")
            # Verify identity before appending files to any existing draft.
            _rest_draft(self, client, draft)
        else:
            self.require(kwargs, "to", "subject", "body")
        files = asyncio.run(load_outgoing_files(
            kwargs.get("files"), storage_client=lambda: client,
        ))
        stage = "prepare_draft"
        submission_uncertain = False
        scheduled_uid = None
        scheduled_response = False
        try:
            if draft is None and files:
                account = self.get_account(kwargs.get("target_mailbox"))
                draft = Message(
                    account=account, folder=account.drafts,
                    author=Mailbox(email_address=account.primary_smtp_address),
                    subject=kwargs["subject"],
                    body=HTMLBody(_compose(kwargs["body"], kwargs.get("body_format") or "html")),
                    to_recipients=[Mailbox(email_address=v.strip()) for v in _as_addr_str(kwargs["to"]).split(";") if v.strip()],
                    cc_recipients=[Mailbox(email_address=v.strip()) for v in _as_addr_str(kwargs.get("cc")).split(";") if v.strip()],
                    bcc_recipients=[Mailbox(email_address=v.strip()) for v in _as_addr_str(kwargs.get("bcc")).split(";") if v.strip()],
                )
                attach_outgoing_files(draft, files)
                save_draft_with_attachments(draft)
                draft_id = draft.id
                draft = find_message_for_account(account, draft_id)
            elif draft is not None and files:
                stage = "append_files"
                attach_outgoing_files(draft, files)
            payload = {
                "actions": {}, "folder": "drafts", "sendImmediately": False,
                "scheduledSend": send_at, "from": client.email,
            }
            if draft is not None:
                stage = "resolve_draft"
                uid, stored = _rest_draft(self, client, draft)
                author = getattr(draft.author, "email_address", None)
                if author:
                    payload["from"] = author
                payload.update({
                    "draftUid": uid, "draftMid": stored["mid"],
                    "ownerEmailAddress": client.email, "attachmentGuid": str(uuid4()),
                    "to": stored.get("to") or "", "cc": stored.get("cc") or "",
                    "bcc": stored.get("bcc") or "", "subject": stored.get("subject") or "",
                    "messageHTML": (stored["messageHTML"] if stored.get("messageHTML") is not None
                                    else _compose(stored.get("messagePlainText") or "", "text")),
                    "originalCidLinks": stored.get("originalCidLinks") or {},
                    "excludeFiles": [], "inlineToRemove": [],
                    "readReceiptRequested": bool(stored.get("requestingReadReceipt")),
                    "deliveryReceiptRequested": bool(stored.get("requestingDeliveryReceipt")),
                    "priority": {0: 1, 1: 0, -1: 2}.get(stored.get("importance"), 1),
                })
            else:
                payload.update({
                    "to": _as_addr_str(kwargs.get("to")),
                    "cc": _as_addr_str(kwargs.get("cc")), "bcc": _as_addr_str(kwargs.get("bcc")),
                    "subject": str(kwargs["subject"]),
                    "messageHTML": _compose(kwargs["body"], kwargs.get("body_format") or "html"),
                })
            if not payload["to"] and not payload.get("cc") and not payload.get("bcc"):
                raise ValidationError("Scheduled email requires at least one recipient")
            stage = "schedule_submission"
            submission_uncertain = True
            data = self.call(client, "POST", "/api/v1/mail/message-put", json=payload)
            uid = data.get("uid")
            scheduled_uid = uid
            scheduled_response = data.get("isScheduledMessage") is True
            stage = "verify_scheduled"
            if (not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0
                    or not scheduled_response
                    or not _scheduled_contains(self, client, uid)):
                raise ToolExecutionError(
                    f"Scheduling acceptance could not be verified (returned uid={uid!r}); "
                    "inspect list_scheduled_emails before retrying to avoid duplicates"
                )
        except Exception as exc:
            recovery = draft_id or (draft.id if draft is not None else None)
            return {
                **format_error_response(exc, "Scheduling failed"),
                "draft_id": recovery, "uid": scheduled_uid,
                "folder": (SCHEDULED_FOLDER if scheduled_response else
                           "drafts" if recovery and not submission_uncertain else None),
                "stage": stage, "submission_uncertain": submission_uncertain,
                "mailbox": client.email,
                "recovery_hint": (
                    "Inspect Drafts, Scheduled and Sent before retrying; submission may have succeeded. "
                    + ("The source draft may have been consumed; appended files may remain." if recovery else
                       "No source draft was created by this tool.")
                    if submission_uncertain else
                    "No scheduling request was submitted. "
                    + ("Inspect the draft before retrying; files may already be appended." if recovery else
                       "No source draft was created by this tool.")
                ),
            }
        return format_success_response(
            f"Email scheduled for {send_at}; delivery not yet confirmed",
            uid=uid, send_at=send_at, is_scheduled=True, draft_id=draft_id,
            sent_time_utc=data.get("sentTimeUtc"), time_left_ms=data.get("timeLeftMs"),
            folder=SCHEDULED_FOLDER, mailbox=client.email, delivery_confirmed=False,
        )


class ListScheduledEmailsTool(_MailTool):
    """List messages waiting in the Scheduled folder."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "list_scheduled_emails",
            "List emails waiting in SmarterMail's Scheduled folder (uid, "
            "subject, recipients, scheduled time). Takes SmarterMail uids, "
            "not Exchange ids.",
            {
                "start_index": {
                    "type": "integer",
                    "default": 0,
                    "description": "Pagination offset (default 0).",
                },
                "count": {
                    "type": "integer",
                    "default": 50,
                    "description": "Page size (default 50).",
                },
            },
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        start = int(kwargs.get("start_index") or 0)
        count = int(kwargs.get("count") or 50)
        data = _list_folder(
            self, client, SCHEDULED_FOLDER,
            start=start, count=count, owner=client.email,
        )
        items = [_slim_message(m) for m in (data.get("results") or [])]
        for item in items:
            item["scheduled_at"] = item.get("date_sent")
        return format_success_response(
            f"{len(items)} scheduled email(s)",
            emails=items,
            total=data.get("totalCount", data.get("total", len(items))),
            folder=SCHEDULED_FOLDER,
            mailbox=client.email,
        )


class CancelScheduledEmailTool(_MailTool):
    """Request queue removal without claiming a race-free delivery guarantee."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "cancel_scheduled_email",
            "Request removal of a scheduled email by SmarterMail Scheduled-folder uid. "
            "Verifies queue removal across all pages, but cannot prove delivery was "
            "prevented: dispatch may already have begun. This is not recall. "
            "Use the uid from list_scheduled_emails, not an EWS id.",
            {
                "uid": {
                    "type": "integer",
                    "description": "SmarterMail Scheduled-folder uid.",
                },
                "permanent": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "If true, delete outright. If false (default), move "
                        "to Deleted Items."
                    ),
                },
            },
            required=["uid"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.require(kwargs, "uid")
        uid = _uid_list(kwargs.get("uid"))[0]
        permanent = bool(kwargs.get("permanent"))
        state = {
            "uid": uid, "folder": SCHEDULED_FOLDER, "mailbox": client.email,
            "cancelled": None, "cancellation_confirmed": False, "delivery_prevented": None,
            "cancel_requested": False, "delete_submitted": False,
            "removed_from_queue": None, "cleanup_confirmed": False,
            "remaining_in_scheduled_folder": None, "still_scheduled": None,
            "moved_to_deleted": None, "deleted_item_uids": [], "stage": "inspect_queue",
        }
        try:
            if not _scheduled_contains(self, client, uid):
                state["remaining_in_scheduled_folder"] = False
                state["still_scheduled"] = False
                return format_success_response(
                    "UID is not in Scheduled; no removal requested. It may already have sent. "
                    "Cancellation and delivery prevention are not confirmed.",
                    **state,
                )
            # The live provider acknowledges message-undo even for stale UIDs
            # without changing scheduledSendDate. Do not treat that as evidence.
            state.update(cancel_requested=True, delete_submitted=True, stage="delete_submission")
            data = self.call(client, "POST", "/api/v1/mail/delete-messages", json={
                "UID": [uid], "folder": SCHEDULED_FOLDER,
                "ownerEmailAddress": client.email, "moveToDeleted": not permanent,
            })
            new_uids = [value for value in (data.get("newUIDs") or [])
                        if isinstance(value, int) and not isinstance(value, bool) and value > 0]
            state["deleted_item_uids"] = new_uids
            state["stage"] = "verify_cleanup"
            remaining = _scheduled_contains(self, client, uid)
            state.update(remaining_in_scheduled_folder=remaining, still_scheduled=remaining,
                         removed_from_queue=not remaining, cleanup_confirmed=not remaining)
            if remaining:
                raise ToolExecutionError("Removal requested, but the UID remains in Scheduled")
            if not permanent and len(new_uids) == 1:
                state["moved_to_deleted"] = True
        except Exception as exc:
            return {
                **format_error_response(exc, "Scheduled removal incomplete"), **state,
                "recovery_hint": (
                    "Inspect Scheduled, Deleted Items and Sent before retrying. "
                    "The removal may have been processed; delivery prevention is unconfirmed. "
                    "This is not recall."
                ),
            }
        return format_success_response(
            "Removal requested and UID no longer in Scheduled. Dispatch may already have begun; "
            "delivery prevention is not confirmed. This is not recall.",
            **state,
        )


class UndoSendTool(_MailTool):
    """Undo submission during SmarterMail's delay-send window, not delivery."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "undo_send",
            "Undo a just-sent email while SmarterMail's delay-send window is "
            "open (user setting DelaySendOption, typically a few seconds). "
            "Requires a numeric SmarterMail REST uid, not an EWS/Graph/IMAP "
            "message ID; normal send_email does not return this uid. "
            "This is not recall of delivered mail. Reports when the window has expired.",
            {
                "uid": {
                    "type": "integer",
                    "description": "SmarterMail uid of the delayed/sent message.",
                },
                "is_scheduled": {
                    "type": "boolean",
                    "default": False,
                    "description": "True if the uid is a scheduled (not delay-send) message.",
                },
                "keep_in_folder": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, keep the message in its folder after undo.",
                },
            },
            required=["uid"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.require(kwargs, "uid")
        uid = _uid_list(kwargs.get("uid"))[0]
        payload = {
            "Uids": [uid],
            "IsScheduled": bool(kwargs.get("is_scheduled")),
            "KeepInFolder": bool(kwargs.get("keep_in_folder")),
        }
        try:
            data = self.call(client, "POST", "/api/v1/mail/message-undo", json=payload)
            if data.get("success") is not True:
                raise ToolExecutionError("Undo request was not acknowledged")
        except ToolExecutionError as exc:
            expired = bool(re.search(r"400|Bad Request", str(exc), re.I))
            error = (ToolExecutionError(
                f"Undo window expired or delay-send is inactive for uid {uid}. "
                "Delivery state is unconfirmed; this is not recall."
            ) if expired else exc)
            return {
                **format_error_response(error, "Undo request failed"),
                "uid": uid, "undo_requested": True, "undo_acknowledged": False,
                "undone": False if expired else None, "delivery_prevented": None,
                "mailbox": client.email,
            }
        return format_success_response(
            f"Undo request acknowledged for uid {uid}; the provider ACK does not prove the "
            "message was not sent. Inspect the mailbox; this is not recall.",
            uid=uid, undo_requested=True, undo_acknowledged=data.get("success") is True,
            undone=None, delivery_prevented=None,
            mailbox=client.email,
            api=data,
        )


# ---------------------------------------------------------------------------
# Bulk patch / delete / export / unsubscribe
# ---------------------------------------------------------------------------


class BulkUpdateEmailsTool(_MailTool):
    """Mark read/unread, flag, categorise, mass-delete, or purge."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "bulk_update_emails",
            "Bulk-update SmarterMail messages in one folder. Actions: "
            "mark_read, mark_unread, flag, unflag, categorize, "
            "clear_categories, delete (mass-delete), purge (expunge "
            "marked-deleted in the folder). uids are SmarterMail folder "
            "uids, not Exchange ids.",
            {
                "action": {
                    "type": "string",
                    "enum": list(_BULK_ACTIONS),
                    "description": "Bulk operation to perform.",
                },
                "folder": {
                    "type": "string",
                    "description": (
                        "Folder path or alias (inbox, sent, drafts, deleted, "
                        "junk, archive, scheduled, or a custom path)."
                    ),
                },
                "uids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "SmarterMail folder uids. Required except for purge.",
                },
                "categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Category names for action=categorize.",
                },
                "permanent": {
                    "type": "boolean",
                    "default": False,
                    "description": "For delete: skip Deleted Items (hard delete).",
                },
            },
            required=["action", "folder"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs, _BULK_ACTIONS)
        folder = _folder(kwargs)
        owner = _owner(client, kwargs)
        uids = _uid_list(kwargs.get("uids"), required=action != "purge")

        if action in ("mark_read", "mark_unread"):
            data = self.call(
                client,
                "POST",
                "/api/v1/mail/messages-patch",
                json={
                    "UID": uids,
                    "folder": folder,
                    "OwnerEmailAddress": owner,
                    "markRead": action == "mark_read",
                },
            )
            return format_success_response(
                f"{action} on {len(uids)} message(s)",
                action=action, uids=uids, folder=folder, mailbox=client.email, api=data,
            )

        if action in ("flag", "unflag"):
            data = self.call(
                client,
                "POST",
                "/api/v1/mail/messages-flag-patch",
                json={
                    "UID": uids,
                    "folder": folder,
                    "OwnerEmailAddress": owner,
                    "flagAction": {"type": "SetBasic" if action == "flag" else "Clear"},
                },
            )
            return format_success_response(
                f"{action} on {len(uids)} message(s)",
                action=action, uids=uids, folder=folder, mailbox=client.email, api=data,
            )

        if action == "categorize":
            cats = kwargs.get("categories") or []
            if isinstance(cats, str):
                cats = [cats]
            cats = [str(c) for c in cats if c]
            if not cats:
                raise ValidationError("categories required for action=categorize")
            data = self.call(
                client,
                "POST",
                "/api/v1/mail/messages-category-patch",
                json={
                    "UID": uids,
                    "folder": folder,
                    "OwnerEmailAddress": owner,
                    "categories": cats,
                },
            )
            return format_success_response(
                f"categorized {len(uids)} message(s)",
                action=action, uids=uids, categories=cats, folder=folder,
                mailbox=client.email, api=data,
            )

        if action == "clear_categories":
            data = self.call(
                client,
                "POST",
                "/api/v1/mail/messages-category-patch",
                json={
                    "UID": uids,
                    "folder": folder,
                    "OwnerEmailAddress": owner,
                    "clearCategories": True,
                },
            )
            return format_success_response(
                f"cleared categories on {len(uids)} message(s)",
                action=action, uids=uids, folder=folder, mailbox=client.email, api=data,
            )

        if action == "delete":
            if kwargs.get("permanent"):
                data = self.call(
                    client,
                    "POST",
                    "/api/v1/mail/delete-messages",
                    json={
                        "UID": uids,
                        "folder": folder,
                        "ownerEmailAddress": owner,
                        "moveToDeleted": False,
                    },
                )
            else:
                data = self.call(
                    client,
                    "POST",
                    "/api/v1/mail/delete-messages-mass",
                    json={
                        "messages": [
                            {"uid": u, "folder": folder, "ownerEmailAddress": owner}
                            for u in uids
                        ],
                    },
                )
            return format_success_response(
                f"deleted {len(uids)} message(s)",
                action=action, uids=uids, folder=folder,
                permanent=bool(kwargs.get("permanent")),
                mailbox=client.email, api=data,
            )

        # purge: expunge marked-deleted in the folder (webmail "Purge marked").
        data = self.call(
            client,
            "POST",
            "/api/v1/mail/purge-marked-deleted",
            json={"folder": folder, "ownerEmailAddress": owner},
        )
        return format_success_response(
            f"purged marked-deleted in {folder}",
            action=action, folder=folder, mailbox=client.email, api=data,
        )


class ExportEmailsTool(_MailTool):
    """Download one .eml or a zip of .eml files and write them to data_dir."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "export_emails",
            "Export SmarterMail messages as .eml (one uid) or a zip (several). "
            "Writes the bytes under the configured data_dir and returns path + "
            "size. uids are SmarterMail folder uids.",
            {
                "folder": {
                    "type": "string",
                    "description": "Folder path or alias containing the uids.",
                },
                "uids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "SmarterMail folder uids to export.",
                },
            },
            required=["folder", "uids"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        folder = _folder(kwargs)
        uids = _uid_list(kwargs.get("uids"))
        owner = _owner(client, kwargs)
        raw = self._download(
            client,
            "POST",
            "/api/v1/mail/messages-export",
            json={"OwnerEmailAddress": owner, "Folder": folder, "UID": uids},
        )
        if not raw:
            raise ToolExecutionError("messages-export returned an empty body")
        stripped = raw.lstrip()
        if stripped.startswith(b"{") and b"success" in stripped[:300]:
            try:
                err = json.loads(stripped)
            except Exception:
                err = {}
            if isinstance(err, dict) and err.get("success") is False:
                raise ToolExecutionError(
                    f"messages-export failed: {err.get('message') or err}"
                )
        ext = ".zip" if raw[:2] == b"PK" else ".eml"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if len(uids) == 1:
            filename = f"message-{uids[0]}-{stamp}{ext}"
        else:
            filename = f"messages-{len(uids)}-{stamp}{ext}"
        data_dir = Path(getattr(self.ews_client.config, "data_dir", "data") or "data")
        out_dir = data_dir / "smartermail_exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / filename
        path.write_bytes(raw)
        return format_success_response(
            f"Exported {len(uids)} message(s) ({len(raw)} bytes)",
            path=str(path.resolve()),
            bytes=len(raw),
            filename=filename,
            content_type="application/zip" if ext == ".zip" else "message/rfc822",
            uids=uids,
            folder=folder,
            mailbox=client.email,
        )


class UnsubscribeEmailTool(_MailTool):
    """Fire List-Unsubscribe (mailto / one-click / link) for a message."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "unsubscribe_email",
            "Unsubscribe using the message's List-Unsubscribe header via "
            "SmarterMail (mail/unsubscribe-message/{method}). uid is the "
            "SmarterMail folder uid. method: oneclick (default), mailto, link.",
            {
                "folder": {
                    "type": "string",
                    "description": "Folder containing the message (default inbox).",
                },
                "uid": {
                    "type": "integer",
                    "description": "SmarterMail folder uid.",
                },
                "mid": {
                    "type": "integer",
                    "description": "Optional webmail mid; mapped to uid if uid omitted.",
                },
                "method": {
                    "type": "string",
                    "enum": list(_UNSUB_METHODS),
                    "default": "oneclick",
                    "description": "Unsubscribe mechanism. Default oneclick.",
                },
            },
            required=["folder"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        folder = _folder(kwargs, default="inbox")
        uid = _resolve_uid(self, client, kwargs, folder)
        method = (kwargs.get("method") or "oneclick").strip().lower()
        if method not in _UNSUB_METHODS:
            raise ValidationError(f"method must be one of {', '.join(_UNSUB_METHODS)}")
        data = self.call(
            client,
            "POST",
            f"/api/v1/mail/unsubscribe-message/{method}",
            json={
                "uid": uid,
                "ownerEmailAddress": _owner(client, kwargs),
                "folder": folder,
            },
        )
        return format_success_response(
            f"Unsubscribe ({method}) requested for uid {uid}",
            uid=uid, method=method, folder=folder, mailbox=client.email, api=data,
        )


# ---------------------------------------------------------------------------
# Read receipts + classification (one tool, action parameter)
# ---------------------------------------------------------------------------


class ManageMailClassificationTool(_MailTool):
    """Read receipts and SpamFoo classification."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_mail_classification",
            "Read receipts and message classification. Actions: "
            "send_receipt, reject_receipt, classify, counts. Receipts take "
            "a single SmarterMail uid. classify takes uids + classification "
            "(primary/transactions/updates/promotions/unclassified/automatic). "
            "counts returns inbox classification tallies.",
            {
                "action": {
                    "type": "string",
                    "enum": list(_META_ACTIONS),
                    "description": "send_receipt, reject_receipt, classify, or counts.",
                },
                "folder": {
                    "type": "string",
                    "description": "Folder containing the uid(s). Default inbox.",
                },
                "uid": {
                    "type": "integer",
                    "description": "Single SmarterMail uid (receipts).",
                },
                "uids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "SmarterMail uids (classify).",
                },
                "mid": {
                    "type": "integer",
                    "description": "Optional webmail mid; mapped to uid if uid omitted.",
                },
                "classification": {
                    "type": "string",
                    "enum": list(_CLASSIFICATIONS),
                    "description": "Target classification for action=classify.",
                },
                "move_to_inbox": {
                    "type": "boolean",
                    "description": (
                        "For classify: move the message to Inbox. Default true "
                        "when the current folder is not inbox."
                    ),
                },
                "create_rule": {
                    "type": "boolean",
                    "default": False,
                    "description": "For classify: also create a sender rule.",
                },
            },
            required=["action"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs, _META_ACTIONS)
        owner = _owner(client, kwargs)

        if action == "counts":
            data = self.call(client, "POST", "/api/v1/mail/classification-counts", json={})
            counts = {
                "primary": data.get("primary", 0),
                "transactions": data.get("transactions", 0),
                "updates": data.get("updates", 0),
                "promotions": data.get("promotions", 0),
                "unclassified": data.get("unclassified", 0),
            }
            return format_success_response(
                "Classification counts",
                counts=counts, mailbox=client.email,
            )

        folder = _folder(kwargs, default="inbox")

        if action in ("send_receipt", "reject_receipt"):
            uid = _resolve_uid(self, client, kwargs, folder)
            path = (
                "/api/v1/mail/readreceipt/send"
                if action == "send_receipt"
                else "/api/v1/mail/readreceipt/reject"
            )
            # UID is a scalar UInt32 on these two endpoints (array 400s).
            data = self.call(
                client,
                "POST",
                path,
                json={"UID": uid, "folder": folder, "ownerEmailAddress": owner},
            )
            verb = "sent" if action == "send_receipt" else "rejected"
            return format_success_response(
                f"Read receipt {verb} for uid {uid}",
                uid=uid, folder=folder, mailbox=client.email, api=data,
            )

        # classify
        classification = (kwargs.get("classification") or "").strip().lower()
        if classification not in _CLASSIFICATIONS:
            raise ValidationError(
                f"classification must be one of {', '.join(_CLASSIFICATIONS)}"
            )
        if kwargs.get("uids") not in (None, "", []):
            uids = _uid_list(kwargs.get("uids"))
        else:
            uids = [_resolve_uid(self, client, kwargs, folder)]
        if kwargs.get("move_to_inbox") is None:
            move = folder.lower() != "inbox"
        else:
            move = bool(kwargs.get("move_to_inbox"))
        data = self.call(
            client,
            "POST",
            "/api/v1/mail/change-classification",
            json={
                "uids": uids,
                "folder": folder,
                "classification": classification,
                "moveToInbox": move,
                "createRule": bool(kwargs.get("create_rule")),
            },
        )
        return format_success_response(
            f"Classified {len(uids)} message(s) as {classification}",
            uids=uids, classification=classification, folder=folder,
            move_to_inbox=move, mailbox=client.email, api=data,
        )


SMARTERMAIL_TOOLS = [
    ScheduleEmailTool,
    ListScheduledEmailsTool,
    CancelScheduledEmailTool,
    UndoSendTool,
    BulkUpdateEmailsTool,
    ExportEmailsTool,
    UnsubscribeEmailTool,
    ManageMailClassificationTool,
]
