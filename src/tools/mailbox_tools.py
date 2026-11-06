"""One public mail-CRUD surface for every configured mailbox.

``email_tools.py`` / ``folder_tools.py`` / ``attachment_tools.py`` hold the
rich Exchange (EWS) implementations. Each mail-CRUD and attachment tool is
subclassed here exactly once so a single public tool name serves any mailbox:
``target_mailbox`` is resolved through the mailbox registry and, when it names
a mailbox served by IMAP (iCloud) or Microsoft Graph (Outlook.com / Microsoft
365), the call runs against that provider's :class:`MailBackend` instead of
EWS. An address that is not a configured mailbox keeps its old meaning — EWS
impersonation / delegate access.

There are therefore no per-provider duplicates of these tools. Adding a new
provider means adding a ``MailBackend`` (and a ``provider`` spelling in
``mail_backends/accounts.py``), never a new tool set.

Exchange-only features (calendar, contacts, tasks, rules, OOF, meetings,
voice, commitments, bulk/conversation reads, ...) are not routed:
:meth:`src.tools.base.BaseTool.get_account` refuses a non-Exchange mailbox
with a clear error rather than pretending to support it.
"""
from __future__ import annotations

import asyncio
import base64
import email
import email.policy
from typing import Any, Dict, List, Optional, Tuple

from ..exceptions import ToolExecutionError, ValidationError
from ..mail_backends.accounts import MailAccount
from ..mail_backends.base import (
    ALL_FOLDERS_EXCLUDED,
    BODY_FORMATS,
    WELL_KNOWN_FOLDERS,
    MailBackend,
    MailBackendError,
    OutgoingAttachment,
    OutgoingMessage,
    normalize_folder,
)
from ..utils import (
    format_success_response,
    format_error_response,
    new_outbound_tracking_id,
    project_fields,
)
from ..attachment_sources import (
    REMOTE_SOURCE_PROPERTIES,
    load_attachment_content,
    resolve_content_type,
)
from .base import BaseTool
from .outgoing_attachments import (
    FILES_SCHEMA,
    VERIFY_FILES_SCHEMA,
    dry_run_file_report,
    dry_run_schema,
    file_summaries as _file_summaries,
    load_outgoing_files,
    validate_outgoing_files,
)
from .email_tools import (
    DeleteEmailTool as _ExchangeDeleteEmailTool,
    ForwardEmailTool as _ExchangeForwardEmailTool,
    GetEmailDetailsTool as _ExchangeGetEmailDetailsTool,
    MoveEmailTool as _ExchangeMoveEmailTool,
    ReadEmailsTool as _ExchangeReadEmailsTool,
    ReplyEmailTool as _ExchangeReplyEmailTool,
    SearchEmailsTool as _ExchangeSearchEmailsTool,
    SendEmailTool as _ExchangeSendEmailTool,
    UpdateEmailTool as _ExchangeUpdateEmailTool,
    address_list as _as_list,
    normalize_flag_status,
    reject_other_mode_params,
    search_mode,
    search_param_is_default,
)
from .email_tools_draft import (
    CreateDraftTool as _ExchangeCreateDraftTool,
    CreateReplyDraftTool as _ExchangeCreateReplyDraftTool,
    CreateForwardDraftTool as _ExchangeCreateForwardDraftTool,
    UpdateDraftTool as _ExchangeUpdateDraftTool,
)
from .folder_tools import ListFoldersTool as _ExchangeListFoldersTool
from .attachment_tools import (
    ARCHIVE_EXTENSIONS,
    DEFAULT_DOWNLOAD_MAX_BYTES,
    DEFAULT_READ_ATTACHMENT_CHARS,
    SIGNATURE_EXTENSIONS,
    AddAttachmentTool as _ExchangeAddAttachmentTool,
    AttachEmailToDraftTool as _ExchangeAttachEmailToDraftTool,
    DeleteAttachmentTool as _ExchangeDeleteAttachmentTool,
    DownloadAttachmentTool as _ExchangeDownloadAttachmentTool,
    GetEmailMimeTool as _ExchangeGetEmailMimeTool,
    ListAttachmentsTool as _ExchangeListAttachmentsTool,
    ReadAttachmentTool as _ExchangeReadAttachmentTool,
    _extract_with_password_fallback,
    clip_extracted_text,
    inline_content_id,
    refuse_oversize_bytes,
    resolve_download_path,
)
from ..attachment_sniff import extension_from_filename, find_signed_part, needs_detached_content
from ..body_format import DEFAULT_READ_FORMAT


# Every tool name that serves any mailbox regardless of provider.
ROUTED_TOOL_NAMES = (
    "list_mailboxes",
    "list_folders",
    "read_emails",
    "search_emails",
    "get_email_details",
    "send_email",
    "reply_email",
    "forward_email",
    "create_draft",
    "create_reply_draft",
    "create_forward_draft",
    "update_draft",
    "move_email",
    "delete_email",
    "update_email",
    "list_attachments",
    "download_attachment",
    "add_attachment",
    "delete_attachment",
    "read_attachment",
    "get_email_mime",
    "attach_email_to_draft",
)

_MAILBOX_DESC = (
    "Mailbox to operate on. Any configured mailbox address (call "
    "list_mailboxes) — Exchange, iCloud/IMAP and Outlook/Graph are routed "
    "automatically — or another Exchange mailbox you have impersonation / "
    "delegate access to. Default: the primary mailbox. Alias: 'mailbox'."
)

_FOLDER_DESC = (
    "For IMAP, the folder containing this message ID (default inbox): "
    + ", ".join(WELL_KNOWN_FOLDERS) + " or the native id/path from list_folders. "
    "Supply the actual folder for messages outside inbox. "
    "Exchange and Graph address the message by ID."
)

_OFFSET_DESC = (
    "Skip this many messages before returning results (non-Exchange "
    "mailboxes; Exchange paging lives in search_emails)."
)

_CLEANUP_ON_FAILURE = {
    "type": "boolean",
    "default": False,
    "description": (
        "Graph workflows and IMAP draft creation: attempt to delete a newly-created "
        "draft if preparation fails before submission. Default preserves it for recovery. Never deletes "
        "a caller-supplied draft or a draft whose submission is uncertain."
    ),
}


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _body_format(value: Any, default: str = "markdown") -> str:
    fmt = str(value or default).strip().lower()
    if fmt not in BODY_FORMATS:
        raise ValidationError(
            f"body format must be one of {', '.join(BODY_FORMATS)}; got {fmt!r}"
        )
    return fmt


_ROUTED_FILES_HINT = (
    "Use files entries with file_content (+ file_name), file_url, or the "
    "file_path returned by stage_outgoing_file."
)


async def _routed_files(
    kwargs: Dict[str, Any], dry_run: bool
) -> Tuple[List[Dict[str, Any]], Optional[List[OutgoingAttachment]]]:
    """Validated ``files`` entries, plus their bytes unless this is a bare dry run."""
    entries = validate_outgoing_files(kwargs.get("files"))
    if dry_run and not kwargs.get("verify_files"):
        return entries, None
    return entries, await load_outgoing_files(entries)


class RoutedMailTool(BaseTool):
    """Mixin: run against ``target_mailbox``'s backend, else fall back to EWS.

    Subclasses mix this in *before* the Exchange tool so ``get_schema`` /
    ``execute`` are intercepted here and delegated to the Exchange
    implementation via ``super()`` whenever the mailbox speaks EWS.
    """

    #: input-schema properties only the non-Exchange path consumes
    routed_properties: Dict[str, Any] = {}

    @property
    def registry(self):
        return self.ews_client.mailbox_registry

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------
    def get_schema(self) -> Dict[str, Any]:
        schema = super().get_schema()
        properties = schema["inputSchema"].setdefault("properties", {})
        properties["target_mailbox"] = {
            "type": "string",
            "description": _MAILBOX_DESC,
        }
        for name, spec in self.routed_properties.items():
            properties.setdefault(name, spec)
        for name in ("attachments", "inline_attachments"):
            if name in properties:
                properties[name] = {
                    **properties[name],
                    "deprecated": True,
                    "description": (
                        "Deprecated Exchange-only compatibility input. Prefer files; "
                        "file_path for server-accessible files, file_url or file_content "
                        "for remote agents."
                    ),
                }
        return schema

    # ------------------------------------------------------------------
    # routing
    # ------------------------------------------------------------------
    def _resolve(self, kwargs: Dict[str, Any]) -> Optional[Tuple[MailAccount, MailBackend]]:
        """Return ``(account, backend)`` for a non-EWS mailbox, else ``None``.

        Normalises the ``mailbox`` alias into ``target_mailbox`` in place so
        the Exchange path keeps seeing the parameter it has always used.
        """
        address = kwargs.pop("mailbox", None) or kwargs.get("target_mailbox")
        address = (address or "").strip() or None
        if address:
            kwargs["target_mailbox"] = address

        account = self.registry.find_account(address)
        if account is None or account.kind == "ews":
            return None
        return account, self.registry.get_backend(account.email)

    async def execute(self, **kwargs) -> Dict[str, Any]:
        routed = self._resolve(kwargs)
        if routed is None:
            return await super().execute(**kwargs)
        account, backend = routed
        # Drop the addressing keys the routed implementations never read.
        kwargs.pop("target_mailbox", None)
        try:
            return await self.execute_routed(account, backend, **kwargs)
        except MailBackendError as exc:
            details = dict(getattr(exc, "details", {}) or {})
            return {
                **details,
                **self.mailbox_info(account),
                **format_error_response(exc, account.email),
                "details": details,
            }

    async def execute_routed(
        self, account: MailAccount, backend: MailBackend, **kwargs
    ) -> Dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    # ------------------------------------------------------------------
    # helpers for the routed path
    # ------------------------------------------------------------------
    async def call(self, func, *args, **kwargs) -> Any:
        """Run a blocking backend call without stalling the event loop."""
        return await asyncio.to_thread(func, *args, **kwargs)

    def mailbox_info(self, account: MailAccount) -> Dict[str, Any]:
        return {
            "mailbox": account.email,
            "provider": account.provider,
            "backend": account.kind,
        }

    def routed_response(
        self,
        message: str,
        account: MailAccount,
        result: Optional[Dict[str, Any]] = None,
        **fields: Any,
    ) -> Dict[str, Any]:
        """Build the success payload from one merged mapping.

        Mailbox identity, the tool's own fields and the backend result all
        share keys (``backend``, ``folder``, ``replied_to``,
        ``attachment_name`` …), and two ``**`` splats sharing a key is a hard
        ``TypeError``. Merging into a single dict first makes that impossible.
        The backend result wins, because it reports what actually happened
        (e.g. the new message id after an IMAP draft rewrite).
        """
        payload = dict(self.mailbox_info(account))
        payload.update(fields)
        payload.update(result or {})
        # Mutations may replace the message (IMAP rewrite or provider move).
        # Keep every public identity alias on the backend's returned object,
        # including an explicit None when a destination UID is unknown.
        if result is not None and "id" in result:
            requested_id = fields.get("message_id", fields.get("draft_id"))
            if requested_id is not None:
                payload["message_id"] = result["id"]
                if "draft_id" in fields:
                    payload["draft_id"] = result["id"]
                if result["id"] != requested_id:
                    payload.setdefault("previous_id", requested_id)
        return format_success_response(message, **payload)

    def reject_exchange_only(
        self,
        account: MailAccount,
        kwargs: Dict[str, Any],
        params: Tuple[str, ...],
        hint: str = "",
    ) -> None:
        """Fail clearly when a caller asks for an EWS-only capability."""
        used = [p for p in params if kwargs.get(p) not in (None, "", [], False)]
        if not used:
            return
        raise ValidationError(
            f"{', '.join(used)}: not supported for {account.email} "
            f"({account.provider} over {account.kind.upper()}) — Exchange-only."
            + (f" {hint}" if hint else "")
        )


class ListMailboxesTool(BaseTool):
    """Inventory of every configured mailbox and the protocol it uses."""

    @property
    def registry(self):
        return self.ews_client.mailbox_registry

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "list_mailboxes",
            "description": (
                "List every configured mailbox with the protocol serving it "
                "(ews = Exchange/SmarterMail, imap = iCloud or generic IMAP, "
                "graph = Outlook.com / Microsoft 365). Pass "
                "test_connection=true to authenticate each one. Start here, "
                "then pass 'target_mailbox' to any mail tool."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "test_connection": {
                        "type": "boolean",
                        "default": False,
                        "description": "Authenticate against each mailbox and report the result.",
                    }
                },
                "required": [],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        accounts = self.registry.describe_accounts()
        native = getattr(self.ews_client, "smartermail", None)
        for info in accounts:
            account = self.registry.find_account(info["email"])
            kind = info["backend"]
            schedule = kind == "ews" and bool(getattr(native, "enabled", False))
            has_caldav = bool(account and (account.get("caldav_url") or account.provider in {"icloud", "apple"}))
            has_carddav = bool(account and (account.get("carddav_url") or account.provider in {"icloud", "apple"}))
            info["capabilities"] = {
                "drafts_create": True,
                "update_draft": True,
                "calendar": kind in ("ews", "graph") or (kind == "imap" and has_caldav),
                "contacts": kind in ("ews", "graph") or (kind == "imap" and has_carddav),
                "schedule": schedule,
                "files": True,
                "folder_scoped_ids": kind == "imap",
            }
            hints = {
                "support": "Configured implementation support; authentication and service permissions are not verified.",
                "files": "Canonical files input is supported; SmarterMail storage_file_id requires its native service.",
            }
            if kind == "graph":
                hints["consent"] = "Mail needs Mail.ReadWrite/Mail.Send; calendar needs Calendars.ReadWrite; contacts need Contacts.ReadWrite. Re-consent and include those scopes in account.settings.graph_scope."
                hints["calendar_contacts"] = "Calendar and personal-contact tools are implemented. Organization free/busy depends on account type and permissions. Directory lookup and communication-history analytics are not implemented."
            elif kind == "imap":
                hints["folder_scoped_ids"] = "Keep the returned folder with every ID; draft edits replace IDs and attachment part IDs."
                if has_caldav or has_carddav:
                    hints["calendar_contacts"] = (
                        "iCloud/DAV calendar and contacts use CalDAV/CardDAV resource hrefs, VEVENT/VCARD UIDs and ETags; "
                        "they never use IMAP UIDs. Event/contact CRUD is supported. Organization free/busy, meeting-time "
                        "search and contact analytics are unavailable."
                    )
                else:
                    hints["calendar_contacts"] = "IMAP does not expose calendar/contacts; configure CalDAV/CardDAV URLs for provider-native support."
            if schedule:
                hints["schedule"] = "Configured SmarterMail REST; requires native SmarterMail endpoint/credentials, not generic Exchange."
            else:
                hints["schedule"] = "Only the configured SmarterMail REST service supports scheduling."
            info["capability_hints"] = hints
        if kwargs.get("test_connection"):
            for info in accounts:
                try:
                    backend = self.registry.get_backend(info["email"])
                    info["connection"] = await asyncio.to_thread(backend.test_connection)
                except MailBackendError as exc:
                    info["connection"] = {"connected": False, "error": str(exc)}
        return format_success_response(
            f"{len(accounts)} mailbox(es) configured",
            mailboxes=accounts,
            default_mailbox=self.registry.default_email,
            mail_tools=list(ROUTED_TOOL_NAMES),
            note=(
                "Mail and attachment tools above route across providers; outside "
                "Exchange, attachment changes require a draft. send_email can "
                "send an existing unchanged draft. Draft creation and partial updates "
                "are routed too; use returned IDs and folders after edits. Calendar "
                "and contact tools also support Graph and DAV-capable iCloud/IMAP "
                "accounts, subject to provider credentials/scopes. Tasks, rules, "
                "OOF and bulk/conversation reads remain Exchange-only. "
                "SmarterMail REST extras, including scheduling "
                "and undo_send, require a SmarterMail mailbox."
            ),
        )


class ListFoldersTool(RoutedMailTool, _ExchangeListFoldersTool):
    """list_folders for any mailbox."""

    async def execute_routed(self, account, backend, **kwargs) -> Dict[str, Any]:
        self.reject_exchange_only(
            account,
            kwargs,
            ("parent_folder_id",),
            hint="Non-Exchange mailboxes return the full flat folder list.",
        )
        parent = (kwargs.get("parent_folder") or "root").strip().lower()
        if parent not in ("", "root"):
            raise ValidationError(
                f"parent_folder={parent!r}: {account.email} "
                f"({account.provider}) lists the whole mailbox at once; "
                f"omit parent_folder."
            )

        folders = await self.call(backend.list_folders)
        include_counts = bool(kwargs.get("include_counts", True))
        listing: List[Dict[str, Any]] = []
        for folder in folders:
            info = folder.to_dict()
            if not include_counts:
                info.pop("total_count", None)
                info.pop("unread_count", None)
            listing.append(info)

        return self.routed_response(
            f"Listed {len(listing)} folder(s)",
            account,
            folders=listing,
            total_folders=len(listing),
            parent_folder="root",
        )


class ReadEmailsTool(RoutedMailTool, _ExchangeReadEmailsTool):
    """read_emails for any mailbox."""

    routed_properties = {
        "offset": {
            "type": "integer",
            "minimum": 0,
            "default": 0,
            "description": _OFFSET_DESC,
        }
    }

    async def execute_routed(self, account, backend, **kwargs) -> Dict[str, Any]:
        folder = normalize_folder(kwargs.get("folder"))
        limit = _clamp(kwargs.get("max_results"), 1, 1000, 50)
        offset = _clamp(kwargs.get("offset"), 0, 100000, 0)
        messages = await self.call(
            backend.list_messages,
            folder,
            limit,
            offset,
            bool(kwargs.get("unread_only", False)),
        )
        emails = [m.to_dict() for m in messages]
        return self.routed_response(
            f"Retrieved {len(emails)} email(s)",
            account,
            emails=emails,
            total_count=len(emails),
            folder=folder,
            offset=offset,
        )


class SearchEmailsTool(RoutedMailTool, _ExchangeSearchEmailsTool):
    """search_emails for any mailbox (quick / full-text modes)."""

    # ``mailbox`` is the documented alias of ``target_mailbox``; the Exchange
    # tool rejects unknown params, so it has to be part of the vocabulary.
    _ALLOWED_PARAMS = _ExchangeSearchEmailsTool._ALLOWED_PARAMS | {"mailbox"}

    async def execute_routed(self, account, backend, **kwargs) -> Dict[str, Any]:
        self._validate_kwargs(kwargs)

        mode = search_mode(kwargs)
        if mode == "advanced":
            raise ValidationError(
                f"mode='advanced': not supported for {account.email} "
                f"({account.provider} over {account.kind.upper()}) — "
                f"multi-folder sorting, categories and importance are "
                f"Exchange-only. Use mode='quick' or mode='full_text'."
            )
        reject_other_mode_params(kwargs, mode)
        # Schema defaults (which some clients fill in) request nothing.
        explicit = {k: v for k, v in kwargs.items() if not search_param_is_default(k, v)}
        self.reject_exchange_only(
            account,
            explicit,
            (
                "to_address",
                "recipient",
                "body_contains",
                "categories",
                "importance",
                "is_flagged",
                "search_in",
                "exact_phrase",
            ),
        )
        if "sort_by" in explicit or "sort_order" in explicit:
            raise ValidationError(
                f"sort_by / sort_order: not supported for {account.email} "
                f"({account.provider}) — results are always newest-first."
            )
        if kwargs.get("is_read") is True:
            raise ValidationError(
                f"is_read=true: not supported for {account.email} "
                f"({account.provider} over {account.kind.upper()}) — only "
                f"is_read=false (unread mail) can be searched for."
            )
        flag_status = normalize_flag_status(kwargs.get("flag_status"))
        if flag_status and flag_status not in backend.flag_statuses:
            raise ValidationError(
                f"flag_status={flag_status!r}: not supported for {account.email} "
                f"({account.provider} over {account.kind.upper()}), which can only "
                f"filter on {' or '.join(backend.flag_statuses)}."
            )

        scope = kwargs.get("search_scope") or []
        if isinstance(scope, str):
            scope = [scope]
        names = [str(name).strip() for name in scope]
        all_folders = any(name.lower() == "all" for name in names)
        # A folder or scope equal to its schema default (inbox) yields to the
        # other; two real choices contradict each other.
        folder_named = normalize_folder(kwargs.get("folder")) != "inbox"
        scoped = [normalize_folder(name) for name in names] not in ([], ["inbox"])
        if folder_named and scoped:
            raise ValidationError(
                "folder and search_scope name different folders; pass one of them."
            )
        include: List[str] = []
        if all_folders:
            if not backend.supports_all_folder_search:
                raise ValidationError(
                    f"search_scope=['all']: not supported for {account.email} "
                    f"({account.provider} over {account.kind.upper()}) — search "
                    f"one folder per call."
                )
            for name in names:
                token = normalize_folder(name).lower()
                if token in ALL_FOLDERS_EXCLUDED:
                    include.append(token)
                elif token not in ("all", "inbox", "sent"):
                    raise ValidationError(
                        f"search_scope: {name!r} cannot be combined with 'all', "
                        f"which already covers every mail folder; next to 'all' "
                        f"name only {', '.join(ALL_FOLDERS_EXCLUDED)} to search "
                        f"those as well."
                    )
        elif len(scope) > 1:
            raise ValidationError(
                f"search_scope with {len(scope)} folders: not supported for "
                f"{account.email} ({account.provider}) — search one folder "
                f"per call, or the whole mailbox with search_scope=['all']."
            )

        query = kwargs.get("query") or kwargs.get("search_query") or None
        is_read = kwargs.get("is_read")
        has_attachments = kwargs.get("has_attachments")
        limit = _clamp(kwargs.get("max_results"), 1, 1000, 50)
        offset = _clamp(kwargs.get("offset"), 0, 100000, 0)
        criteria = dict(
            query=query,
            from_address=kwargs.get("from_address") or kwargs.get("sender") or None,
            subject=kwargs.get("subject_contains") or None,
            since=kwargs.get("start_date") or None,
            before=kwargs.get("end_date") or None,
            unread_only=is_read is False,
            has_attachments=None if has_attachments is None else bool(has_attachments),
            limit=limit + offset,
            flag_status=flag_status,
        )

        response: Dict[str, Any] = {}
        if all_folders:
            messages, searched = await self.call(
                backend.search_all_folders, include, **criteria,
            )
            response["folders_searched"] = searched
        else:
            folder = normalize_folder(
                kwargs.get("folder") if folder_named else (scope[0] if scope else None)
            )
            messages = await self.call(backend.search_messages, folder, **criteria)
            response["folder"] = folder
        page = messages[offset : offset + limit]
        fields = kwargs.get("fields")
        items = [
            project_fields(m.to_dict(), fields) if fields else m.to_dict() for m in page
        ]

        response.update({
            "items": items,
            "count": len(items),
            "total_available": len(messages),
        })
        if len(messages) > offset + len(items):
            response["next_offset"] = offset + len(items)
        return self.routed_response(
            f"Found {len(items)} matching emails", account, **response
        )


class GetEmailDetailsTool(RoutedMailTool, _ExchangeGetEmailDetailsTool):
    """get_email_details for any mailbox."""

    routed_properties = {
        "folder": {"type": "string", "default": "inbox", "description": _FOLDER_DESC}
    }

    async def execute_routed(self, account, backend, **kwargs) -> Dict[str, Any]:
        message_id = str(kwargs.get("message_id") or "").strip()
        if not message_id:
            raise ValidationError("message_id is required")
        self.reject_exchange_only(
            account,
            kwargs,
            ("trim_quoted",),
            hint="Non-Exchange bodies are returned as sent.",
        )

        folder = normalize_folder(kwargs.get("folder"))
        # ``format`` defaults to markdown (same as Exchange); html is opt-in.
        fmt = _body_format(kwargs.get("format"), DEFAULT_READ_FORMAT)
        message = await self.call(backend.get_message, message_id, folder, fmt)

        email = message.to_dict()
        if not bool(kwargs.get("include_body", True)):
            email["body"] = ""
            email["body_format"] = "omitted"
        fields = kwargs.get("fields")
        if fields:
            email = project_fields(email, fields)

        return self.routed_response("Email details retrieved", account, email=email)


class SendEmailTool(RoutedMailTool, _ExchangeSendEmailTool):
    """send_email for any mailbox."""

    routed_properties = {
        "files": FILES_SCHEMA,
        "cleanup_on_failure": _CLEANUP_ON_FAILURE,
        "folder": {
            "type": "string",
            "default": "drafts",
            "description": "Drafts folder for IMAP draft_id sends; ignored by Exchange/Graph.",
        },
    }

    async def execute_routed(self, account, backend, **kwargs) -> Dict[str, Any]:
        draft_id = self._draft_id(kwargs)
        if draft_id is not None:
            result = await self.call(
                backend.send_draft, draft_id,
                folder=normalize_folder(kwargs.get("folder") or "drafts"),
                dry_run=bool(kwargs.get("dry_run", False)),
            )
            return self.routed_response(
                "Dry run — no email sent" if result.get("dry_run") else
                "Draft accepted by mail server; final delivery not yet confirmed",
                account, result,
            )
        self.reject_exchange_only(
            account,
            kwargs,
            ("attachments", "inline_attachments", "importance"),
            hint=(
                "attachments (server-local paths) and inline_attachments "
                "(base64 objects) are Exchange-only legacy parameters; importance "
                "is also Exchange-only. " + _ROUTED_FILES_HINT
            ),
        )
        to = _as_list(kwargs.get("to"), "to")
        if not to:
            raise ValidationError("'to' is required")
        body = kwargs.get("body")
        if body is None:
            raise ValidationError("body is required")

        dry_run = bool(kwargs.get("dry_run", False))
        entries, loaded = await _routed_files(kwargs, dry_run)
        files: List[OutgoingAttachment] = loaded or []
        outgoing = OutgoingMessage(
            to=to,
            subject=kwargs.get("subject") or "",
            body=str(body),
            body_format=_body_format(kwargs.get("body_format"), "html"),
            cc=_as_list(kwargs.get("cc"), "cc"),
            bcc=_as_list(kwargs.get("bcc"), "bcc"),
            tracking_id=new_outbound_tracking_id(),
            attachments=files,
            cleanup_on_failure=bool(kwargs.get("cleanup_on_failure", False)),
        )
        if dry_run:
            # Without verify_files the sources are NOT fetched, so say how
            # many would be pulled instead of implying they were checked.
            return self.routed_response(
                "Dry run - no email sent",
                account,
                dry_run=True,
                sent=False,
                would_send_to=outgoing.to,
                would_cc=outgoing.cc,
                would_bcc=outgoing.bcc,
                subject=outgoing.subject,
                would_attach=len(entries),
                **dry_run_file_report(entries, loaded),
            )

        result = await self.call(backend.send_message, outgoing)
        extra: Dict[str, Any] = {}
        if files:
            extra["attachments"] = _file_summaries(files)
        return self.routed_response(
            "Email accepted by mail server; final delivery not yet confirmed",
            account,
            result,
            recipients=outgoing.to,
            accepted_by_mail_server=True,
            delivery_confirmed=False,
            **extra,
        )


class ReplyEmailTool(RoutedMailTool, _ExchangeReplyEmailTool):
    """reply_email for any mailbox (keeps the thread on every provider)."""

    routed_properties = {
        "folder": {"type": "string", "default": "inbox", "description": _FOLDER_DESC},
        "files": FILES_SCHEMA,
        "cleanup_on_failure": _CLEANUP_ON_FAILURE,
    }

    async def execute_routed(self, account, backend, **kwargs) -> Dict[str, Any]:
        message_id = str(kwargs.get("message_id") or "").strip()
        if not message_id:
            raise ValidationError("message_id is required")
        body = str(kwargs.get("body") or "").strip()
        if not body:
            raise ValidationError("body is required")
        self.reject_exchange_only(
            account,
            kwargs,
            ("attachments", "inline_attachments"),
            hint=(
                "attachments (server-local paths) and inline_attachments "
                "(base64 objects) are Exchange-only legacy parameters. "
                + _ROUTED_FILES_HINT
            ),
        )

        dry_run = bool(kwargs.get("dry_run", False))
        entries, loaded = await _routed_files(kwargs, dry_run)
        files = loaded or []
        outgoing = OutgoingMessage(
            to=[],
            body=body,
            body_format=_body_format(kwargs.get("body_format"), "html"),
            cc=_as_list(kwargs.get("cc"), "cc"),
            bcc=_as_list(kwargs.get("bcc"), "bcc"),
            reply_to_message_id=message_id,
            reply_to_folder=normalize_folder(kwargs.get("folder")),
            reply_all=bool(kwargs.get("reply_all", False)),
            tracking_id=new_outbound_tracking_id(),
            attachments=files,
            cleanup_on_failure=bool(kwargs.get("cleanup_on_failure", False)),
        )
        if dry_run:
            # The original is not read here: an IMAP fetch could flip \Seen.
            # So the preview names the caller's copies only; the provider
            # derives the rest when the reply is sent.
            return self.routed_response(
                "Dry run - no reply sent",
                account,
                dry_run=True,
                sent=False,
                replying_to=message_id,
                reply_all=outgoing.reply_all,
                message_checked=False,
                would_cc=outgoing.cc,
                would_bcc=outgoing.bcc,
                recipients_note=(
                    "would_cc and would_bcc are your additions. Sending also "
                    "addresses the original sender and, on reply_all, the "
                    "original To and Cc, listing nobody twice."
                ),
                would_attach=len(entries),
                **dry_run_file_report(entries, loaded),
            )
        result = await self.call(backend.send_message, outgoing)
        extra: Dict[str, Any] = {}
        if files:
            extra["attachments"] = _file_summaries(files)
        return self.routed_response(
            "Reply accepted by mail server; final delivery not yet confirmed",
            account,
            result,
            replied_to=message_id,
            reply_all=outgoing.reply_all,
            accepted_by_mail_server=True,
            delivery_confirmed=False,
            **extra,
        )

class ForwardEmailTool(RoutedMailTool, _ExchangeForwardEmailTool):
    """Forward original content and attachments, plus optional files, on any mailbox."""

    routed_properties = {
        "folder": {"type": "string", "default": "inbox", "description": _FOLDER_DESC},
        "files": FILES_SCHEMA,
        "cleanup_on_failure": _CLEANUP_ON_FAILURE,
    }

    async def execute_routed(self, account, backend, **kwargs) -> Dict[str, Any]:
        message_id = str(kwargs.get("message_id") or "").strip()
        if not message_id:
            raise ValidationError("message_id is required")
        to = _as_list(kwargs.get("to"), "to")
        if not to:
            raise ValidationError("'to' is required")
        self.reject_exchange_only(
            account, kwargs, ("attachments", "inline_attachments"),
            hint=_ROUTED_FILES_HINT,
        )
        dry_run = bool(kwargs.get("dry_run", False))
        entries, loaded = await _routed_files(kwargs, dry_run)
        files = loaded or []
        outgoing = OutgoingMessage(
            to=to,
            body=str(kwargs.get("body") or ""),
            body_format=_body_format(kwargs.get("body_format"), "html"),
            cc=_as_list(kwargs.get("cc"), "cc"),
            bcc=_as_list(kwargs.get("bcc"), "bcc"),
            tracking_id=new_outbound_tracking_id(),
            attachments=files,
            cleanup_on_failure=bool(kwargs.get("cleanup_on_failure", False)),
        )
        if dry_run:
            return self.routed_response(
                "Dry run - no forward sent",
                account,
                dry_run=True,
                sent=False,
                forwarding=message_id,
                would_send_to=outgoing.to,
                would_cc=outgoing.cc,
                would_bcc=outgoing.bcc,
                message_checked=False,
                would_attach=len(entries),
                **dry_run_file_report(entries, loaded),
            )
        result = await self.call(
            backend.forward_message, message_id, outgoing,
            folder=normalize_folder(kwargs.get("folder")),
        )
        return self.routed_response(
            "Forward accepted by mail server; final delivery not yet confirmed",
            account, result,
            forwarded_from=message_id,
            attachments=_file_summaries(files),
        )


class _RoutedDraftCreator(RoutedMailTool):
    """Shared input handling for the three public draft creation operations."""

    routed_properties = {
        "files": FILES_SCHEMA,
        "cleanup_on_failure": _CLEANUP_ON_FAILURE,
    }

    def get_schema(self):
        schema = super().get_schema()
        schema["description"] = (
            schema["description"].replace("Exchange-only ", "")
            + " Supported on Exchange, Graph and IMAP. Use returned IDs and folder "
            "for edits or sending; IMAP edits replace IDs."
        )
        return schema

    async def outgoing(self, account, kwargs, *, require_to=False, require_body=False):
        self.reject_exchange_only(
            account, kwargs, ("attachments", "inline_attachments", "importance", "categories"),
            hint="Use canonical files for attachments. Legacy inputs and categories/importance are Exchange-only.",
        )
        to = _as_list(kwargs.get("to"), "to")
        if require_to and not to:
            raise ValidationError("'to' is required")
        if require_body and kwargs.get("body") is None:
            raise ValidationError("body is required")
        return OutgoingMessage(
            to=to, subject=kwargs.get("subject") or "", body=str(kwargs.get("body") or ""),
            body_format=_body_format(kwargs.get("body_format"), "html"),
            cc=_as_list(kwargs.get("cc"), "cc"), bcc=_as_list(kwargs.get("bcc"), "bcc"),
            reply_all=bool(kwargs.get("reply_all", False)),
            attachments=await load_outgoing_files(kwargs.get("files")),
            cleanup_on_failure=bool(kwargs.get("cleanup_on_failure", False)),
        )

    def draft_response(self, account, result):
        return self.routed_response(
            "Draft created; use the returned message_id and folder for edits or sending",
            account, result, message_id=result.get("id"),
        )


class CreateDraftTool(_RoutedDraftCreator, _ExchangeCreateDraftTool):
    async def execute_routed(self, account, backend, **kwargs):
        if "subject" not in kwargs:
            raise ValidationError("subject is required")
        outgoing = await self.outgoing(account, kwargs, require_to=True, require_body=True)
        return self.draft_response(account, await self.call(backend.create_draft, outgoing))


class CreateReplyDraftTool(_RoutedDraftCreator, _ExchangeCreateReplyDraftTool):
    routed_properties = {
        **_RoutedDraftCreator.routed_properties,
        "folder": {"type": "string", "default": "inbox", "description": _FOLDER_DESC},
    }

    async def execute_routed(self, account, backend, **kwargs):
        message_id = _require_message_id(kwargs)
        outgoing = await self.outgoing(account, kwargs)
        result = await self.call(
            backend.create_reply_draft, message_id, outgoing,
            folder=normalize_folder(kwargs.get("folder")),
        )
        return self.draft_response(account, result)


class CreateForwardDraftTool(_RoutedDraftCreator, _ExchangeCreateForwardDraftTool):
    routed_properties = {
        **_RoutedDraftCreator.routed_properties,
        "folder": {"type": "string", "default": "inbox", "description": _FOLDER_DESC},
    }

    async def execute_routed(self, account, backend, **kwargs):
        message_id = _require_message_id(kwargs)
        outgoing = await self.outgoing(account, kwargs, require_to=True)
        result = await self.call(
            backend.create_forward_draft, message_id, outgoing,
            folder=normalize_folder(kwargs.get("folder")),
        )
        return self.draft_response(account, result)


class UpdateDraftTool(RoutedMailTool, _ExchangeUpdateDraftTool):
    routed_properties = {
        "folder": {
            "type": "string", "default": "drafts",
            "description": "Folder containing the draft. Required alongside IMAP IDs; default drafts.",
        },
    }

    async def execute_routed(self, account, backend, **kwargs):
        draft_id, body_format = self.validate_update(kwargs)
        result = await self.call(
            backend.update_draft, draft_id, subject=kwargs.get("subject"),
            body=kwargs.get("body"), body_format=body_format,
            folder=normalize_folder(kwargs.get("folder") or "drafts"),
        )
        return self.routed_response(
            "Draft updated; use returned IDs for subsequent operations",
            account, result, draft_id=draft_id,
        )



class MoveEmailTool(RoutedMailTool, _ExchangeMoveEmailTool):
    """move_email for any mailbox."""

    routed_properties = {
        "folder": {"type": "string", "default": "inbox", "description": _FOLDER_DESC}
    }

    async def execute_routed(self, account, backend, **kwargs) -> Dict[str, Any]:
        message_id = str(kwargs.get("message_id") or "").strip()
        if not message_id:
            raise ValidationError("message_id is required")
        self.reject_exchange_only(
            account,
            kwargs,
            ("destination_folder_id",),
            hint="Use destination_folder with a name/alias.",
        )
        destination = str(kwargs.get("destination_folder") or "").strip()
        if not destination:
            raise ValidationError("destination_folder is required")

        result = await self.call(
            backend.move_message,
            message_id,
            normalize_folder(destination),
            normalize_folder(kwargs.get("folder")),
        )
        return self.routed_response(
            f"Email moved to {normalize_folder(destination)}",
            account,
            result,
            message_id=message_id,
            folder=normalize_folder(destination),
            destination_folder=normalize_folder(destination),
        )


class DeleteEmailTool(RoutedMailTool, _ExchangeDeleteEmailTool):
    """delete_email for any mailbox."""

    routed_properties = {
        "folder": {"type": "string", "default": "inbox", "description": _FOLDER_DESC}
    }

    async def execute_routed(self, account, backend, **kwargs) -> Dict[str, Any]:
        message_id = str(kwargs.get("message_id") or "").strip()
        if not message_id:
            raise ValidationError("message_id is required")
        permanent = bool(
            kwargs.get("permanent", False) or kwargs.get("hard_delete", False)
        )
        result = await self.call(
            backend.delete_message,
            message_id,
            normalize_folder(kwargs.get("folder")),
            permanent,
        )
        return self.routed_response(
            "Email permanently deleted" if permanent else "Email moved to trash",
            account,
            result,
            message_id=message_id,
            folder=normalize_folder(kwargs.get("folder")),
            permanent=permanent,
            hard_delete=permanent,
        )


class UpdateEmailTool(RoutedMailTool, _ExchangeUpdateEmailTool):
    """update_email for any mailbox (read state + follow-up flag)."""

    routed_properties = {
        "folder": {"type": "string", "default": "inbox", "description": _FOLDER_DESC}
    }

    async def execute_routed(self, account, backend, **kwargs) -> Dict[str, Any]:
        message_id = str(kwargs.get("message_id") or "").strip()
        if not message_id:
            raise ValidationError("message_id is required")
        self.reject_exchange_only(account, kwargs, ("categories", "importance"))

        is_read = kwargs.get("is_read")
        flag_status = (kwargs.get("flag_status") or "").strip()
        is_flagged: Optional[bool] = None
        if flag_status:
            if flag_status == "Flagged":
                is_flagged = True
            elif flag_status == "NotFlagged":
                is_flagged = False
            else:
                raise ValidationError(
                    f"flag_status={flag_status!r}: not supported for "
                    f"{account.email} ({account.provider}) — only 'Flagged' "
                    f"and 'NotFlagged' exist outside Exchange."
                )
        if is_read is None and is_flagged is None:
            raise ValidationError("pass is_read and/or flag_status")

        result = await self.call(
            backend.mark_message,
            message_id,
            normalize_folder(kwargs.get("folder")),
            None if is_read is None else bool(is_read),
            is_flagged,
        )
        return self.routed_response(
            "Email updated", account, result, message_id=message_id,
            folder=normalize_folder(kwargs.get("folder")),
        )


# ----------------------------------------------------------------------
# attachments
# ----------------------------------------------------------------------
_ATTACHMENT_FOLDER_DESC = _FOLDER_DESC
_DRAFT_FOLDER_DESC = (
    "Folder holding the draft (default: drafts). iCloud/IMAP and "
    "Outlook/Graph only allow attachment changes on a draft."
)


def _require_message_id(kwargs: Dict[str, Any]) -> str:
    message_id = str(kwargs.get("message_id") or "").strip()
    if not message_id:
        raise ValidationError("message_id is required")
    return message_id


def _attachment_selector(kwargs: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    attachment_id = str(kwargs.get("attachment_id") or "").strip() or None
    attachment_name = str(kwargs.get("attachment_name") or "").strip() or None
    if not attachment_id and not attachment_name:
        raise ValidationError(
            "Either attachment_id or attachment_name is required"
        )
    return attachment_id, attachment_name


def _attachment_payload(info) -> Dict[str, Any]:
    """Backend attachment metadata in the shape the EWS tools return."""
    payload: Dict[str, Any] = {
        "id": info.attachment_id,
        "name": info.name,
        "size": info.size,
        "content_type": info.content_type,
        "is_inline": info.is_inline,
    }
    if info.content_id:
        payload["content_id"] = info.content_id
    return payload


def _decoded_subject(mime: bytes) -> str:
    """Subject of a raw message, used to name an attached .eml."""
    try:
        parsed = email.message_from_bytes(mime, policy=email.policy.default)
        return str(parsed.get("Subject") or "").strip()
    except Exception:  # pragma: no cover - malformed header
        return ""


class ListAttachmentsTool(RoutedMailTool, _ExchangeListAttachmentsTool):
    """list_attachments for any mailbox."""

    routed_properties = {
        "folder": {"type": "string", "default": "inbox", "description": _ATTACHMENT_FOLDER_DESC}
    }

    async def execute_routed(self, account, backend, **kwargs) -> Dict[str, Any]:
        message_id = _require_message_id(kwargs)
        folder = normalize_folder(kwargs.get("folder"))
        items = await self.call(
            backend.list_attachments,
            message_id,
            folder,
            bool(kwargs.get("include_inline", True)),
        )
        attachments = [_attachment_payload(i) for i in items]
        return self.routed_response(
            f"Found {len(attachments)} attachment(s)",
            account,
            message_id=message_id,
            folder=folder,
            attachments=attachments,
            count=len(attachments),
        )


class DownloadAttachmentTool(RoutedMailTool, _ExchangeDownloadAttachmentTool):
    """download_attachment for any mailbox."""

    routed_properties = {
        "folder": {"type": "string", "default": "inbox", "description": _ATTACHMENT_FOLDER_DESC}
    }

    async def execute_routed(self, account, backend, **kwargs) -> Dict[str, Any]:
        message_id = _require_message_id(kwargs)
        attachment_id, attachment_name = _attachment_selector(kwargs)
        return_as = kwargs.get("return_as", "base64")
        save_path = kwargs.get("save_path")
        if return_as == "file_path" and not save_path:
            raise ValidationError(
                "save_path is required when return_as is 'file_path'"
            )
        folder = normalize_folder(kwargs.get("folder"))

        attachment = await self.call(
            backend.get_attachment,
            message_id,
            folder,
            attachment_id,
            attachment_name,
        )
        common = dict(
            message_id=message_id,
            folder=folder,
            attachment_id=attachment.attachment_id,
            name=attachment.name,
            size=attachment.size,
            content_type=attachment.content_type or "application/octet-stream",
        )
        if return_as == "file_path":
            file_path = resolve_download_path(save_path, attachment.name, strict=True)
            await asyncio.to_thread(file_path.write_bytes, attachment.content)
            return self.routed_response(
                "Attachment saved successfully",
                account,
                file_path=str(file_path),
                saved_path=str(file_path),
                **common,
            )
        refuse_oversize_bytes(
            len(attachment.content),
            kwargs.get("max_bytes", DEFAULT_DOWNLOAD_MAX_BYTES),
            kind="Attachment",
        )
        return self.routed_response(
            "Attachment downloaded successfully",
            account,
            content_base64=base64.b64encode(attachment.content).decode("ascii"),
            file_path=None,
            saved_path=None,
            **common,
        )


class AddAttachmentTool(RoutedMailTool, _ExchangeAddAttachmentTool):
    """add_attachment for any mailbox (drafts only outside Exchange)."""

    routed_properties = {
        "folder": {"type": "string", "default": "drafts", "description": _DRAFT_FOLDER_DESC}
    }

    async def execute_routed(self, account, backend, **kwargs) -> Dict[str, Any]:
        message_id = _require_message_id(kwargs)
        loaded = await load_attachment_content(
            kwargs,
            # Personal file storage is a SmarterMail feature; iCloud/IMAP and
            # Outlook/Graph mailboxes have no equivalent to download from.
            storage_unavailable=(
                f"{account.email} ({account.provider} over "
                f"{account.kind.upper()}) has no SmarterMail file storage. "
                "Use file_url or file_content."
            ),
        )
        file_name = loaded.file_name

        result = await self.call(
            backend.add_attachment,
            message_id,
            file_name,
            loaded.content,
            normalize_folder(kwargs.get("folder", "drafts")),
            resolve_content_type(
                file_name, kwargs.get("content_type"), loaded.remote_content_type
            ),
            bool(kwargs.get("is_inline", False)),
            # Same derivation as the Exchange path: an inline part with no
            # Content-ID is unreachable from the HTML body.
            inline_content_id(
                str(file_name),
                bool(kwargs.get("is_inline", False)),
                kwargs.get("content_id"),
            ),
        )
        extras: Dict[str, Any] = {"source": loaded.source}
        if loaded.source_detail:
            extras["source_detail"] = loaded.source_detail
        return self.routed_response(
            f"Attachment '{file_name}' added successfully",
            account,
            result,
            message_id=message_id,
            folder=normalize_folder(kwargs.get("folder", "drafts")),
            **extras,
        )


class DeleteAttachmentTool(RoutedMailTool, _ExchangeDeleteAttachmentTool):
    """delete_attachment for any mailbox (drafts only outside Exchange)."""

    routed_properties = {
        "folder": {"type": "string", "default": "drafts", "description": _DRAFT_FOLDER_DESC}
    }

    async def execute_routed(self, account, backend, **kwargs) -> Dict[str, Any]:
        message_id = _require_message_id(kwargs)
        attachment_id, attachment_name = _attachment_selector(kwargs)
        result = await self.call(
            backend.delete_attachment,
            message_id,
            normalize_folder(kwargs.get("folder", "drafts")),
            attachment_id,
            attachment_name,
        )
        return self.routed_response(
            "Attachment deleted successfully", account, result, message_id=message_id,
            folder=normalize_folder(kwargs.get("folder", "drafts")),
        )


class ReadAttachmentTool(RoutedMailTool, _ExchangeReadAttachmentTool):
    """read_attachment for any mailbox."""

    routed_properties = {
        "folder": {"type": "string", "default": "inbox", "description": _ATTACHMENT_FOLDER_DESC}
    }

    async def execute_routed(self, account, backend, **kwargs) -> Dict[str, Any]:
        message_id = _require_message_id(kwargs)
        attachment_name = str(kwargs.get("attachment_name") or "").strip()
        attachment_id = str(kwargs.get("attachment_id") or "").strip() or None
        if not attachment_name and not attachment_id:
            raise ValidationError("attachment_name is required")
        folder = normalize_folder(kwargs.get("folder"))
        password = kwargs.get("password")

        attachment = await self.call(
            backend.get_attachment,
            message_id,
            folder,
            attachment_id,
            attachment_name or None,
        )
        if not attachment.content:
            raise ToolExecutionError("Attachment content is empty")
        file_ext = extension_from_filename(attachment.name)

        # The parent body only matters for password hints (archives/PDF), so
        # don't pay for it otherwise.
        body_text = ""
        if file_ext in ARCHIVE_EXTENSIONS or file_ext == "pdf":
            parent = await self.call(backend.get_message, message_id, folder, "text")
            body_text = parent.body or ""

        # A detached S/MIME signature verifies only against the exact signed
        # part of the raw message. Fetch the MIME only for a signature with no
        # content of its own. Attached and encrypted ones need nothing more.
        signed_content = None
        if file_ext in SIGNATURE_EXTENSIONS and needs_detached_content(attachment.content):
            try:
                raw_mime = await self.call(backend.get_mime, message_id, folder)
            except MailBackendError:
                # No MIME export on this backend: the signature is reported
                # as unverifiable rather than failing the read.
                raw_mime = None
            signed_content = find_signed_part(attachment.content, raw_mime)

        extracted_text, password_used = await asyncio.to_thread(
            _extract_with_password_fallback,
            attachment.content,
            file_ext,
            body_text=body_text,
            signed_content=signed_content,
            attachment_name=attachment.name,
            max_pages=kwargs.get("max_pages", 50),
            extract_tables=bool(kwargs.get("extract_tables", False)),
            password=password,
        )
        extras: Dict[str, Any] = {}
        if password_used and password_used != password:
            extras["password_source"] = "email_body"
            extras["password_used"] = True

        extras.update(clip_extracted_text(
            extracted_text,
            max_chars=kwargs.get("max_chars", DEFAULT_READ_ATTACHMENT_CHARS),
            offset=kwargs.get("offset", 0),
        ))

        return self.routed_response(
            "Text extracted successfully",
            account,
            message_id=message_id,
            folder=folder,
            file_name=attachment.name,
            file_type=file_ext,
            file_size=attachment.size,
            supports_arabic=True,
            **extras,
        )


class GetEmailMimeTool(RoutedMailTool, _ExchangeGetEmailMimeTool):
    """get_email_mime for any mailbox."""

    routed_properties = {
        "folder": {"type": "string", "default": "inbox", "description": _ATTACHMENT_FOLDER_DESC}
    }

    async def execute_routed(self, account, backend, **kwargs) -> Dict[str, Any]:
        message_id = _require_message_id(kwargs)
        folder = normalize_folder(kwargs.get("folder"))
        mime = await self.call(backend.get_mime, message_id, folder)
        refuse_oversize_bytes(
            len(mime),
            kwargs.get("max_bytes", DEFAULT_DOWNLOAD_MAX_BYTES),
            kind="MIME",
        )
        return self.routed_response(
            "Email MIME retrieved successfully",
            account,
            message_id=message_id,
            folder=folder,
            subject=_decoded_subject(mime),
            size=len(mime),
            mime_content_base64=base64.b64encode(mime).decode("ascii"),
        )


class AttachEmailToDraftTool(RoutedMailTool, _ExchangeAttachEmailToDraftTool):
    """attach_email_to_draft for any mailbox.

    Exchange embeds the message as an EWS ``ItemAttachment``; IMAP and Graph
    have no such construct, so the source message is attached as a
    ``message/rfc822`` (.eml) file — which every mail client renders as an
    attached e-mail.
    """

    routed_properties = {
        "folder": {"type": "string", "default": "drafts", "description": _DRAFT_FOLDER_DESC},
        "source_folder": {
            "type": "string",
            "default": "inbox",
            "description": "Folder holding source_message_id. " + _ATTACHMENT_FOLDER_DESC,
        },
    }

    async def execute_routed(self, account, backend, **kwargs) -> Dict[str, Any]:
        draft_id = str(kwargs.get("draft_id") or "").strip()
        source_message_id = str(kwargs.get("source_message_id") or "").strip()
        if not draft_id:
            raise ValidationError("draft_id is required")
        if not source_message_id:
            raise ValidationError("source_message_id is required")
        draft_folder = normalize_folder(kwargs.get("folder", "drafts"))
        source_folder = normalize_folder(kwargs.get("source_folder", "inbox"))

        mime = await self.call(backend.get_mime, source_message_id, source_folder)
        name = str(kwargs.get("attachment_name") or "").strip()
        if not name:
            subject = _decoded_subject(mime) or "attached-email"
            name = f"{subject}.eml"
        elif not name.lower().endswith(".eml"):
            name = f"{name}.eml"

        result = await self.call(
            backend.add_attachment,
            draft_id,
            name,
            mime,
            draft_folder,
            "message/rfc822",
            False,
            None,
        )
        return self.routed_response(
            "Email attached to draft successfully",
            account,
            result,
            draft_id=draft_id,
            folder=draft_folder,
            source_message_id=source_message_id,
            source_folder=source_folder,
        )
