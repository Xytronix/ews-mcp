"""Shared files schema and resolution for compose, reply, forward and drafts."""
from typing import Any, Callable, Dict, List, Optional

from exchangelib import FileAttachment, Message

from ..attachment_sources import (
    ALIAS_DESCRIPTION,
    FILE_NAME_REQUIRED,
    REMOTE_SOURCE_PROPERTIES,
    SOURCE_FIELDS,
    load_attachment_content,
    normalize_source_aliases,
    resolve_content_type,
    selected_source,
)
from ..exceptions import ToolExecutionError, ValidationError
from ..mail_backends.base import OutgoingAttachment
from ..smartermail import SmarterMailClient
from .attachment_tools import inline_content_id


FILES_SCHEMA: Dict[str, Any] = {
    "type": "array",
    "description": (
        "Canonical attachment input for send_email, reply_email, forward_email "
        "and create_*_draft on Exchange, Graph and IMAP. No separate attachment "
        "call is needed. Each entry supplies exactly one of "
        + ", ".join(SOURCE_FIELDS) + ". "
        "Remote agents (no filesystem shared with this server): use "
        "{file_content: <base64>, file_name: 'photo.jpg'} or file_url; for large "
        "or reused files call stage_outgoing_file once and pass the file_path it "
        "returns. file_path is only for paths on the Email server itself "
        "(stage_outgoing_file / download_attachment results), never the caller's box. "
        "storage_file_id requires SmarterMail personal storage. "
        + ALIAS_DESCRIPTION + " "
        "Legacy attachments/inline_attachments remain supported only on their "
        "existing Exchange paths and are deprecated; use files for new calls. "
        "Inline entries need the HTML body to reference cid:<content_id>."
    ),
    "items": {
        "type": "object",
        "properties": {
            "file_content": {
                "type": "string",
                "description": (
                    "Base64 encoded file content (data: URL prefix allowed); requires "
                    "file_name. Aliases: content, base64, data."
                ),
            },
            **REMOTE_SOURCE_PROPERTIES,
            "file_path": {
                "type": "string",
                "description": (
                    "Absolute path on the Email MCP server's filesystem, e.g. the "
                    "file_path returned by stage_outgoing_file. Paths from the "
                    "caller's own machine do not exist here. Aliases: path, filepath."
                ),
            },
            "file_name": {
                "type": "string",
                "description": (
                    "Required with file_content; otherwise overrides the source file "
                    "name. Alias: filename."
                ),
            },
            "content_type": {
                "type": "string",
                "description": "MIME content type; guessed from the file name when omitted.",
            },
            "is_inline": {
                "type": "boolean",
                "default": False,
                "description": "Embed a part referenced by cid:<content_id> in the HTML body.",
            },
            "content_id": {
                "type": "string",
                "description": "Content-ID for an inline part; derived from file_name if omitted.",
            },
        },
    },
}


def validate_outgoing_files(raw: Any) -> List[Dict[str, Any]]:
    """Validate every source before any fetch; also used for no-I/O previews.

    Returns the entries with alias spellings normalised to canonical fields.
    """
    if raw in (None, "", []):
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValidationError("files must be an array of file objects")
    entries: List[Dict[str, Any]] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValidationError(f"files[{index}] must be an object with exactly one content source")
        entry = normalize_source_aliases(entry)
        try:
            source = selected_source(entry)
        except ValidationError as exc:
            raise ValidationError(f"files[{index}]: {exc}", hint=exc.hint) from None
        if source == "file_content" and not str(entry.get("file_name") or "").strip():
            raise ValidationError(f"files[{index}]: {FILE_NAME_REQUIRED}")
        entries.append(entry)
    return entries


async def load_outgoing_files(
    raw: Any,
    *,
    storage_client: Optional[Callable[[], SmarterMailClient]] = None,
    storage_unavailable: Optional[str] = None,
) -> List[OutgoingAttachment]:
    """Resolve once through the same guarded loader used by add_attachment."""
    loaded: List[OutgoingAttachment] = []
    for entry in validate_outgoing_files(raw):
        blob = await load_attachment_content(
            entry, storage_client=storage_client, storage_unavailable=storage_unavailable,
        )
        is_inline = bool(entry.get("is_inline", False))
        loaded.append(OutgoingAttachment(
            name=blob.file_name,
            content=blob.content,
            content_type=resolve_content_type(
                blob.file_name, entry.get("content_type"), blob.remote_content_type,
            ),
            is_inline=is_inline,
            content_id=inline_content_id(blob.file_name, is_inline, entry.get("content_id")),
        ))
    return loaded


def attach_outgoing_files(message, files: List[OutgoingAttachment]) -> int:
    """Attach resolved bytes to an Exchange Message before saving or sending."""
    for attachment in files:
        message.attach(FileAttachment(
            name=attachment.name,
            content=attachment.content,
            content_type=attachment.content_type,
            is_inline=attachment.is_inline,
            content_id=attachment.content_id,
        ))
    return len(files)


def file_summaries(files: List[OutgoingAttachment]) -> List[Dict[str, Any]]:
    """Success-payload rows — names and sizes, never the bytes."""
    rows: List[Dict[str, Any]] = []
    for att in files:
        row: Dict[str, Any] = {
            "name": att.name,
            "size": len(att.content),
            "content_type": att.content_type,
            "is_inline": att.is_inline,
        }
        if att.content_id:
            row["content_id"] = att.content_id
        rows.append(row)
    return rows


#: Shared by send_email / reply_email / forward_email.
VERIFY_FILES_SCHEMA: Dict[str, Any] = {
    "type": "boolean",
    "default": False,
    "description": (
        "With dry_run: also resolve every files entry (decode base64, read the "
        "staged/server path, download file_url / storage_file_id) to prove it "
        "loads, and report name, size and content_type per file. Nothing is sent."
    ),
}


def dry_run_schema(action: str) -> Dict[str, Any]:
    return {
        "type": "boolean",
        "default": False,
        "description": (
            f"Preview without {action}: validates arguments and the files schema "
            "(set verify_files=true to also load every file) and returns "
            "would_attach / files_validated. Use this to smoke-test attachments."
        ),
    }


def dry_run_file_report(
    entries: List[Dict[str, Any]], loaded: Optional[List[OutgoingAttachment]]
) -> Dict[str, Any]:
    """Dry-run fields describing ``files``; ``loaded`` is None when not fetched."""
    report: Dict[str, Any] = {
        "files_validated": True,
        "files_fetched": loaded is not None,
    }
    if loaded is not None:
        report["files"] = file_summaries(loaded)
    return report


def save_draft_with_attachments(message: Message) -> None:
    """Save a new draft, then persist its resolved attachments via CreateAttachment."""
    if message.id:
        raise ValueError("Draft attachment persistence requires a new, unsaved message")
    # SmarterMail SaveOnly may ignore embedded attachments without an error.
    # Keep resolved objects aside and upload explicitly once an item ID exists.
    attachments, message.attachments = message.attachments, []
    message.save()
    try:
        message.attach(attachments)
    except Exception as upload_error:
        draft_id = message.id
        try:
            message.delete()
        except Exception as cleanup_error:
            raise ToolExecutionError(
                f"Attachment upload failed: {upload_error}. "
                f"Could not delete newly-created draft {draft_id}: {cleanup_error}. "
                "The partial draft may remain in Drafts."
            ) from upload_error
        raise
