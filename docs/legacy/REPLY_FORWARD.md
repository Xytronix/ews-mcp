> **Legacy 4.0 documentation.** This page describes the `src/` server as
> extended by this fork. Upstream's `ghcr.io/azizmazrou/ews-mcp:latest` image
> does not include the fork's additions, and the fork publishes no image (see
> the [README](../../README.md#quick-start)). For upstream's 4.5 line see
> [`v5/README.md`](../../v5/README.md) and [`v5/docs/API.md`](../../v5/docs/API.md).

# Reply & Forward Email Tools

This document describes the `reply_email` and `forward_email` tools for the EWS MCP Server.

## Overview

Both tools route by `target_mailbox` to configured Exchange, Graph or
IMAP/SMTP accounts. Use `list_mailboxes` to discover accounts. For IMAP,
also pass the source `folder` when the message is outside Inbox; use a
standard alias or the native folder `id`/`path` returned by `list_folders`.

The Exchange implementation quotes original content, preserves inline
images and formats Outlook-style headers. Graph uses native reply/forward
actions. IMAP replies include the supplied body and new files, not automatic
quotation of the original body or its inline images; IMAP forwards retain
the original content and attachments.

Use canonical `files` for one-call attachments across providers. Prefer
`file_path` when the server can read the file; remote agents without shared
storage use `file_url` or base64 `file_content`. Legacy Exchange
`attachments`/`inline_attachments` remain supported but deprecated.
For review before sending, use `create_reply_draft` or `create_forward_draft`
on any provider, then `update_draft` and `send_email(draft_id=…)`.
See [drafts and recovery](API.md#drafts) for fresh IDs, limits and error handling.

---

## reply_email Tool

### Description

On Exchange, replies are composed locally with quoted content and available
`In-Reply-To`/`References` headers, not Exchange's native reply action.
Graph uses native reply actions; IMAP composes a new MIME reply with
threading headers. Client conversation grouping is not guaranteed.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `message_id` | string | Yes | Message ID in the selected mailbox; IMAP also needs the correct `folder` |
| `body` | string | Yes | Reply content; interpreted according to `body_format` |
| `reply_all` | boolean | No | If true, the sender and original To recipients go in To and the original Cc recipients stay in Cc (your own address and duplicates are dropped). If false, reply only to the sender (default: false) |
| `files` | array | No | Canonical attachment sources; see [API](API.md#attachment-sources) |
| `attachments` | array | No | **Deprecated, Exchange-only** server-local file paths; use `files` |
| `inline_attachments` | array | No | **Deprecated, Exchange-only** base64 file objects; use `files` |
| `body_format` | string | No | `html` (default), `markdown` or `text` |
| `folder` | string | No | IMAP source folder (default `inbox`) |
| `cleanup_on_failure` | boolean | No | Graph multi-step sends: default false retains a newly created draft for recovery; see [recovery](API.md#draft-recovery) |
| `target_mailbox` | string | No | Configured account to reply from; Exchange impersonation/delegate access where applicable |

### Response

Exchange example (provider-specific metadata varies). Acceptance is not
confirmation of recipient delivery:

```json
{
  "status": "success",
  "accepted_by_mail_server": true,
  "delivery_confirmed": false,
  "original_subject": "Original Email Subject",
  "reply_to": ["sender@example.com"],
  "reply_all": false,
  "attachments_count": 0,
  "inline_attachments_preserved": 2,
  "mailbox": "user@example.com"
}
```

### Example Usage

```python
# Simple reply
reply_email(
    message_id="AAMkADc3MWUy...",
    body="Thank you for your email. I'll review and get back to you."
)

# Reply all with HTML
reply_email(
    message_id="AAMkADc3MWUy...",
    body="<p>Sounds good! I'm <b>available</b> on Tuesday.</p>",
    reply_all=True
)

# Reply with a file accessible to the server
reply_email(
    message_id="AAMkADc3MWUy...",
    body="Please find the updated document attached.",
    files=[{"file_path": "/path/to/document.pdf"}]
)

# Exchange reply on behalf of another user (impersonation)
reply_email(
    message_id="AAMkADc3MWUy...",
    body="Thank you for contacting our team.",
    target_mailbox="shared@company.com"
)
```

---

## forward_email Tool

### Description

Forward an existing email to new recipients while preserving the original content, formatting, and attachments.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `message_id` | string | Yes | Message ID in the selected mailbox; IMAP also needs the correct `folder` |
| `to` | array | Yes | List of recipient email addresses |
| `body` | string | No | Optional message to add before the forwarded content |
| `cc` | array | No | CC recipients |
| `bcc` | array | No | BCC recipients |
| `files` | array | No | Canonical additional attachment sources; see [API](API.md#attachment-sources) |
| `attachments` | array | No | **Deprecated, Exchange-only** server-local file paths; use `files` |
| `inline_attachments` | array | No | **Deprecated, Exchange-only** base64 file objects; use `files` |
| `body_format` | string | No | `html` (default), `markdown` or `text` |
| `folder` | string | No | IMAP source folder (default `inbox`) |
| `cleanup_on_failure` | boolean | No | Graph multi-step sends: default false retains a newly created draft for recovery; see [recovery](API.md#draft-recovery) |
| `target_mailbox` | string | No | Configured account to forward from; Exchange impersonation/delegate access where applicable |

### Response

Exchange example (provider-specific metadata varies):

```json
{
  "status": "success",
  "accepted_by_mail_server": true,
  "delivery_confirmed": false,
  "original_subject": "Original Email Subject",
  "forwarded_to": ["recipient@example.com"],
  "cc": null,
  "bcc": null,
  "attachments_included": 3,
  "inline_attachments_preserved": 2,
  "additional_attachments": 1,
  "mailbox": "user@example.com"
}
```

### Example Usage

```python
# Simple forward
forward_email(
    message_id="AAMkADc3MWUy...",
    to=["colleague@company.com"]
)

# Forward with a message
forward_email(
    message_id="AAMkADc3MWUy...",
    to=["manager@company.com"],
    body="FYI - Please see the email below regarding the project update."
)

# Forward to multiple recipients with CC
forward_email(
    message_id="AAMkADc3MWUy...",
    to=["team-lead@company.com"],
    cc=["team@company.com"],
    body="<p>Please review the attached proposal.</p>"
)

# Forward with an additional file accessible to the server
forward_email(
    message_id="AAMkADc3MWUy...",
    to=["client@external.com"],
    body="Here's the information you requested, along with our latest brochure.",
    files=[{"file_path": "/path/to/brochure.pdf"}]
)

# Exchange forward on behalf of another user (impersonation)
forward_email(
    message_id="AAMkADc3MWUy...",
    to=["recipient@company.com"],
    target_mailbox="shared@company.com"
)
```

---

## Header Formatting

The Exchange implementations format quoted headers in Outlook-style.
The following are illustrative layouts, not exact output templates:

### HTML Format

```html
<hr style="border: none; border-top: 1px solid #ccc; margin: 20px 0;">
<div style="color: #1f497d; font-family: Calibri, Arial, sans-serif;">
<b>From:</b> John Doe <john.doe@company.com><br>
<b>Sent:</b> Sunday, December 07, 2025 10:30:45 AM<br>
<b>To:</b> Jane Smith <jane.smith@company.com>; Bob Wilson <bob@company.com><br>
<b>Cc:</b> Team Lead <lead@company.com><br>
<b>Subject:</b> Project Update
</div>
<br>
[Original email body here]
```

### Plain Text Format

```
────────────────────────────────────────
From: John Doe <john.doe@company.com>
Sent: Sunday, December 07, 2025 10:30:45 AM
To: Jane Smith <jane.smith@company.com>; Bob Wilson <bob@company.com>
Cc: Team Lead <lead@company.com>
Subject: Project Update

[Original email body here]
```

---

## Inline Image Preservation

On Exchange, original signature images and embedded images are copied into
replies/forwards with their CID metadata. This is not an IMAP reply guarantee.

### How It Works

1. Original email contains inline images referenced by `cid:` (Content-ID):
   ```html
   <img src="cid:image001.png@01DC676B.768520B0">
   ```

2. When forwarding/replying on Exchange, the tools copy attachments with:
   - `content_id` - The unique identifier for cid: references
   - `is_inline` - Flag marking the attachment as embedded

3. The recipient sees the images inline, not as broken icons

### Supported Elements

- Company logos in signatures
- Social media icons
- Certification badges
- Personal photos
- Any embedded image

---

## Conversation Threading

Exchange replies and IMAP replies use available `In-Reply-To` and `References`
headers to associate the reply with the original message. Graph uses the
provider's native reply/forward actions. This does not promise unchanged
Exchange conversation IDs or `Thread-Index` values across providers.
Final grouping in Outlook, Gmail, Apple Mail or other clients is client-dependent.

---

## RTL (Right-to-Left) Support

The Exchange quoted-body formatting handles Arabic, Hebrew and other RTL
content. Unicode content is supported; the final rendering depends on the
recipient's mail client. IMAP replies do not automatically quote the original.

---

## Error Handling

Graph draft/upload/send failures expose recovery details rather than silently
losing the draft ID. Retain the returned `draft_id`, `mailbox`, `folder` and
`stage`; inspect submission uncertainty before retrying. Default behavior keeps
the draft. Optional `cleanup_on_failure=true` only concerns a draft created by
the current call, never an existing caller-supplied draft.
The draft-creation variants also honor this option on IMAP when post-creation
metadata/relisting fails and the new ID is known. Unknown IDs require
re-listing Drafts, not guessing a deletion target. Graph skips cleanup if send
acceptance is uncertain, even when cleanup was requested.

IMAP responses distinguish `accepted_recipients` from `refused_recipients`.
Partial SMTP acceptance is not delivery and must not trigger a resend to the
accepted recipients. A timeout can leave submission uncertain. Check the
mailbox and server evidence before retrying; neither SMTP nor these tools
guarantee exactly-once delivery.

### Common Errors

| Error | Cause | Solution |
|-------|-------|----------|
| `message_id is required` | Missing message ID | Provide a valid message ID |
| `body is required` | Empty reply body | Provide reply content |
| `to recipients are required` | No forward recipients | Specify at least one recipient |
| `Message not found` | Invalid message ID | Verify the message exists |
| `Attachment file not found` | Invalid file path | Check the attachment path |
| `Permission denied` | File read error | Check file permissions |
| `Impersonation not enabled` | Missing config | Enable impersonation in .env |

---

## Best Practices

### 1. Use HTML for Rich Content
```python
reply_email(
    message_id="...",
    body="<p>I <b>agree</b> with the proposal.</p><ul><li>Point 1</li><li>Point 2</li></ul>"
)
```

### 2. Check Message ID Before Reply/Forward
```python
# Keep the original ID rather than indexing a provider-specific response.
original_id = "..."
details = get_email_details(message_id=original_id)

# For IMAP outside Inbox, pass the same source folder to both calls.
reply_email(message_id=original_id, body="...")
```

### 3. Use Reply All Carefully
```python
# Only use reply_all when all original recipients need the response
reply_email(
    message_id="...",
    body="...",
    reply_all=True  # Everyone on the original email will receive this
)
```

### 4. Add Context When Forwarding
```python
forward_email(
    message_id="...",
    to=["manager@company.com"],
    body="For your review - this relates to the budget discussion from last week."
)
```

---

## Technical Details

### Body Extraction

The tools correctly extract HTML content from Exchange's `HTMLBody` object:

```python
# Exchange returns: message.body (HTMLBody object)
# Actual content is: message.body.body (string)

def extract_body_html(message):
    body = message.body
    if hasattr(body, 'body') and body.body:
        return body.body  # The actual HTML string
    return str(body) if body else ""
```

### Attachment Copying

The Exchange implementation copies file attachments with CID metadata:

```python
new_attachment = FileAttachment(
    name=original.name,
    content=original.content,
    content_type=original.content_type,
    content_id=original.content_id,    # For cid: references
    is_inline=original.is_inline       # Embedded vs attached
)
```

---

## See Also

- [Impersonation Guide](IMPERSONATION.md) - Access other mailboxes
- [Email Tools](API.md#email-tools) - All email operations
- [Troubleshooting](TROUBLESHOOTING.md) - Common issues
