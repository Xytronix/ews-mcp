"""Draft email tools for EWS MCP Server."""
import os
import re
from typing import Any, Dict
from datetime import datetime
from exchangelib import Message, Mailbox, FileAttachment, HTMLBody, Body

from .base import BaseTool
from .email_tools import (
    extract_body_html,
    clean_original_body_for_signature,
    format_forward_header,
    copy_attachments_to_message,
)
from ..models import SendEmailRequest
from ..exceptions import ToolExecutionError, ValidationError
from ..utils import (
    format_success_response,
    safe_get,
    find_message_for_account,
    ews_id_to_str,
    attach_inline_files,
    INLINE_ATTACHMENTS_SCHEMA,
    escape_html,
    format_body_for_html,
    sanitize_html,
)
from ..body_format import compose_body, WRITE_FORMAT_SCHEMA as BODY_FORMAT_SCHEMA
from .email_tools import add_reply_prefix, add_forward_prefix, reply_recipients
from .outgoing_attachments import (
    FILES_SCHEMA, load_outgoing_files, attach_outgoing_files, save_draft_with_attachments,
)
from .smartermail_base import smartermail_client
from .attachment_tools import extract_attachment_id, attachment_content_type


def _draft_metadata(message):
    """Saved identities, including persisted attachment IDs, without file bytes."""
    attachments = []
    for attachment in message.attachments or []:
        name = safe_get(attachment, "name", "")
        attachments.append({
            "attachment_id": extract_attachment_id(attachment),
            "name": name,
            "size": safe_get(attachment, "size", None),
            "content_type": attachment_content_type(attachment, name),
            "is_inline": bool(safe_get(attachment, "is_inline", False)),
            "content_id": safe_get(attachment, "content_id", None),
        })
    return {
        "id": ews_id_to_str(message.id),
        "folder": "drafts",
        "backend": "ews",
        "attachments": attachments,
    }


class CreateDraftTool(BaseTool):
    """Tool for creating draft emails in the Drafts folder."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "create_draft",
            "description": "Create an Exchange-only draft for review, optionally including files. Returns message_id. Review/edit it in your mail client, or send it unchanged with send_email(draft_id=<message_id>, target_mailbox=...). Do not pass compose fields when sending the saved draft.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Recipient email addresses"
                    },
                    "subject": {
                        "type": "string",
                        "description": "Email subject"
                    },
                    "body": {
                        "type": "string",
                        "description": "Email body (HTML supported)"
                    },
                    "cc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "CC recipients (optional)"
                    },
                    "bcc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "BCC recipients (optional)"
                    },
                    "importance": {
                        "type": "string",
                        "enum": ["Low", "Normal", "High"],
                        "description": "Email importance level (optional)"
                    },
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Exchange-only server-local file paths; prefer files for URL or base64 sources."
                    },
                    **INLINE_ATTACHMENTS_SCHEMA,
                    "files": FILES_SCHEMA,
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Outlook categories to attach to the draft (Issue #114)."
                    },
                    **BODY_FORMAT_SCHEMA,
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to create draft on behalf of (requires impersonation/delegate access)"
                    }
                },
                "required": ["to", "subject", "body"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Create draft email via EWS and save to Drafts folder."""
        target_mailbox = kwargs.pop("target_mailbox", None)
        categories = kwargs.pop("categories", None)
        body_format = kwargs.pop("body_format", "html")
        request = self.validate_input(SendEmailRequest, **kwargs)

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)
            files = await load_outgoing_files(
                kwargs.get("files"),
                storage_client=lambda: smartermail_client(self.ews_client, target_mailbox),
            )

            email_body = request.body.strip()

            # Strip CDATA wrapper if present
            if email_body.startswith('<![CDATA[') and email_body.endswith(']]>'):
                email_body = email_body[9:-3].strip()

            if not email_body:
                raise ToolExecutionError("Email body is empty after processing")
            if body_format != "html":
                email_body, _ = compose_body(email_body, body_format)


            is_html = bool(re.search(r'<[^>]+>', email_body))

            # Create message with appropriate body type
            if is_html:
                message = Message(
                    account=account,
                    subject=request.subject,
                    body=HTMLBody(email_body),
                    to_recipients=[Mailbox(email_address=email) for email in request.to],
                    folder=account.drafts,
                )
            else:
                message = Message(
                    account=account,
                    subject=request.subject,
                    body=Body(email_body),
                    to_recipients=[Mailbox(email_address=email) for email in request.to],
                    folder=account.drafts,
                )

            # Add CC/BCC
            if request.cc:
                message.cc_recipients = [Mailbox(email_address=email) for email in request.cc]
            if request.bcc:
                message.bcc_recipients = [Mailbox(email_address=email) for email in request.bcc]

            # Issue #114 — Outlook categories on the draft.
            if categories:
                message.categories = list(categories)

            # Set importance
            message.importance = request.importance.value

            # Add file attachments
            attachment_count = 0
            if request.attachments:
                for file_path in request.attachments:
                    try:
                        file_name = os.path.basename(file_path)
                        with open(file_path, 'rb') as f:
                            content = f.read()
                            attachment = FileAttachment(name=file_name, content=content)
                            message.attach(attachment)
                            attachment_count += 1
                    except FileNotFoundError:
                        raise ToolExecutionError(f"Attachment file not found: {file_path}")
                    except Exception as e:
                        raise ToolExecutionError(f"Failed to attach file {file_path}: {e}")

            # Add inline attachments
            inline_count = attach_inline_files(message, kwargs.get("inline_attachments", []))
            attachment_count += inline_count
            attachment_count += attach_outgoing_files(message, files)

            # Save as draft instead of sending
            save_draft_with_attachments(message)

            self.logger.info(f"Draft saved for {', '.join(request.to)} with {attachment_count} attachment(s)")

            return format_success_response(
                "Draft created successfully — check your Drafts folder in OWA/Outlook",
                message_id=ews_id_to_str(message.id) if hasattr(message, 'id') else None,
                created_time=datetime.now().isoformat(),
                recipients=request.to,
                subject=request.subject,
                mailbox=mailbox,
                **_draft_metadata(message),
            )

        except Exception as e:
            self.logger.error(f"Failed to create draft: {e}")
            raise ToolExecutionError(f"Failed to create draft: {e}")


class CreateReplyDraftTool(BaseTool):
    """Tool for creating reply drafts in the Drafts folder."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "create_reply_draft",
            "description": "Create an Exchange-only reply draft for review. Returns message_id. Review/edit it in your mail client, or send it unchanged with send_email(draft_id=<message_id>, target_mailbox=...). Do not pass compose fields when sending the saved draft.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The Exchange message ID of the email to reply to"
                    },
                    "body": {
                        "type": "string",
                        "description": "Optional reply body to include in the draft"
                    },
                    "reply_all": {
                        "type": "boolean",
                        "description": "If true, create a reply-all draft; otherwise create a reply draft",
                        "default": False
                    },
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Exchange-only server-local file paths; prefer files for URL or base64 sources."
                    },
                    **INLINE_ATTACHMENTS_SCHEMA,
                    "files": FILES_SCHEMA,
                    "cc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Additional CC recipients to add to the reply draft. "
                            "Saved in Cc alongside the auto-derived recipients: "
                            "the sender in To, plus the original To in To and the "
                            "original Cc in Cc when reply_all=true. Issue #119."
                        )
                    },
                    "bcc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Additional BCC recipients to add to the reply draft. "
                            "Mirrors create_forward_draft for parity."
                        )
                    },
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Outlook categories to attach to the reply draft (Issue #114)."
                    },
                    **BODY_FORMAT_SCHEMA,
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to create the draft on behalf of (requires impersonation/delegate access)"
                    }
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Create a reply draft via EWS and save it to Drafts."""
        message_id = kwargs.get("message_id")
        reply_all = kwargs.get("reply_all", False)
        attachments = kwargs.get("attachments", [])
        target_mailbox = kwargs.get("target_mailbox")
        body = (kwargs.get("body") or "").strip()
        body_format = kwargs.get("body_format", "html")
        # Issue #119: caller-supplied cc/bcc were silently dropped on the
        # saved draft. Now propagated alongside the auto-derived recipients.
        extra_cc = kwargs.get("cc") or []
        extra_bcc = kwargs.get("bcc") or []
        categories = kwargs.get("categories")  # Issue #114

        # Convert markdown/text bodies to HTML BEFORE the heuristic in
        # format_body_for_html runs. Mirrors ReplyEmailTool/ForwardEmailTool.
        if body and body_format != "html":
            body, _ = compose_body(body, body_format)

        if not message_id:
            raise ToolExecutionError("message_id is required")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)
            files = await load_outgoing_files(
                kwargs.get("files"),
                storage_client=lambda: smartermail_client(self.ews_client, target_mailbox),
            )

            original_message = find_message_for_account(account, message_id)
            original_subject = safe_get(original_message, "subject", "") or ""
            # Use shared prefix helpers so we don't produce "RE: RE: ..." stacks.
            reply_subject = add_reply_prefix(original_subject) if original_subject else "RE:"
            original_sender = safe_get(original_message, "sender", None)
            original_from_email = ""
            if original_sender and hasattr(original_sender, "email_address"):
                original_from_email = original_sender.email_address or ""

            original_to = [
                r.email_address
                for r in (safe_get(original_message, "to_recipients", []) or [])
                if r and hasattr(r, "email_address") and r.email_address
            ]
            original_cc = [
                r.email_address
                for r in (safe_get(original_message, "cc_recipients", []) or [])
                if r and hasattr(r, "email_address") and r.email_address
            ]

            # Reply-all keeps the original roles: sender + To in To, original
            # Cc in Cc. Issue #119: the caller's cc/bcc are added on top.
            reply_to_list, reply_cc_list, reply_bcc_list = reply_recipients(
                original_from_email,
                original_to,
                original_cc,
                account.primary_smtp_address,
                reply_all=reply_all,
                extra_cc=extra_cc,
                extra_bcc=extra_bcc,
            )

            header = format_forward_header(original_message)
            safe_from = escape_html(header.get("from", ""))
            safe_to = escape_html(header.get("to", ""))
            safe_cc = escape_html(header.get("cc", ""))
            safe_sent = escape_html(header.get("sent", ""))
            safe_subject = escape_html(header.get("subject", ""))

            original_body_html = sanitize_html(extract_body_html(original_message))
            original_body_html = clean_original_body_for_signature(original_body_html)

            # Render user body safely: plain text gets HTML-escaped and newlines
            # converted to <br/>; HTML goes through the lightweight sanitiser.
            user_body_html = format_body_for_html(body)

            headers_html = f'''<p style="font-size:11pt;font-family:Calibri,sans-serif;">
<b>From:</b> {safe_from}<br/>
<b>Sent:</b> {safe_sent}<br/>'''
            if safe_to:
                headers_html += f'''<b>To:</b> {safe_to}<br/>'''
            if safe_cc:
                headers_html += f'''<b>Cc:</b> {safe_cc}<br/>'''
            headers_html += f'''<b>Subject:</b> {safe_subject}
</p>'''

            complete_body = f'''<div class="WordSection1">
<p class="MsoNormal" style="font-size:11pt;font-family:Calibri,sans-serif;">{user_body_html}</p>
</div>
<div style="border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0in 0in 0in">
{headers_html}
</div>
{original_body_html}'''

            message = Message(
                account=account,
                subject=reply_subject,
                body=HTMLBody(complete_body),
                to_recipients=[Mailbox(email_address=e) for e in reply_to_list],
                folder=account.drafts,
            )

            if reply_cc_list:
                message.cc_recipients = [Mailbox(email_address=e) for e in reply_cc_list]
            if reply_bcc_list:
                message.bcc_recipients = [Mailbox(email_address=e) for e in reply_bcc_list]

            # Issue #114 — Outlook categories on the reply draft.
            if categories:
                message.categories = list(categories)

            inline_count, regular_count = copy_attachments_to_message(original_message, message)
            attachment_count = inline_count + regular_count

            for file_path in attachments:
                try:
                    file_name = os.path.basename(file_path)
                    with open(file_path, "rb") as f:
                        content = f.read()
                    message.attach(FileAttachment(name=file_name, content=content))
                    attachment_count += 1
                except FileNotFoundError:
                    raise ToolExecutionError(f"Attachment file not found: {file_path}")
                except PermissionError:
                    raise ToolExecutionError(f"Permission denied reading attachment: {file_path}")
                except Exception as e:
                    raise ToolExecutionError(f"Failed to attach file {file_path}: {e}")

            inline_b64_count = attach_inline_files(message, kwargs.get("inline_attachments", []))
            attachment_count += inline_b64_count
            attachment_count += attach_outgoing_files(message, files)

            save_draft_with_attachments(message)
            draft_message_id = ews_id_to_str(message.id)
            reply_to = header.get("from", "")

            self.logger.info(f"Reply draft saved for message {message_id} in mailbox: {mailbox}")

            return format_success_response(
                "Reply draft created successfully - check your Drafts folder in OWA/Outlook",
                message_id=draft_message_id,
                original_message_id=message_id,
                original_subject=original_subject,
                reply_subject=reply_subject,
                reply_to=reply_to,
                reply_all=reply_all,
                # Issue #119: surface every saved recipient on the response so
                # callers can sanity-check what landed.
                to=reply_to_list,
                cc=reply_cc_list,
                bcc=reply_bcc_list,
                attachments_count=attachment_count,
                inline_attachments_preserved=inline_count,
                created_time=datetime.now().isoformat(),
                mailbox=mailbox,
                **_draft_metadata(message),
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to create reply draft: {e}")
            raise ToolExecutionError(f"Failed to create reply draft: {e}")


class CreateForwardDraftTool(BaseTool):
    """Tool for creating forward drafts in the Drafts folder."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "create_forward_draft",
            "description": "Create an Exchange-only forward draft for review. Returns message_id. Review/edit it in your mail client, or send it unchanged with send_email(draft_id=<message_id>, target_mailbox=...). Do not pass compose fields when sending the saved draft.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The Exchange message ID of the email to forward"
                    },
                    "to": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of recipient email addresses"
                    },
                    "body": {
                        "type": "string",
                        "description": "Optional message to add before the forwarded content"
                    },
                    "cc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "CC recipients (optional)"
                    },
                    "bcc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "BCC recipients (optional)"
                    },
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Exchange-only server-local file paths; prefer files for URL or base64 sources."
                    },
                    **INLINE_ATTACHMENTS_SCHEMA,
                    "files": FILES_SCHEMA,
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Outlook categories to attach to the forward draft (Issue #114)."
                    },
                    **BODY_FORMAT_SCHEMA,
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to create the draft on behalf of (requires impersonation/delegate access)"
                    }
                },
                "required": ["message_id", "to"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Create a forward draft via EWS and save it to Drafts."""
        message_id = kwargs.get("message_id")
        to_recipients = kwargs.get("to", [])
        body = (kwargs.get("body") or "").strip()
        body_format = kwargs.get("body_format", "html")
        cc_recipients = kwargs.get("cc", [])
        bcc_recipients = kwargs.get("bcc", [])
        attachments = kwargs.get("attachments", [])
        target_mailbox = kwargs.get("target_mailbox")
        categories = kwargs.get("categories")  # Issue #114

        # Convert markdown/text bodies to HTML BEFORE the heuristic in
        # format_body_for_html runs. Mirrors CreateReplyDraftTool.
        if body and body_format != "html":
            body, _ = compose_body(body, body_format)

        if not message_id:
            raise ToolExecutionError("message_id is required")
        if not to_recipients:
            raise ToolExecutionError("to recipients are required")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)
            files = await load_outgoing_files(
                kwargs.get("files"),
                storage_client=lambda: smartermail_client(self.ews_client, target_mailbox),
            )

            original_message = find_message_for_account(account, message_id)
            original_subject = safe_get(original_message, "subject", "") or ""
            # Shared prefix helper avoids "FW: FW: ..." stacks.
            forward_subject = add_forward_prefix(original_subject) if original_subject else "FW:"

            header = format_forward_header(original_message)
            safe_from = escape_html(header.get("from", ""))
            safe_to = escape_html(header.get("to", ""))
            safe_cc = escape_html(header.get("cc", ""))
            safe_sent = escape_html(header.get("sent", ""))
            safe_subject = escape_html(header.get("subject", ""))

            original_body_html = sanitize_html(extract_body_html(original_message))
            original_body_html = clean_original_body_for_signature(original_body_html)

            user_body_html = format_body_for_html(body)

            headers_html = f'''<p style="font-size:11pt;font-family:Calibri,sans-serif;">
<b>From:</b> {safe_from}<br/>
<b>Date:</b> {safe_sent}<br/>
<b>Subject:</b> {safe_subject}<br/>'''
            if safe_to:
                headers_html += f'''<b>To:</b> {safe_to}<br/>'''
            if safe_cc:
                headers_html += f'''<b>Cc:</b> {safe_cc}<br/>'''
            headers_html += "</p>"

            complete_body = f'''<div class="WordSection1">
<p class="MsoNormal" style="font-size:11pt;font-family:Calibri,sans-serif;">{user_body_html}</p>
</div>
<div style="border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0in 0in 0in">
{headers_html}
</div>
{original_body_html}'''

            message = Message(
                account=account,
                subject=forward_subject,
                body=HTMLBody(complete_body),
                to_recipients=[Mailbox(email_address=email) for email in to_recipients],
                folder=account.drafts,
            )

            if cc_recipients:
                message.cc_recipients = [Mailbox(email_address=email) for email in cc_recipients]
            if bcc_recipients:
                message.bcc_recipients = [Mailbox(email_address=email) for email in bcc_recipients]

            # Issue #114 — Outlook categories on the forward draft.
            if categories:
                message.categories = list(categories)

            inline_count, regular_count = copy_attachments_to_message(original_message, message)
            original_attachment_count = inline_count + regular_count
            additional_attachment_count = 0

            for file_path in attachments:
                try:
                    file_name = os.path.basename(file_path)
                    with open(file_path, "rb") as f:
                        content = f.read()
                    message.attach(FileAttachment(name=file_name, content=content))
                    additional_attachment_count += 1
                except FileNotFoundError:
                    raise ToolExecutionError(f"Attachment file not found: {file_path}")
                except PermissionError:
                    raise ToolExecutionError(f"Permission denied reading attachment: {file_path}")
                except Exception as e:
                    raise ToolExecutionError(f"Failed to attach file {file_path}: {e}")

            inline_b64_count = attach_inline_files(message, kwargs.get("inline_attachments", []))
            additional_attachment_count += inline_b64_count
            additional_attachment_count += attach_outgoing_files(message, files)

            save_draft_with_attachments(message)
            draft_message_id = ews_id_to_str(message.id)

            self.logger.info(f"Forward draft saved for message {message_id} in mailbox: {mailbox}")

            return format_success_response(
                "Forward draft created successfully - check your Drafts folder in OWA/Outlook",
                message_id=draft_message_id,
                original_message_id=message_id,
                original_subject=original_subject,
                forward_subject=forward_subject,
                forwarded_to=to_recipients,
                cc=cc_recipients if cc_recipients else None,
                bcc=bcc_recipients if bcc_recipients else None,
                attachments_included=original_attachment_count,
                inline_attachments_preserved=inline_count,
                additional_attachments=additional_attachment_count,
                created_time=datetime.now().isoformat(),
                mailbox=mailbox,
                **_draft_metadata(message),
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to create forward draft: {e}")
            raise ToolExecutionError(f"Failed to create forward draft: {e}")


class UpdateDraftTool(BaseTool):
    """Partially update a saved draft without replacing its recipients or files."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "update_draft",
            "description": (
                "Update a saved draft's subject and/or body. Omitted fields are preserved; "
                "empty strings clear them. Recipients and attachments are preserved. "
                "Use the returned message_id for subsequent operations."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string", "description": "Saved draft message ID"},
                    "subject": {"type": "string", "description": "New subject; empty clears"},
                    "body": {"type": "string", "description": "New body; empty clears"},
                    "body_format": {
                        "type": "string", "enum": ["markdown", "html", "text"],
                        "default": "markdown",
                    },
                    "target_mailbox": {"type": "string", "description": "Mailbox containing the draft"},
                },
                "required": ["draft_id"],
            },
        }

    @staticmethod
    def validate_update(kwargs):
        draft_id = kwargs.get("draft_id")
        if not isinstance(draft_id, str) or not draft_id.strip():
            raise ValidationError("draft_id is required")
        for field in ("subject", "body"):
            if field in kwargs and not isinstance(kwargs[field], str):
                raise ValidationError(f"{field} must be a string; omit it to preserve its value")
        body_format = kwargs.get("body_format", "markdown")
        if body_format not in ("markdown", "html", "text"):
            raise ValidationError("body_format must be markdown, html or text")
        return draft_id.strip(), body_format

    async def execute(self, **kwargs) -> Dict[str, Any]:
        draft_id, body_format = self.validate_update(kwargs)
        target_mailbox = kwargs.get("target_mailbox")
        account = self.get_account(target_mailbox)
        try:
            # Restrict fetched fields too: avoid MIME conversion and never fetch
            # recipients/attachments merely to send them back in an update.
            message = next(iter(account.fetch(
                ids=[(draft_id, None)], only_fields=("is_draft", "subject", "body"),
            )), None)
            if isinstance(message, Exception):
                raise message
            if message is None:
                raise ToolExecutionError(f"Draft not found: {draft_id}")
            if not message.is_draft:
                raise ToolExecutionError("update_draft requires a draft; sent messages cannot be edited")
            changed = []
            if "subject" in kwargs:
                message.subject = kwargs["subject"]
                changed.append("subject")
            if "body" in kwargs:
                body = kwargs["body"]
                html, _actual = compose_body(body, body_format)
                message.body = HTMLBody(html if body else "")
                changed.append("body")
            if changed:
                message.save(update_fields=changed)
            saved_id = ews_id_to_str(message.id)
            return format_success_response(
                "Draft updated" if changed else "Draft unchanged",
                id=saved_id, message_id=saved_id, draft_id=saved_id,
                folder="drafts", backend="ews", mailbox=self.get_mailbox_info(target_mailbox),
                updated_fields=changed,
            )
        except ToolExecutionError:
            raise
        except Exception as exc:
            raise ToolExecutionError(f"Failed to update draft: {exc}") from exc
