"""EWS (exchangelib) adapter for the provider-neutral MailBackend contract.

Wraps the existing :class:`~src.ews_client.EWSClient` so unified mailbox tools
can serve Exchange / SmarterMail accounts without touching ``src/tools``.
"""
from __future__ import annotations

import logging
import re
from functools import cached_property
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple

from exchangelib import FileAttachment, HTMLBody, Mailbox, Message
from exchangelib.errors import ErrorAccessDenied, ErrorItemNotFound, UnauthorizedError
from exchangelib.queryset import Q

from ..body_format import compose_body, render_body
from ..exceptions import AuthenticationError, EWSConnectionError, ToolExecutionError
from ..utils import (
    ews_id_to_str,
    new_outbound_tracking_id,
    find_message_for_account,
    format_datetime,
    parse_datetime_tz_aware,
    safe_get,
)
from .accounts import MailAccount
from .base import (
    WELL_KNOWN_FOLDERS,
    MailAttachmentInfo,
    MailBackend,
    MailBackendError,
    MailFolder,
    MailMessage,
    OutgoingAttachment,
    OutgoingMessage,
    preview_text,
)

if TYPE_CHECKING:
    from ..ews_client import EWSClient

_log = logging.getLogger(__name__)

_WELL_KNOWN_ATTRS = {
    "inbox": "inbox",
    "sent": "sent",
    "drafts": "drafts",
    "trash": "trash",
    "junk": "junk",
    "archive": "archive",
}

# Folder classes that are not mail (IPF.Note*). Used when walking msg_folder_root.
_NON_MAIL_CLASSES = (
    "IPF.Appointment",
    "IPF.Contact",
    "IPF.Task",
    "IPF.StickyNote",
    "IPF.Journal",
    "IPF.Configuration",
)

# Summary projection for list/search. SmarterMail leaves text_body empty on
# FindItem, so ``body`` is required for previews.
_LIST_FIELDS = (
    "id",
    "subject",
    "sender",
    "author",
    "to_recipients",
    "datetime_received",
    "datetime_sent",
    "is_read",
    "has_attachments",
    "importance",
    "conversation_id",
    "message_id",
    "text_body",
    "body",
    "size",
)

_AUTH_ERROR_NAMES = frozenset({
    "AuthenticationError",
    "UnauthorizedError",
    "ErrorAccessDenied",
    "ErrorAccountDisabled",
    "ErrorInvalidCredentials",
})


def _is_auth_error(exc: BaseException) -> bool:
    if isinstance(exc, (AuthenticationError, UnauthorizedError, ErrorAccessDenied)):
        return True
    return type(exc).__name__ in _AUTH_ERROR_NAMES


def _mailbox_email(mb: Any) -> str:
    if mb is None:
        return ""
    if isinstance(mb, str):
        return mb
    return safe_get(mb, "email_address", "") or ""


def _mailbox_name(mb: Any) -> str:
    if mb is None or isinstance(mb, str):
        return ""
    return safe_get(mb, "name", "") or ""


def _mailbox_list(values: Any) -> List[str]:
    if not values:
        return []
    out: List[str] = []
    for mb in values:
        addr = _mailbox_email(mb)
        if addr:
            out.append(addr)
    return out


def _preview_source(text_body: str, html_body: str) -> str:
    if text_body:
        return text_body
    if not html_body:
        return ""
    stripped = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_body)
    return re.sub(r"<[^>]+>", " ", stripped)


def _as_mailboxes(addresses: Iterable[str]) -> List[Mailbox]:
    return [Mailbox(email_address=addr) for addr in addresses if addr]


def _is_mail_folder(folder: Any) -> bool:
    folder_class = safe_get(folder, "folder_class", "") or ""
    if not folder_class:
        return True
    return not any(folder_class.startswith(prefix) for prefix in _NON_MAIL_CLASSES)


def _item_flagged(item: Any) -> bool:
    """True when exchangelib exposes ``item.flag.flag_status == 'Flagged'``."""
    flag = getattr(item, "flag", None)
    if flag is None:
        return False
    status = getattr(flag, "flag_status", None)
    return str(status) == "Flagged"


class EWSBackend(MailBackend):
    """MailBackend over Exchange Web Services via the shared :class:`EWSClient`."""

    kind = "ews"

    def __init__(self, account: MailAccount, ews_client: "EWSClient") -> None:
        super().__init__(account.email)
        self._mail_account = account
        self._ews_client = ews_client
        self._folder_cache: Dict[str, Any] = {}
        _log.debug(
            "EWSBackend created email=%s password=%s",
            account.email,
            "<set>" if account.password else "<missing>",
        )

    @cached_property
    def _account(self):
        """Resolve the exchangelib Account lazily (construction is network-free)."""
        try:
            return self._ews_client.get_account(self.email)
        except AuthenticationError as exc:
            raise MailBackendError(
                f"Authentication failed for {self.email}: {exc}"
            ) from exc
        except EWSConnectionError as exc:
            raise MailBackendError(
                f"Cannot connect to EWS for {self.email}: {exc}"
            ) from exc
        except Exception as exc:
            if _is_auth_error(exc):
                raise MailBackendError(
                    f"Authentication failed for {self.email}: {exc}"
                ) from exc
            raise MailBackendError(
                f"Cannot open EWS mailbox {self.email}: {exc}"
            ) from exc

    # --- errors ---------------------------------------------------------
    def _fail(self, action: str, exc: BaseException) -> MailBackendError:
        if isinstance(exc, MailBackendError):
            return exc
        if _is_auth_error(exc):
            return MailBackendError(
                f"Authentication failed for {self.email} during {action}: {exc}"
            )
        return MailBackendError(f"{action} failed: {exc}")

    # --- folders --------------------------------------------------------
    def _folder(self, token: str) -> Any:
        """Resolve a well-known alias or ``Parent/Child`` path to an EWS folder."""
        key = (token or "inbox").strip()
        cache_key = key.lower()
        cached = self._folder_cache.get(cache_key)
        if cached is not None:
            return cached

        account = self._account
        attr = _WELL_KNOWN_ATTRS.get(cache_key)
        if attr:
            folder = getattr(account, attr, None)
            if folder is not None:
                self._folder_cache[cache_key] = folder
                return folder
            if cache_key == "archive":
                folder = self._named_child(account.msg_folder_root, "Archive")
                if folder is not None:
                    self._folder_cache[cache_key] = folder
                    return folder

        folder = self._walk_to(key)
        if folder is not None:
            self._folder_cache[cache_key] = folder
            return folder

        existing = self._existing_folder_names()
        listed = ", ".join(existing[:40]) if existing else "(none)"
        raise MailBackendError(
            f"Folder {key!r} not found in {self.email}. Existing folders: {listed}"
        )

    def _named_child(self, parent: Any, name: str) -> Any:
        target = name.lower()
        try:
            children = list(getattr(parent, "children", []) or [])
        except Exception as exc:
            _log.debug("children of %s failed: %s", safe_get(parent, "name", "?"), exc)
            return None
        for child in children:
            if (safe_get(child, "name", "") or "").lower() == target:
                return child
        return None

    def _walk_to(self, token: str) -> Any:
        parts = [p for p in token.split("/") if p.strip()]
        if not parts:
            return None
        account = self._account
        root = account.msg_folder_root
        current = root
        for part in parts:
            found = self._named_child(current, part)
            if found is None:
                # Single-segment names may live anywhere under the mail root.
                if len(parts) == 1:
                    return self._find_by_name(root, part)
                return None
            current = found
        return current if current is not root else None

    def _find_by_name(self, parent: Any, name: str) -> Any:
        target = name.lower()
        for folder, _path in self._iter_mail_folders(parent):
            if (safe_get(folder, "name", "") or "").lower() == target:
                return folder
        return None

    def _iter_mail_folders(
        self, root: Optional[Any] = None,
    ) -> Iterable[Tuple[Any, str]]:
        account = self._account
        start = root if root is not None else account.msg_folder_root
        try:
            children = list(getattr(start, "children", []) or [])
        except Exception as exc:
            _log.debug("mail-folder walk failed at root: %s", exc)
            return
        stack: List[Tuple[Any, str]] = []
        for child in reversed(children):
            name = safe_get(child, "name", "") or ""
            stack.append((child, name))
        seen = set()
        while stack:
            folder, path = stack.pop()
            fid = ews_id_to_str(safe_get(folder, "id", None)) or id(folder)
            if fid in seen:
                continue
            seen.add(fid)
            if _is_mail_folder(folder):
                yield folder, path
            try:
                grandchildren = list(getattr(folder, "children", []) or [])
            except Exception as exc:
                _log.debug("skipping children of %s: %s", path, exc)
                continue
            for child in reversed(grandchildren):
                cname = safe_get(child, "name", "") or ""
                child_path = f"{path}/{cname}" if path else cname
                stack.append((child, child_path))

    def _existing_folder_names(self) -> List[str]:
        names = list(WELL_KNOWN_FOLDERS)
        try:
            for _folder, path in self._iter_mail_folders():
                if path and path not in names:
                    names.append(path)
        except Exception as exc:
            _log.debug("listing existing folders failed: %s", exc)
        return names

    def _well_known_ids(self) -> Dict[str, str]:
        account = self._account
        mapping: Dict[str, str] = {}
        for alias, attr in _WELL_KNOWN_ATTRS.items():
            folder = getattr(account, attr, None)
            if folder is None and alias == "archive":
                folder = self._named_child(account.msg_folder_root, "Archive")
            fid = ews_id_to_str(safe_get(folder, "id", None)) if folder is not None else None
            if fid:
                mapping[fid] = alias
        return mapping

    # --- items ----------------------------------------------------------
    def _get_item(self, message_id: str, folder_token: str) -> Any:
        folder = self._folder(folder_token)
        try:
            item = folder.get(id=message_id)
            if item is not None:
                return item
        except ErrorItemNotFound:
            pass
        except Exception as exc:
            _log.debug("folder.get(%s) in %s failed: %s", message_id, folder_token, exc)
        # find_message_across_folders(ews_client, id) always searches the
        # *primary* EWSClient.account, which is the wrong mailbox for extra
        # accounts. find_message_for_account uses the resolved Account.
        try:
            return find_message_for_account(self._account, message_id)
        except ToolExecutionError as exc:
            raise MailBackendError(str(exc)) from exc
        except Exception as exc:
            raise self._fail(f"get message {message_id}", exc) from exc

    def _to_message(
        self,
        item: Any,
        folder_token: str,
        *,
        with_body: bool = False,
        body_format: str = "markdown",
    ) -> MailMessage:
        sender = safe_get(item, "sender", None) or safe_get(item, "author", None)
        text_body = safe_get(item, "text_body", "") or ""
        html_body = str(safe_get(item, "body", "") or "")
        preview_src = _preview_source(text_body, html_body)
        importance = safe_get(item, "importance", None)
        conv = safe_get(item, "conversation_id", None)
        attachments: List[MailAttachmentInfo] = []
        body: Optional[str] = None
        actual_format: Optional[str] = None
        if with_body:
            try:
                body, actual_format = render_body(html_body, text_body, body_format)
            except ValueError as exc:
                raise MailBackendError(str(exc)) from exc
            attachments = self._attachment_info(item)
        return MailMessage(
            id=ews_id_to_str(safe_get(item, "id", None)) or "",
            folder=folder_token,
            subject=safe_get(item, "subject", "") or "",
            from_address=_mailbox_email(sender),
            from_name=_mailbox_name(sender),
            to=_mailbox_list(safe_get(item, "to_recipients", [])),
            cc=_mailbox_list(safe_get(item, "cc_recipients", [])) if with_body else [],
            bcc=_mailbox_list(safe_get(item, "bcc_recipients", [])) if with_body else [],
            reply_to=_mailbox_list(safe_get(item, "reply_to", [])) if with_body else [],
            received=format_datetime(safe_get(item, "datetime_received", None)),
            sent=format_datetime(safe_get(item, "datetime_sent", None)),
            is_read=bool(safe_get(item, "is_read", False)),
            is_flagged=_item_flagged(item),
            has_attachments=bool(safe_get(item, "has_attachments", False)),
            importance=str(importance) if importance is not None else None,
            internet_message_id=safe_get(item, "message_id", None),
            conversation_id=ews_id_to_str(conv) if conv is not None else None,
            preview=preview_text(preview_src),
            body=body,
            body_format=actual_format,
            attachments=attachments,
            size=safe_get(item, "size", None),
        )

    def _attachment_info(self, item: Any) -> List[MailAttachmentInfo]:
        # Metadata only — never touch ``content`` / ``_content`` (that downloads).
        out: List[MailAttachmentInfo] = []
        try:
            attachments = safe_get(item, "attachments", []) or []
        except Exception as exc:
            _log.debug("reading attachment metadata failed: %s", exc)
            return out
        for att in attachments:
            try:
                att_id = ews_id_to_str(
                    safe_get(att, "attachment_id", None) or safe_get(att, "id", None)
                )
                out.append(
                    MailAttachmentInfo(
                        name=safe_get(att, "name", "") or "",
                        content_type=safe_get(att, "content_type", None),
                        size=safe_get(att, "size", None),
                        attachment_id=att_id,
                        is_inline=bool(safe_get(att, "is_inline", False)),
                    )
                )
            except Exception as exc:
                _log.debug("skipping attachment: %s", exc)
        return out

    def _aware(self, dt_str: Optional[str], label: str) -> Any:
        if not dt_str:
            return None
        parsed = parse_datetime_tz_aware(dt_str)
        if parsed is None:
            raise MailBackendError(
                f"Invalid {label} datetime {dt_str!r}; expected ISO-8601"
            )
        tz = getattr(self._account, "default_timezone", None)
        if tz is None:
            return parsed
        try:
            converted = parsed.astimezone(tz)
            return type(parsed)(
                converted.year,
                converted.month,
                converted.day,
                converted.hour,
                converted.minute,
                converted.second,
                converted.microsecond,
                tzinfo=tz,
            )
        except Exception:
            return parsed

    def _paginate(self, qs: Any, limit: int, offset: int = 0) -> List[Any]:
        limit = max(0, int(limit))
        offset = max(0, int(offset))
        if limit == 0:
            return []
        try:
            qs = qs.only(*_LIST_FIELDS)
        except Exception as exc:
            _log.debug("QuerySet.only() rejected: %s", exc)
        try:
            qs = qs.order_by("-datetime_received")
        except Exception as exc:
            _log.debug("order_by datetime_received failed: %s", exc)
        try:
            return list(qs[offset : offset + limit])
        except Exception as exc:
            raise self._fail("list/search messages", exc) from exc

    # --- discovery ------------------------------------------------------
    def test_connection(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "connected": False,
            "backend": self.kind,
            "email": self.email,
        }
        try:
            account = self._account
            _ = account.inbox.total_count
            result["connected"] = True
            try:
                result["folder_count"] = int(
                    getattr(account.msg_folder_root, "child_folder_count", 0) or 0
                )
            except Exception as exc:
                _log.debug("folder_count unavailable: %s", exc)
        except Exception as exc:
            result["error"] = str(exc)
            _log.debug("test_connection failed for %s: %s", self.email, exc)
        return result

    def list_folders(self) -> List[MailFolder]:
        try:
            known = self._well_known_ids()
            folders: List[MailFolder] = []
            for folder, path in self._iter_mail_folders():
                try:
                    fid = ews_id_to_str(safe_get(folder, "id", None)) or str(
                        safe_get(folder, "id", "") or path
                    )
                    folders.append(
                        MailFolder(
                            id=fid,
                            name=safe_get(folder, "name", "") or path,
                            path=path,
                            well_known=known.get(fid),
                            total_count=safe_get(folder, "total_count", None),
                            unread_count=safe_get(folder, "unread_count", None),
                        )
                    )
                except Exception as exc:
                    _log.debug("skipping folder %s: %s", path, exc)
            return folders
        except MailBackendError:
            raise
        except Exception as exc:
            raise self._fail("list folders", exc) from exc

    # --- read -----------------------------------------------------------
    def list_messages(
        self,
        folder: str = "inbox",
        limit: int = 25,
        offset: int = 0,
        unread_only: bool = False,
    ) -> List[MailMessage]:
        try:
            ews_folder = self._folder(folder)
            qs = ews_folder.all()
            if unread_only:
                qs = qs.filter(is_read=False)
            items = self._paginate(qs, limit, offset)
            return [self._to_message(item, folder) for item in items]
        except MailBackendError:
            raise
        except Exception as exc:
            raise self._fail(f"list messages in {folder}", exc) from exc

    def get_message(
        self,
        message_id: str,
        folder: str = "inbox",
        body_format: str = "markdown",
    ) -> MailMessage:
        if not message_id:
            raise MailBackendError("message_id is required")
        try:
            item = self._get_item(message_id, folder)
            return self._to_message(
                item, folder, with_body=True, body_format=body_format,
            )
        except MailBackendError:
            raise
        except Exception as exc:
            raise self._fail(f"get message {message_id}", exc) from exc

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
        """Server-side search.

        exchangelib 5.0.3 *does* accept EWS QueryString via
        ``folder.filter("text")``, but SmarterMail honours it as a no-op
        (unfiltered newest-first). Free-text therefore uses a Restriction:
        ``Q(subject__icontains) | Q(body__contains)``.
        """
        try:
            ews_folder = self._folder(folder)
            query = (query or "").strip() or None
            qs = ews_folder.all()
            if query:
                qs = qs.filter(
                    Q(subject__icontains=query) | Q(body__contains=query)
                )
            if subject:
                qs = qs.filter(subject__icontains=subject)
            if from_address:
                try:
                    qs = qs.filter(sender__contains=from_address)
                except Exception:
                    qs = qs.filter(sender=from_address)
            since_dt = self._aware(since, "since")
            if since_dt is not None:
                qs = qs.filter(datetime_received__gte=since_dt)
            before_dt = self._aware(before, "before")
            if before_dt is not None:
                qs = qs.filter(datetime_received__lt=before_dt)
            if unread_only:
                qs = qs.filter(is_read=False)
            if has_attachments is not None:
                qs = qs.filter(has_attachments=bool(has_attachments))
            items = self._paginate(qs, limit, 0)
            return [self._to_message(item, folder) for item in items]
        except MailBackendError:
            raise
        except Exception as exc:
            raise self._fail(f"search messages in {folder}", exc) from exc

    # --- write ----------------------------------------------------------
    def _attach_outgoing(
        self, item: Message, attachments: List[OutgoingAttachment]
    ) -> None:
        """Copy files onto an exchangelib item before send.

        ``Item.attach`` issues CreateAttachment; appending to the local
        list would drop the files on the floor.
        """
        for att in attachments:
            kwargs: Dict[str, Any] = {
                "name": att.name,
                "content": att.content,
                "content_type": att.content_type,
                "is_inline": att.is_inline,
            }
            if att.content_id:
                kwargs["content_id"] = att.content_id
            item.attach(FileAttachment(**kwargs))

    def send_message(self, message: OutgoingMessage) -> Dict[str, Any]:
        """Send a new message or a reply.

        The tracking header rides on freshly created messages only: EWS
        ``CreateItem`` replies are built from the original item and take no
        extended properties, so a reply reports ``tracking_id: None`` rather
        than an ID that never reached the wire.
        """
        if not message.to and not message.reply_to_message_id:
            raise MailBackendError("send_message requires at least one recipient")
        try:
            html, _actual = compose_body(message.body or "", message.body_format or "markdown")
        except ValueError as exc:
            raise MailBackendError(str(exc)) from exc
        account = self._account
        subject = message.subject or ""
        replied_to: Optional[str] = None
        tracking_id: Optional[str] = None
        try:
            if message.reply_to_message_id:
                original = self._get_item(
                    message.reply_to_message_id,
                    message.reply_to_folder or "inbox",
                )
                replied_to = ews_id_to_str(safe_get(original, "id", None))
                if not subject:
                    original_subject = safe_get(original, "subject", "") or ""
                    subject = (
                        original_subject
                        if original_subject.lower().startswith("re:")
                        else f"Re: {original_subject}"
                    )
                body = HTMLBody(html)
                if message.reply_all:
                    reply_item = original.create_reply_all(subject=subject, body=body)
                else:
                    reply_item = original.create_reply(
                        subject=subject,
                        body=body,
                        to_recipients=_as_mailboxes(message.to) or None,
                        cc_recipients=_as_mailboxes(message.cc) or None,
                        bcc_recipients=_as_mailboxes(message.bcc) or None,
                    )
                if message.attachments:
                    self._attach_outgoing(reply_item, message.attachments)
                reply_item.send()
            else:
                # No folder= here: exchangelib files the saved copy in the
                # folder the item was created in, so naming Drafts would
                # leave every sent mail sitting in Drafts.
                # ``author`` maps to the writable EWS ``message:From``.
                # SmarterMail accepts a CreateItem without it and then emits
                # RFC822 mail with no From header, which bounces downstream.
                tracking_id = message.tracking_id or new_outbound_tracking_id()
                msg = Message(
                    account=account,
                    author=Mailbox(email_address=account.primary_smtp_address),
                    outbound_tracking_id=tracking_id,
                    subject=subject,
                    body=HTMLBody(html),
                    to_recipients=_as_mailboxes(message.to),
                    cc_recipients=_as_mailboxes(message.cc) or None,
                    bcc_recipients=_as_mailboxes(message.bcc) or None,
                )
                if message.attachments:
                    self._attach_outgoing(msg, message.attachments)
                msg.send_and_save()
            return {
                "sent": True,
                "backend": self.kind,
                "to": list(message.to or []),
                "subject": subject,
                "replied_to": replied_to,
                "tracking_id": tracking_id,
            }
        except MailBackendError:
            raise
        except Exception as exc:
            raise self._fail("send message", exc) from exc

    def move_message(
        self, message_id: str, destination: str, folder: str = "inbox",
    ) -> Dict[str, Any]:
        if not message_id:
            raise MailBackendError("message_id is required")
        if not destination:
            raise MailBackendError("destination folder is required")
        try:
            item = self._get_item(message_id, folder)
            target = self._folder(destination)
            item.move(target)
            new_id = ews_id_to_str(safe_get(item, "id", None)) or message_id
            return {
                "moved": True,
                "backend": self.kind,
                "id": new_id,
                "source": folder,
                "destination": destination,
            }
        except MailBackendError:
            raise
        except Exception as exc:
            raise self._fail(f"move message {message_id}", exc) from exc

    def delete_message(
        self, message_id: str, folder: str = "inbox", hard_delete: bool = False,
    ) -> Dict[str, Any]:
        if not message_id:
            raise MailBackendError("message_id is required")
        try:
            item = self._get_item(message_id, folder)
            if hard_delete:
                item.delete()
            else:
                item.move_to_trash()
            return {
                "deleted": True,
                "hard_delete": bool(hard_delete),
                "backend": self.kind,
                "id": message_id,
                "folder": folder,
            }
        except MailBackendError:
            raise
        except Exception as exc:
            raise self._fail(f"delete message {message_id}", exc) from exc

    def mark_message(
        self,
        message_id: str,
        folder: str = "inbox",
        is_read: Optional[bool] = None,
        is_flagged: Optional[bool] = None,
    ) -> Dict[str, Any]:
        if not message_id:
            raise MailBackendError("message_id is required")
        if is_read is None and is_flagged is None:
            return {"changed": False, "backend": self.kind, "id": message_id}
        try:
            item = self._get_item(message_id, folder)
            update_fields: List[str] = []
            result: Dict[str, Any] = {
                "changed": False,
                "backend": self.kind,
                "id": message_id,
                "folder": folder,
            }
            if is_read is not None:
                item.is_read = bool(is_read)
                update_fields.append("is_read")
                result["is_read"] = bool(is_read)
            if is_flagged is not None:
                # exchangelib 5.0.3 has no Message.flag; don't crash.
                if not hasattr(item, "flag"):
                    result["flag_supported"] = False
                else:
                    try:
                        status = "Flagged" if is_flagged else "NotFlagged"
                        flag_obj = getattr(item, "flag", None)
                        if flag_obj is not None and hasattr(flag_obj, "flag_status"):
                            flag_obj.flag_status = status
                        else:
                            item.flag = status
                        update_fields.append("flag")
                        result["is_flagged"] = bool(is_flagged)
                    except Exception as exc:
                        _log.debug("flag update unsupported: %s", exc)
                        result["flag_supported"] = False
            if update_fields:
                item.save(update_fields=update_fields)
                result["changed"] = True
            return result
        except MailBackendError:
            raise
        except Exception as exc:
            raise self._fail(f"mark message {message_id}", exc) from exc

    def close(self) -> None:
        self._folder_cache.clear()
        # Shared EWSClient owns the protocol sockets; do not close them here.
