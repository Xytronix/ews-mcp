"""Attachment management tools for EWS MCP Server."""

import asyncio
from typing import Any, Dict, List
import base64
import io
import os
import re
from pathlib import Path

from exchangelib import Body, FileAttachment, HTMLBody, ItemAttachment, Message

from .base import BaseTool
from ..exceptions import ToolExecutionError, ValidationError
from ..utils import (
    _safe_content_id,
    find_message_across_folders,
    find_message_for_account,
    format_success_response,
    guess_content_type,
    safe_get,
)
from ..attachment_parsers import extract_attachment_text
from ..attachment_password import guess_passwords_from_email, is_password_protected_error
from ..attachment_sniff import extension_from_filename, find_signed_part, needs_detached_content
from ..body_format import render_body
from ..attachment_sources import (
    ALIAS_DESCRIPTION,
    REMOTE_SOURCE_PROPERTIES,
    load_attachment_content,
    resolve_content_type,
    selected_source,
)
from .smartermail_base import smartermail_client


# Download jail for attachment file writes. Default: ./downloads inside the
# project working directory (created on first use). Operators can override via
# EWS_DOWNLOAD_DIR. download_attachment(save_path=...) is interpreted relative
# to this directory; absolute paths, "..", and symlinks that escape the jail
# are rejected.
_DEFAULT_DOWNLOAD_DIR = Path(os.environ.get("EWS_DOWNLOAD_DIR", "downloads"))


def _is_traversal_path(name: str) -> bool:
    """True iff ``name`` contains anything that escapes a single filename.

    Rejects absolute paths, "..", "./", leading slashes, UNC roots, and
    NUL bytes. Used to make save_path a *safe-or-rejected* field: either
    it's a simple filename we can honour, or the caller hears about it.
    """
    if not isinstance(name, str) or not name:
        return False
    if "\x00" in name:
        return True
    if os.path.isabs(name):
        return True
    # Normalise both slash flavours.
    unified = name.replace("\\", "/")
    if unified.startswith("/") or unified.startswith("~"):
        return True
    parts = [p for p in unified.split("/") if p]
    for p in parts:
        if p == "..":
            return True
    # "./foo" — the "." element is technically safe, but require a plain
    # filename with no separators at all for simplicity.
    return "/" in unified or "\\" in name


def _safe_basename(name: str) -> str:
    """Strip directory traversal / path separators from a user-supplied name."""
    if not name:
        return "attachment"
    name = os.path.basename(name)
    # Collapse any remaining path-like characters
    name = re.sub(r"[\\/]+", "_", name).strip()
    return name or "attachment"


def resolve_download_path(
    save_path: Any,
    default_name: str,
    *,
    strict: bool = False,
) -> Path:
    """Resolve a save_path into a safe, jailed absolute Path.

    - If ``save_path`` is None/empty, use ``default_name`` inside the jail.
    - If ``save_path`` contains directory components or parent refs and
      ``strict=True``, raise :class:`ValidationError` (HTTP 400).
    - If ``strict=False`` (legacy mode), only the basename is kept.
    - The returned path is always inside the jail; symlink tricks that
      would escape raise :class:`ToolExecutionError`.
    """
    jail = _DEFAULT_DOWNLOAD_DIR.resolve()
    jail.mkdir(parents=True, exist_ok=True)

    if save_path and _is_traversal_path(str(save_path)):
        if strict:
            raise ValidationError(
                f"Invalid save_path {save_path!r}: must be a plain filename "
                f"with no directory components or '..' refs. Files are "
                f"written under {jail}"
            )
        # Back-compat: strip to basename.
        save_path = _safe_basename(str(save_path))

    filename = (
        _safe_basename(str(save_path)) if save_path else _safe_basename(default_name)
    )
    candidate = (jail / filename).resolve()

    # Verify the resolved path is still inside the jail (defence against
    # symlink tricks or unusual basename behaviour).
    try:
        candidate.relative_to(jail)
    except ValueError as exc:
        raise ToolExecutionError(
            "Invalid save_path: resolved outside the download directory"
        ) from exc

    return candidate

# Token caps for attachment payloads. Unbounded extracted text and base64
# dumps dominate the MCP tool-result budget; callers page with offset /
# raise max_bytes explicitly when they really need the rest.
DEFAULT_READ_ATTACHMENT_CHARS = 8000
DEFAULT_DOWNLOAD_MAX_BYTES = 65536


def _nonneg_int(value: Any, default: int) -> int:
    """Parse a caller-supplied int, treating junk as ``default`` and negatives as 0."""
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else 0


def clip_extracted_text(
    text: str,
    *,
    max_chars: Any = None,
    offset: Any = None,
) -> Dict[str, Any]:
    """Slice extracted attachment text.

    ``content_length`` is always the unclipped size so callers can page
    with ``offset``. ``truncated`` is true iff any trailing text was cut.
    """
    full = text or ""
    total = len(full)
    start = _nonneg_int(offset, 0)
    limit = _nonneg_int(max_chars, DEFAULT_READ_ATTACHMENT_CHARS)
    sliced = full[start : start + limit]
    return {
        "content": sliced,
        "content_length": total,
        "truncated": (start + len(sliced)) < total,
    }


def refuse_oversize_bytes(
    size: int,
    max_bytes: Any = None,
    *,
    kind: str = "Attachment",
) -> None:
    """Refuse a base64 dump that would blow the token budget.

    Raises :class:`ValidationError` naming the actual size and pointing at
    ``read_attachment`` / ``file_url`` / ``storage_file_id`` / a larger
    explicit ``max_bytes``.
    """
    limit = _nonneg_int(max_bytes, DEFAULT_DOWNLOAD_MAX_BYTES)
    if size <= limit:
        return
    raise ValidationError(
        f"{kind} is {size} bytes; exceeds max_bytes={limit}. "
        "Use read_attachment, file_url, storage_file_id, or pass a larger max_bytes."
    )



ARCHIVE_EXTENSIONS = frozenset({"zip", "7z", "rar"})
SIGNATURE_EXTENSIONS = frozenset({"p7s", "p7m"})


def _message_body_text(message) -> str:
    """Plain text from message body for password heuristics."""
    text_body = safe_get(message, "text_body", "") or ""
    html_body = str(safe_get(message, "body", "") or "")
    if html_body:
        plain_from_html, _ = render_body(html=html_body, plain="", fmt="text")
        if plain_from_html:
            if text_body:
                return f"{text_body}\n{plain_from_html}"
            return plain_from_html
    return text_body


def _detached_signed_content(content: bytes, message) -> bytes | None:
    """Exact signed MIME part for a detached S/MIME signature, if obtainable.

    Only a SignedData without encapsulated content needs it: OpenSSL fails an
    attached signature (smime.p7m, CAdES .p7m) that is also handed outside
    content, and a detached one verifies only against the raw signed part,
    never the rendered body. The EWS lookup already fetched mime_content.
    """
    if not needs_detached_content(content):
        return None
    mime_content = safe_get(message, "mime_content", None)
    if not mime_content:
        return None
    if isinstance(mime_content, str):
        mime_content = mime_content.encode("utf-8")
    return find_signed_part(content, bytes(mime_content))


def _extract_with_password_fallback(
    content: bytes,
    file_ext: str,
    *,
    body_text: str,
    signed_content: bytes | None,
    attachment_name: str,
    max_pages: int,
    extract_tables: bool,
    password: str | None,
) -> tuple[str, str | None]:
    """Extract text; on archive password errors, guess password from ``body_text``.

    Provider-neutral: the caller supplies the parent message's plain-text body
    (password hints) and, for detached S/MIME signatures, the signed bytes.
    """

    try:
        return (
            extract_attachment_text(
                content,
                file_ext,
                max_pages=max_pages,
                extract_tables=extract_tables,
                password=password,
                signed_content=signed_content,
            ),
            password,
        )
    except RuntimeError as exc:
        if password or not is_password_protected_error(exc):
            raise
        if file_ext not in ARCHIVE_EXTENSIONS and file_ext != "pdf":
            raise

        candidates = guess_passwords_from_email(body_text, attachment_name)
        if not candidates:
            raise RuntimeError(
                f"{exc} — no password candidates found in email body; "
                "pass the `password` parameter explicitly"
            ) from exc

        last_exc: RuntimeError = exc
        for candidate in candidates:
            try:
                text = extract_attachment_text(
                    content,
                    file_ext,
                    max_pages=max_pages,
                    extract_tables=extract_tables,
                    password=candidate,
                    signed_content=signed_content,
                )
                return text, candidate
            except RuntimeError as retry_exc:
                last_exc = retry_exc
                if not is_password_protected_error(retry_exc):
                    raise

        raise RuntimeError(
            f"{last_exc} — tried {len(candidates)} password(s) from email body"
        ) from last_exc


def extract_attachment_id(attachment) -> str:
    """Extract a plain attachment ID from exchangelib attachment variants."""
    att_id = safe_get(attachment, 'attachment_id', '')
    if hasattr(att_id, 'id'):
        return att_id.id
    if isinstance(att_id, dict):
        return att_id.get('id', '')
    return str(att_id) if att_id else ''


def attachment_bytes(attachment) -> tuple[bytes, str]:
    """Content of any attachment class plus the file name to report.

    ``ItemAttachment`` (an embedded e-mail) has no ``content`` — only
    ``item`` — so reading ``attachment.content`` reports every embedded
    message as empty. Serve its raw MIME as an ``.eml`` payload instead.
    Fetch failures must also surface as themselves: ``content`` is lazy
    (GetAttachment on first read), and swallowing that error into "empty"
    misdiagnoses a transport problem as an empty attachment.
    """
    name = safe_get(attachment, "name", "attachment") or "attachment"
    if isinstance(attachment, ItemAttachment):
        try:
            item = attachment.item
        except Exception as exc:
            raise ToolExecutionError(f"Failed to fetch embedded message: {exc}")
        mime = safe_get(item, "mime_content", None) if item is not None else None
        if not mime:
            raise ToolExecutionError(
                "Embedded message has no MIME content on this server"
            )
        content = mime.encode("utf-8") if isinstance(mime, str) else bytes(mime)
        if not name.lower().endswith(".eml"):
            name = f"{name}.eml"
        return content, name

    try:
        content = attachment.content
    except Exception as exc:
        raise ToolExecutionError(f"Failed to fetch attachment content: {exc}")
    if not content:
        raise ToolExecutionError("Attachment content is empty")
    return bytes(content), name


def attachment_content_type(attachment, name: str) -> str:
    """Reported MIME type; embedded messages are message/rfc822."""
    if isinstance(attachment, ItemAttachment):
        return "message/rfc822"
    return safe_get(attachment, "content_type", None) or guess_content_type(name)


def names_match(candidate: Any, wanted: str) -> bool:
    """Attachment-name comparison: trimmed and case-insensitive everywhere."""
    return (str(candidate or "")).strip().lower() == str(wanted or "").strip().lower()


def inline_content_id(
    file_name: str, is_inline: bool, content_id: Any = None
) -> str | None:
    """The Content-ID to store with an attachment, normalised.

    Shared by the Exchange tool and the routed (IMAP/Graph) one so an inline
    image behaves the same on every provider: an inline part is unreachable
    from the HTML body without a Content-ID, so one is derived from the file
    name when the caller omits it. Angle brackets are stripped because that
    is how the value is written in HTML (``cid:logo123``); the IMAP backend
    adds them back for the MIME header.
    """
    explicit = str(content_id or "").strip().strip("<>").strip()
    if explicit:
        return explicit
    if not is_inline:
        return None
    return _safe_content_id(str(file_name or "attachment"), 0, set())


def build_embedded_message(account, source_message) -> tuple[Message, str]:
    """Detached copy of ``source_message``, ready to embed as an ItemAttachment.

    Returns the payload and how faithful it is:

    ``"mime"``   the source's raw MimeContent. The server rebuilds the
                 embedded item from it verbatim, so the original envelope
                 headers (From/Date/Message-ID) *and* the source's own
                 attachments survive the round trip — verified against this
                 SmarterMail EWS server with a two-attachment source.
    ``"fields"`` a field-by-field copy, used only when the server withheld
                 MimeContent. Nested attachments and envelope headers cannot
                 be rebuilt from those fields, so the copy is a summary and
                 the caller has to be told.

    Sending both at once (the first implementation) is the one thing to
    avoid: EWS treats MimeContent as *the* definition of the item, so the
    Subject/Body/recipient properties beside it are redundant at best and a
    property-set conflict at worst.
    """
    mime_content = safe_get(source_message, "mime_content", None)
    if mime_content:
        return Message(account=account, mime_content=mime_content), "mime"

    body_value = safe_get(source_message, "body", None)
    if hasattr(body_value, "body") and body_value.body:
        body = HTMLBody(body_value.body)
    elif isinstance(body_value, str) and "<" in body_value and ">" in body_value:
        body = HTMLBody(body_value)
    else:
        body = Body(str(body_value or safe_get(source_message, "text_body", "") or ""))

    kwargs = {
        "account": account,
        "subject": safe_get(source_message, "subject", "") or "Attached email",
        "body": body,
        "to_recipients": list(safe_get(source_message, "to_recipients", []) or []),
        "cc_recipients": list(safe_get(source_message, "cc_recipients", []) or []),
        "bcc_recipients": list(safe_get(source_message, "bcc_recipients", []) or []),
    }
    if safe_get(source_message, "sender", None):
        kwargs["sender"] = source_message.sender
    if safe_get(source_message, "author", None):
        kwargs["author"] = source_message.author

    return Message(**kwargs), "fields"


class ListAttachmentsTool(BaseTool):
    """Tool for listing email attachments."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "list_attachments",
            "description": "List attachments for an email message.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Email message ID"
                    },
                    "include_inline": {
                        "type": "boolean",
                        "description": "Include inline attachments (images embedded in email)",
                        "default": True
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """List attachments for an email."""
        message_id = kwargs.get("message_id")
        include_inline = kwargs.get("include_inline", True)
        target_mailbox = kwargs.get("target_mailbox")

        if not message_id:
            raise ToolExecutionError("message_id is required")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Find message across all folders (including custom subfolders)
            message = find_message_for_account(account, message_id)

            # List attachments
            attachments = []
            if hasattr(message, 'attachments') and message.attachments:
                for attachment in message.attachments:
                    # Check if it's inline and whether to include it
                    is_inline = safe_get(attachment, 'is_inline', False)

                    if not include_inline and is_inline:
                        continue

                    attachment_info = {
                        "id": extract_attachment_id(attachment),
                        "name": safe_get(attachment, 'name', 'unnamed'),
                        "size": safe_get(attachment, 'size', 0),
                        "content_type": safe_get(attachment, 'content_type', 'application/octet-stream'),
                        "is_inline": is_inline,
                        "content_id": safe_get(attachment, 'content_id'),
                        "attachment_type": attachment.__class__.__name__
                    }
                    attachments.append(attachment_info)

            self.logger.info(f"Found {len(attachments)} attachment(s) for message {message_id}")

            return format_success_response(
                f"Found {len(attachments)} attachment(s)",
                message_id=message_id,
                attachments=attachments,
                count=len(attachments),
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to list attachments: {e}")
            raise ToolExecutionError(f"Failed to list attachments: {e}")


class DownloadAttachmentTool(BaseTool):
    """Tool for downloading email attachments."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "download_attachment",
            "description": (
                "Download an attachment. Provide ``attachment_id`` (from "
                "list_attachments) OR ``attachment_name`` (exact file name, "
                "case-insensitive)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Email message ID"
                    },
                    "attachment_id": {
                        "type": "string",
                        "description": "Attachment ID (preferred; from list_attachments)"
                    },
                    "attachment_name": {
                        "type": "string",
                        "description": (
                            "Attachment file name (fallback when "
                            "attachment_id isn't known). Case-insensitive "
                            "exact match. Errors if multiple attachments "
                            "share the same name — use attachment_id in "
                            "that case."
                        )
                    },
                    "return_as": {
                        "type": "string",
                        "enum": ["base64", "file_path"],
                        "description": (
                            "base64 = the bytes come back in the response "
                            "(use this over MCP: the server's filesystem is "
                            "not yours). file_path = the server writes the "
                            "file into its own download directory and returns "
                            "that path, which a remote caller cannot read."
                        ),
                        "default": "base64"
                    },
                    "save_path": {
                        "type": "string",
                        "description": (
                            "File name under the server's download directory "
                            "(required when return_as is 'file_path'). Paths "
                            "that escape that directory are rejected."
                        )
                    },
                    "max_bytes": {
                        "type": "integer",
                        "default": DEFAULT_DOWNLOAD_MAX_BYTES,
                        "description": (
                            "Refuse a base64 return when the attachment is "
                            f"larger than this (default {DEFAULT_DOWNLOAD_MAX_BYTES}). "
                            "The error names the actual size. Use "
                            "read_attachment for text, file_url / "
                            "storage_file_id to send the file, or pass a "
                            "larger max_bytes. Not applied when return_as is "
                            "file_path."
                        ),
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                # Only message_id is strictly required; at least one of
                # attachment_id / attachment_name must be supplied — the
                # execute path enforces that with a ValidationError.
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Download an attachment."""
        message_id = kwargs.get("message_id")
        attachment_id = kwargs.get("attachment_id")
        attachment_name = kwargs.get("attachment_name")
        return_as = kwargs.get("return_as", "base64")
        save_path = kwargs.get("save_path")
        target_mailbox = kwargs.get("target_mailbox")

        if not message_id:
            raise ValidationError("message_id is required")

        if not attachment_id and not attachment_name:
            raise ValidationError(
                "Either attachment_id or attachment_name is required"
            )

        if return_as == "file_path" and not save_path:
            raise ValidationError(
                "save_path is required when return_as is 'file_path'"
            )

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Find message across all folders (including custom subfolders)
            message = find_message_for_account(account, message_id)

            # Find the attachment. Prefer attachment_id (stable). Fall
            # back to case-insensitive name match and raise if it's
            # ambiguous.
            attachment = None
            attachments = list(
                getattr(message, "attachments", None) or []
            )
            if attachment_id:
                for att in attachments:
                    if str(extract_attachment_id(att)) == str(attachment_id):
                        attachment = att
                        break
            if attachment is None and attachment_name:
                name_matches = [
                    att for att in attachments
                    if names_match(safe_get(att, "name", ""), attachment_name)
                ]
                if len(name_matches) > 1:
                    raise ValidationError(
                        f"Multiple attachments named {attachment_name!r} "
                        f"on this message; use attachment_id to disambiguate."
                    )
                if name_matches:
                    attachment = name_matches[0]

            if not attachment:
                ident = attachment_id or attachment_name
                raise ValidationError(f"Attachment not found: {ident!r}")

            # Content of a file attachment, or the embedded message's MIME.
            content, attachment_name = attachment_bytes(attachment)
            attachment_id = extract_attachment_id(attachment) or attachment_id
            content_type = attachment_content_type(attachment, attachment_name)

            if return_as == "base64":
                refuse_oversize_bytes(
                    len(content),
                    kwargs.get("max_bytes", DEFAULT_DOWNLOAD_MAX_BYTES),
                    kind="Attachment",
                )
                content_b64 = base64.b64encode(content).decode('utf-8')

                self.logger.info(f"Downloaded attachment {attachment_name} ({len(content)} bytes)")

                # file_path is always present in the response so callers
                # can key off a stable field regardless of return_as (null
                # when no file was written). Bug ATT-010.
                return format_success_response(
                    "Attachment downloaded successfully",
                    message_id=message_id,
                    attachment_id=attachment_id,
                    name=attachment_name,
                    size=len(content),
                    content_type=content_type,
                    content_base64=content_b64,
                    file_path=None,
                    saved_path=None,
                    mailbox=mailbox
                )

            elif return_as == "file_path":
                # Save to file, jailed under EWS_DOWNLOAD_DIR.
                #
                # ``strict=True`` converts traversal-style save_paths
                # ("../../etc/passwd", "/tmp/x", "C:\\...", NUL bytes)
                # into ValidationError -> HTTP 400. Previously the path
                # was silently reduced to its basename — callers thought
                # the tool had honoured their request, but it had jailed
                # it quietly. Bug #6.
                file_path = resolve_download_path(
                    save_path, attachment_name, strict=True
                )

                with open(file_path, 'wb') as f:
                    f.write(content)

                self.logger.info(
                    f"Saved attachment {attachment_name} to {file_path}"
                )

                return format_success_response(
                    "Attachment saved successfully",
                    message_id=message_id,
                    attachment_id=attachment_id,
                    name=attachment_name,
                    size=len(content),
                    content_type=content_type,
                    # ``file_path`` is the historical field name; ``saved_path``
                    # is a more descriptive alias. Both present = no caller
                    # surprises regardless of which they read (Bug ATT-010).
                    file_path=str(file_path),
                    saved_path=str(file_path),
                    mailbox=mailbox
                )

        except (ValidationError, ToolExecutionError):
            raise
        except Exception as e:
            self.logger.exception(f"Failed to download attachment: {e}")
            raise ToolExecutionError(f"Failed to download attachment: {e}")


class AddAttachmentTool(BaseTool):
    """Tool for adding attachments to draft or existing emails."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "add_attachment",
            "description": (
                "Attach a file to a draft. Exchange also accepts a message "
                "already in inbox/sent; iCloud (IMAP) and Outlook (Graph) do "
                "not - delivered mail is immutable there, and on IMAP the "
                "draft's id changes (the response carries the new id). Supply "
                "the bytes with exactly one of file_content (base64, with "
                "file_name), file_url (server fetches an HTTP(S) URL), "
                "storage_file_id (SmarterMail file storage) or file_path (a "
                "path on the Email server, e.g. from stage_outgoing_file - never "
                "the caller's own box). " + ALIAS_DESCRIPTION + " Prefer send_email / reply_email / "
                "forward_email(files=[...]) for one-call sending on any mailbox. "
                "For review first, Exchange draft creators accept files too. "
                "Use this tool for follow-on attachments, then send_email("
                "draft_id=..., target_mailbox=...). Keep the same target_mailbox; "
                "on IMAP use the latest returned draft id."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Email message ID to add attachment to"
                    },
                    "file_path": {
                        "type": "string",
                        "description": (
                            "Path on the Email MCP server's own filesystem (e.g. "
                            "the file_path returned by stage_outgoing_file). Paths "
                            "from the caller's machine do not exist here - remote "
                            "clients use file_content, file_url or storage_file_id."
                        )
                    },
                    "file_content": {
                        "type": "string",
                        "description": "Base64 encoded file content (requires file_name)"
                    },
                    **REMOTE_SOURCE_PROPERTIES,
                    "file_name": {
                        "type": "string",
                        "description": (
                            "File name for the attachment. Required with "
                            "file_content; otherwise taken from the source "
                            "(path, Content-Disposition / URL path, or the "
                            "stored file name)."
                        )
                    },
                    "content_type": {
                        "type": "string",
                        "description": (
                            "MIME content type (e.g. 'application/pdf'). "
                            "Guessed from the file name when omitted."
                        )
                    },
                    "is_inline": {
                        "type": "boolean",
                        "description": (
                            "Embed the file in the message body instead of "
                            "listing it as a separate attachment. Only useful "
                            "for images the HTML body references as "
                            "cid:<content_id>. On Exchange set the body with "
                            "create_draft / create_reply_draft / create_forward_draft; "
                            "update_email cannot change the body. Existing Graph/IMAP "
                            "drafts must already contain the cid reference."
                        ),
                        "default": False
                    },
                    "content_id": {
                        "type": "string",
                        "description": (
                            "Content-ID for inline references, e.g. 'logo123' "
                            "cited as <img src=\"cid:logo123\"> in the HTML "
                            "body. Derived from the file name when is_inline "
                            "is true and this is omitted (the response says "
                            "which value was used). Angle brackets are "
                            "stripped. Works with every content source, so an "
                            "inline image can come straight from file_url."
                        )
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Add attachment to email."""
        message_id = kwargs.get("message_id")
        is_inline = kwargs.get("is_inline", False)
        content_id = kwargs.get("content_id")
        target_mailbox = kwargs.get("target_mailbox")

        if not message_id:
            raise ToolExecutionError("message_id is required")

        # Reject a missing or ambiguous content source before authenticating,
        # searching folders or fetching any bytes.
        selected_source(kwargs)

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Find the message in Drafts folder (most common use case)
            message = None
            folders_to_search = [
                account.drafts,
                account.inbox,
                account.sent
            ]

            for folder in folders_to_search:
                try:
                    message = folder.get(id=message_id)
                    if message:
                        break
                except Exception:
                    continue

            if not message:
                raise ToolExecutionError(f"Message not found: {message_id}")

            loaded = await load_attachment_content(
                kwargs,
                storage_client=lambda: smartermail_client(
                    self.ews_client, target_mailbox
                ),
            )
            file_name = loaded.file_name
            file_content = loaded.content

            # Let the MIME type follow the file name unless the caller pinned
            # one: a hardcoded octet-stream default hides every PDF/image from
            # the receiving client.
            content_type = resolve_content_type(
                file_name, kwargs.get("content_type"), loaded.remote_content_type
            )
            content_id = inline_content_id(str(file_name), bool(is_inline), content_id)

            attachment_kwargs = {
                "name": file_name,
                "content": file_content,
                "content_type": content_type,
                "is_inline": is_inline,
            }
            if content_id:
                attachment_kwargs["content_id"] = content_id
            attachment = FileAttachment(**attachment_kwargs)

            # Persist via CreateAttachment. `Item.attach(...)` issues the
            # EWS call and mutates `attachment.attachment_id` in place — the
            # earlier `attachments.append(...) + message.save()` pattern
            # only mutated the local list and never reached the server, so
            # subsequent list_attachments calls returned an empty list.
            message.attach(attachment)

            attachment_id = extract_attachment_id(attachment)

            self.logger.info(
                f"Added attachment '{file_name}' ({loaded.source}) to message {message_id}"
            )

            response_extras = {"source": loaded.source}
            if loaded.source_detail:
                response_extras["source_detail"] = loaded.source_detail
            if content_id:
                response_extras["content_id"] = content_id
            return format_success_response(
                f"Attachment '{file_name}' added successfully",
                message_id=message_id,
                attachment_id=attachment_id,
                attachment_name=file_name,
                attachment_size=len(file_content),
                content_type=content_type,
                is_inline=is_inline,
                mailbox=mailbox,
                **response_extras,
            )

        except (ValidationError, ToolExecutionError):
            raise
        except Exception as e:
            self.logger.error(f"Failed to add attachment: {e}")
            raise ToolExecutionError(f"Failed to add attachment: {e}")


class DeleteAttachmentTool(BaseTool):
    """Tool for removing attachments from email messages."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "delete_attachment",
            "description": "Remove an attachment from an email.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Email message ID containing the attachment"
                    },
                    "attachment_id": {
                        "type": "string",
                        "description": "Attachment ID to remove"
                    },
                    "attachment_name": {
                        "type": "string",
                        "description": "Attachment name to remove (alternative to attachment_id)"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Delete attachment from email."""
        message_id = kwargs.get("message_id")
        attachment_id = kwargs.get("attachment_id")
        attachment_name = kwargs.get("attachment_name")
        target_mailbox = kwargs.get("target_mailbox")

        if not message_id:
            raise ToolExecutionError("message_id is required")

        if not attachment_id and not attachment_name:
            raise ToolExecutionError("Either attachment_id or attachment_name is required")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Find the message
            message = None
            folders_to_search = [
                account.drafts,
                account.inbox,
                account.sent
            ]

            for folder in folders_to_search:
                try:
                    message = folder.get(id=message_id)
                    if message:
                        break
                except Exception:
                    continue

            if not message:
                raise ToolExecutionError(f"Message not found: {message_id}")

            # Find the attachment to delete
            if not hasattr(message, 'attachments') or not message.attachments:
                raise ToolExecutionError("Message has no attachments")

            attachment_to_delete = None
            deleted_name = None

            for att in message.attachments:
                # Check by ID
                if attachment_id:
                    if str(extract_attachment_id(att)) == str(attachment_id):
                        attachment_to_delete = att
                        deleted_name = safe_get(att, 'name', 'Unknown')
                        break

                # Check by name
                elif attachment_name:
                    att_name = safe_get(att, 'name', '')
                    if names_match(att_name, attachment_name):
                        attachment_to_delete = att
                        deleted_name = att_name
                        break

            if not attachment_to_delete:
                raise ToolExecutionError(f"Attachment not found")

            # Persist via DeleteAttachment. Same reason as add_attachment:
            # mutating the local list and saving the parent item does not
            # send DeleteAttachment to EWS.
            message.detach(attachment_to_delete)

            self.logger.info(f"Deleted attachment '{deleted_name}' from message {message_id}")

            return format_success_response(
                f"Attachment '{deleted_name}' deleted successfully",
                message_id=message_id,
                attachment_name=deleted_name,
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to delete attachment: {e}")
            raise ToolExecutionError(f"Failed to delete attachment: {e}")


class ReadAttachmentTool(BaseTool):
    """Tool for reading and extracting text content from email attachments."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "read_attachment",
            "description": (
                "Extract text content from attachments. "
                "Broad format support: Office/PDF/images, ICS/VCF/QFX/MT940, iWork, "
                "ZIP/7Z/TAR/RAR, EPUB, PKPass, XPS, certs/PGP/S-MIME, DWG/DXF, "
                "KML/GPX, Parquet/Avro, RTF, MSG/EML/MIME, HTML/MHTML, config/text; "
                "magic-byte sniffing; password for PDF/ZIP/7Z/RAR; S/MIME "
                "signatures verified (detached .p7s against the message's signed "
                "MIME part); encrypted .p7m reported, not decrypted."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Email message ID"
                    },
                    "attachment_name": {
                        "type": "string",
                        "description": "Name of the attachment to read"
                    },
                    "extract_tables": {
                        "type": "boolean",
                        "description": "Extract tables from documents (PDF, DOCX, XLSX)",
                        "default": False
                    },
                    "max_pages": {
                        "type": "integer",
                        "description": "Maximum number of pages to extract (for PDFs)",
                        "default": 50
                    },
                    "max_chars": {
                        "type": "integer",
                        "default": DEFAULT_READ_ATTACHMENT_CHARS,
                        "description": (
                            "Maximum characters of extracted text to return "
                            f"(default {DEFAULT_READ_ATTACHMENT_CHARS}). "
                            "Use offset to page through the rest."
                        ),
                    },
                    "offset": {
                        "type": "integer",
                        "default": 0,
                        "description": (
                            "Character offset into the extracted text "
                            "(default 0)."
                        ),
                    },
                    "password": {
                        "type": "string",
                        "description": (
                            "Password for encrypted PDF, ZIP, 7z, or RAR attachments. "
                            "If omitted and the archive is password-protected, "
                            "read_attachment scans the email body for common "
                            "password hints (e.g. 'Password: ...')."
                        )
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["message_id", "attachment_name"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Read and extract text from attachment."""
        message_id = kwargs.get("message_id")
        attachment_name = kwargs.get("attachment_name")
        extract_tables = kwargs.get("extract_tables", False)
        max_pages = kwargs.get("max_pages", 50)
        password = kwargs.get("password")
        target_mailbox = kwargs.get("target_mailbox")

        if not message_id:
            raise ToolExecutionError("message_id is required")

        if not attachment_name:
            raise ToolExecutionError("attachment_name is required")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Find message across all folders (including custom subfolders)
            message = find_message_for_account(account, message_id)

            # Find the attachment by name
            attachment = None
            if hasattr(message, 'attachments') and message.attachments:
                for att in message.attachments:
                    if names_match(safe_get(att, 'name', ''), attachment_name):
                        attachment = att
                        break

            if not attachment:
                raise ToolExecutionError(f"Attachment '{attachment_name}' not found")

            # Content of a file attachment, or the embedded message's MIME
            # (served as .eml so the parser treats it as a message).
            content, attachment_name = attachment_bytes(attachment)

            # Determine file type from extension
            file_ext = extension_from_filename(attachment_name)

            # Extract text based on file type
            file_type = file_ext
            extracted_text, password_used = await asyncio.to_thread(
                _extract_with_password_fallback,
                content,
                file_ext,
                body_text=_message_body_text(message),
                signed_content=(
                    _detached_signed_content(content, message)
                    if file_ext in SIGNATURE_EXTENSIONS
                    else None
                ),
                attachment_name=attachment_name,
                max_pages=max_pages,
                extract_tables=extract_tables,
                password=password,
            )

            self.logger.info(
                f"Extracted {len(extracted_text)} characters from {attachment_name}"
            )

            extras: Dict[str, Any] = {}
            if password_used and password_used != password:
                extras["password_source"] = "email_body"
                extras["password_used"] = True

            clipped = clip_extracted_text(
                extracted_text,
                max_chars=kwargs.get("max_chars", DEFAULT_READ_ATTACHMENT_CHARS),
                offset=kwargs.get("offset", 0),
            )
            extras.update(clipped)

            return format_success_response(
                "Text extracted successfully",
                message_id=message_id,
                file_name=attachment_name,
                file_type=file_type,
                file_size=len(content),
                supports_arabic=True,  # UTF-8 encoding supports Arabic
                mailbox=mailbox,
                **extras,
            )

        except ToolExecutionError:
            raise
        except RuntimeError as e:
            raise ToolExecutionError(str(e))
        except Exception as e:
            self.logger.error(f"Failed to read attachment: {e}")
            raise ToolExecutionError(f"Failed to read attachment: {e}")


class GetEmailMimeTool(BaseTool):
    """Tool for retrieving raw MIME content for an email."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "get_email_mime",
            "description": "Return the raw RFC822 MIME content for an email as base64.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Email message ID to export as MIME"
                    },
                    "max_bytes": {
                        "type": "integer",
                        "default": DEFAULT_DOWNLOAD_MAX_BYTES,
                        "description": (
                            "Refuse the call when the MIME is larger than this "
                            f"(default {DEFAULT_DOWNLOAD_MAX_BYTES}). The error "
                            "names the actual size. Pass a larger max_bytes or "
                            "use get_email_details / read_attachment."
                        ),
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        message_id = kwargs.get("message_id")
        target_mailbox = kwargs.get("target_mailbox")

        if not message_id:
            raise ToolExecutionError("message_id is required")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)
            message = find_message_for_account(account, message_id)
            mime_content = safe_get(message, "mime_content", None)
            if not mime_content:
                raise ToolExecutionError("MIME content is not available for this message")

            if isinstance(mime_content, str):
                mime_bytes = mime_content.encode("utf-8")
            else:
                mime_bytes = bytes(mime_content)

            refuse_oversize_bytes(
                len(mime_bytes),
                kwargs.get("max_bytes", DEFAULT_DOWNLOAD_MAX_BYTES),
                kind="MIME",
            )

            return format_success_response(
                "Email MIME retrieved successfully",
                message_id=message_id,
                subject=safe_get(message, "subject", ""),
                mime_content_base64=base64.b64encode(mime_bytes).decode("utf-8"),
                mailbox=mailbox
            )
        except (ValidationError, ToolExecutionError):
            raise
        except Exception as e:
            self.logger.error(f"Failed to get email MIME: {e}")
            raise ToolExecutionError(f"Failed to get email MIME: {e}")


class AttachEmailToDraftTool(BaseTool):
    """Tool for attaching an existing email as an embedded message to a draft."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "attach_email_to_draft",
            "description": (
                "Attach an existing email as an embedded message to a draft. "
                "The source message is embedded from its raw MIME, so its own "
                "attachments and its From/Date headers travel with it. "
                "Outside Exchange (iCloud/IMAP, Outlook/Graph) the same tool "
                "attaches the source as a message/rfc822 .eml file, and the "
                "draft's id changes on IMAP."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "draft_id": {
                        "type": "string",
                        "description": "Draft message ID that should receive the embedded email"
                    },
                    "source_message_id": {
                        "type": "string",
                        "description": "Existing message ID to embed as an email attachment"
                    },
                    "attachment_name": {
                        "type": "string",
                        "description": "Optional display name for the embedded email attachment"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["draft_id", "source_message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        draft_id = kwargs.get("draft_id")
        source_message_id = kwargs.get("source_message_id")
        attachment_name = kwargs.get("attachment_name")
        target_mailbox = kwargs.get("target_mailbox")

        if not draft_id:
            raise ToolExecutionError("draft_id is required")
        if not source_message_id:
            raise ToolExecutionError("source_message_id is required")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)
            draft_message = find_message_for_account(account, draft_id)
            source_message = find_message_for_account(account, source_message_id)

            embedded_message, fidelity = build_embedded_message(account, source_message)
            # ``safe_get`` only falls back when the attribute is missing, so a
            # subject of None used to produce the literal name "None.eml".
            subject = safe_get(source_message, 'subject', None) or "attached-email"
            embedded_name = attachment_name or _safe_basename(f"{subject}.eml")
            item_attachment = ItemAttachment(name=embedded_name, item=embedded_message)
            draft_message.attach(item_attachment)

            extras: Dict[str, Any] = {"embedded_from": fidelity}
            if fidelity == "fields":
                # Silently shipping a lossy copy is worse than a slower call:
                # say what is missing so the caller can forward instead.
                extras["note"] = (
                    "The server did not return the source message's MIME "
                    "content, so the embedded copy carries only subject, body "
                    "and recipients - the source's own attachments and its "
                    "original headers are not included."
                )

            return format_success_response(
                "Email attached to draft successfully",
                draft_id=draft_id,
                source_message_id=source_message_id,
                attachment_name=embedded_name,
                attachment_id=extract_attachment_id(item_attachment),
                mailbox=mailbox,
                **extras,
            )
        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to attach email to draft: {e}")
            raise ToolExecutionError(f"Failed to attach email to draft: {e}")
