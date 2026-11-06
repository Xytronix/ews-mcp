> **Legacy 4.0 documentation.** This page describes the `src/` server as
> extended by this fork. Upstream's `ghcr.io/azizmazrou/ews-mcp:latest` image
> does not include the fork's additions, and the fork publishes no image (see
> the [README](../../README.md#quick-start)). For upstream's 4.5 line see
> [`v5/README.md`](../../v5/README.md) and [`v5/docs/API.md`](../../v5/docs/API.md).

# API Reference — v4.0

This reference covers the v4 core tools and this fork's mail workflows;
it is not an exhaustive inventory of the configured server. Optional
SmarterMail tools and fork additions change the available set. Use the
runtime `tools/list` for available tools and canonical input schemas.
This document explains intent, non-obvious parameters and response shapes.

Sections:

1. [Conventions](#conventions)
2. [Email — read & write (11 tools)](#email--read--write)
3. [Drafts](#drafts)
4. [Attachments (9 tools)](#attachments)
5. [Calendar (7 tools)](#calendar)
6. [Contacts (5 tools)](#contacts)
7. [Tasks (5 tools)](#tasks)
8. [Folders (3 tools)](#folders)
9. [Search (1 tool)](#search)
10. [Out-of-Office (4 tools)](#out-of-office)
11. [AI — semantic search (1 tool)](#ai--semantic-search)
12. [Agent-secretary primitives](#agent-secretary-primitives)
13. [Compound tools (2 tools)](#compound-tools)

---

## Conventions

### `target_mailbox` (multi-mailbox)

Use `list_mailboxes` to discover configured accounts and `target_mailbox: <smtp>`
to select one. Omit it to use the primary mailbox. Configured Graph/IMAP
accounts use their own credentials; Exchange impersonation/delegate access
requires the appropriate rights and `EWS_IMPERSONATION_ENABLED=true`.

Each `list_mailboxes` entry reports boolean `capabilities`: `drafts_create`,
`update_draft`, `calendar`, `contacts`, `schedule`, `files` and
`folder_scoped_ids`. Consult its `capability_hints` string-valued map for restrictions
and identity guidance. These describe the configured backend, not proof that
the provider has granted every required scope/permission; authorization still
applies to each operation. The server-wide tool list is not per-mailbox access.

| Capability | Exchange | Graph | IMAP/SMTP |
|---|---|---|---|
| List folders; read/search/details; move/delete mail | Supported | Supported | Supported; folder-scoped IDs |
| Send/reply/forward with `files`; send an existing draft unchanged | Supported | Supported | Supported; SMTP submission, not delivery |
| Create compose/reply/forward drafts; update draft subject/body | Supported | Supported | Supported; edits rewrite MIME and change IDs |
| List/read/download/add/delete attachments; raw MIME; attach email to draft | Supported | Supported; mutation is draft-only | Supported; mutation requires native Drafts and changes IDs |
| Calendar event CRUD/list/respond | Supported | Supported; calendar scopes/consent required | iCloud/DAV: event CRUD/list; meeting response/free-busy extras unsupported |
| Availability / meeting-time suggestions | Supported | Organization accounts via getSchedule; personal accounts unsupported | Not implemented |
| Contact CRUD / contacts search | Supported | Supported; contact scopes/consent required | iCloud/DAV: contact CRUD and address-book search |
| GAL/history/domain search and contact analytics | Supported | Not implemented; default `find_person` is explicitly contacts-only partial coverage | Not implemented; CardDAV contacts only |
| Tasks and other non-routed core tools | Supported EWS tools | Not implemented | Not implemented |
| Schedule/list/cancel/undo | Separate SmarterMail REST tools only | Not implemented | Not implemented |

Provider support is operation-specific, not full Exchange parity. Scheduling and
undo-send require **SmarterMail REST**, not generic Exchange. `schedule_email`
accepts either compose fields or a saved Exchange draft, with optional `files`.
`undo_send` requests the provider's short-window undo operation but its
acknowledgment does not prove delivery stopped. See [native scheduled send](#schedule_email).
Large Graph upload sessions and portable scheduling are not implemented. Graph
mail credentials alone do not grant calendar/contact access.

For IMAP message operations, pass the actual `folder` when the message is
outside Inbox; IDs are folder-scoped. A standard alias or the native `id`/`path`
returned by `list_folders` works. Exchange and Graph address messages by ID.
Keep `mailbox`, `folder` and the current message ID together. Never reuse an
IMAP UID from another mailbox/folder. After a draft edit or attachment mutation,
replace the old ID with the returned `id`/`message_id`, and use fresh attachment
part IDs from the result or `list_attachments`. If no authoritative new ID is
available, re-list Drafts rather than retrying against the old ID.

### CalDAV/CardDAV for iCloud IMAP mailboxes

IMAP is still mail-only, but iCloud mailboxes can use Apple's CalDAV/CardDAV
services behind the same agent-facing calendar/contact tools. `ICLOUD_EMAIL`
plus `ICLOUD_APP_PASSWORD` is enough for the default Apple discovery flow
(`caldav.icloud.com` and `contacts.icloud.com`: current principal → home set →
calendar/address-book collection). `ICLOUD_CALDAV_URL` and
`ICLOUD_CARDDAV_URL` may override the discovery roots, and account-table rows
can set `caldav_url` / `carddav_url` in `settings`.

CalDAV/CardDAV IDs are resource hrefs with VEVENT/VCARD UIDs and ETags in the
tool response; they are never IMAP UIDs. CRUD for events and contacts is
supported. Organization free/busy, meeting-time suggestions, attendee response
delivery semantics, GAL/history/domain search, communication statistics and
`analyze_contacts` are unavailable and fail explicitly.

### Body format on read & write

`get_email_details` defaults to `format=markdown`. Pass `format=html` for the
HTML body. `read_emails` returns previews unless `include_body=true`
(Exchange), and that body also defaults to `markdown`. Compose, reply, forward and
draft-creation tools default to `body_format=html`, and `update_draft`
defaults to `body_format=markdown`. On write, opt in to `markdown` or `text`
for server-side conversion, and specify the format explicitly when editing a
stored HTML body.
Omitting `body` from `update_draft` preserves it regardless of `body_format`.

### `categories` (Outlook follow-up labels)

Exchange create/update tools that expose `categories: list[str]` support
Outlook labels. On update, the field is **replaced** with the supplied list
(empty list clears). Strings are matched against the user's category list
case-insensitively by Outlook. Graph/IMAP `update_email` does not support categories.

### Field projection

Many list-style and detail-style tools accept a `fields: list[str]`
parameter to return only the named keys in each result. When omitted,
the full backward-compatible response shape is returned.

### Standard response envelope

```jsonc
{
  "success": true,
  "message": "Human-readable summary",
  // tool-specific fields …
  "mailbox": "user@company.com"
}
```

On failure, `success=false` and `error: <string>`. The MCP client also
sees an HTTP 4xx/5xx mapping for ValidationError (400) and
ToolExecutionError (500) when running over SSE/HTTP.
Routed multi-step failures can instead return a structured `success:false`
result with recovery details and `mailbox`; inspect those fields as well as
the error text. See [draft recovery](#draft-recovery).

---

## Email — read & write

11 core tools covering the email lifecycle, plus the opt-in SmarterMail
scheduling tools. The draft tools are under [Drafts](#drafts). `send_email`,
`reply_email` and `forward_email` accept the v4 `body_format` for
markdown→HTML server-side conversion.

### `send_email`

Compose and send a new email, or send a saved draft unchanged with
`draft_id` on Exchange, Outlook/Graph or iCloud/IMAP. Supports `dry_run`
in either mode.

**Parameters**

| Name | Type | Required | Notes |
|---|---|---|---|
| `draft_id` | `string` | for saved-draft mode | The saved draft's message ID; sends its existing recipients, subject, body and attachments as-is. Supported on Exchange, Outlook/Graph and iCloud/IMAP. |
| `folder` | `string` | no | Draft location for IMAP `draft_id` (default `drafts`); must resolve to the mailbox's native Drafts folder. Ignored by Exchange/Graph. |
| `to` | `string[]` | unless `draft_id` | recipient addresses |
| `subject` | `string` | unless `draft_id` | |
| `body` | `string` | unless `draft_id` | shape governed by `body_format` |
| `body_format` | `"html"\|"markdown"\|"text"` | no | default `html`. `markdown` → server converts to HTML; `text` → wrapped in minimal `<p>`. |
| `cc`, `bcc` | `string[]` | no | |
| `importance` | `"Low"\|"Normal"\|"High"` | no | **Exchange only** |
| `files` | `object[]` | no | Canonical attachments on **all three providers**; see [shared attachment sources](#attachment-sources). Each entry has exactly one source plus optional `file_name`, `content_type`, `is_inline`, `content_id`. |
| `attachments` | `string[]` | no | **Deprecated, Exchange only** server-side file paths; use `files` with `file_path` |
| `inline_attachments` | `object[]` | no | **Deprecated, Exchange only** base64 objects; use `files` with `file_content` and optional CID metadata |
| `dry_run` | `bool` | no | preview without sending. In draft mode, reads the saved draft without changing it. In compose mode, validates + renders; `files` are **not** fetched unless `verify_files` is true. The response reports `would_attach`, `files_validated: true` and `files_fetched`. Legacy Exchange `attachments` paths are still read for the preview. |
| `verify_files` | `bool` | no | with `dry_run`: also load every `files` entry (decode base64, read the staged/server path, download `file_url` / `storage_file_id`) and return `files: [{name, size, content_type, is_inline}]`. Nothing is sent. |
| `cleanup_on_failure` | `bool` | no | Graph multi-step compose mode only: default false retains a newly created draft for recovery. Never deletes an existing `draft_id`. See [draft recovery](#draft-recovery). |
| `target_mailbox` | `string` | no | configured mailbox; Exchange impersonation/delegate access where applicable |

With `draft_id`, omit **all** compose fields: `to`, `subject`, `body`,
`cc`, `bcc`, `importance`, `attachments`, `inline_attachments`,
`body_format`, and `files`. Explicitly supplying any of them is rejected,
even if empty or set to its default. Only `draft_id`, `target_mailbox`,
`dry_run` and IMAP's optional `folder` belong in a saved-draft send.

Use `files` for attachments in a single compose call; see
[efficient attachment flows](#efficient-attachment-flows).
All files are loaded before sending. For Graph sends and draft creators, this
implementation rejects more than **3 MiB total raw bytes** in newly supplied
`files`; this is not a cap on attachments already stored in a saved draft.
For larger content, explicitly put an externally hosted download link in the
body. `attach_file_link` generates such links only for SmarterMail storage.

**Response.** `send_email` / `reply_email` / `forward_email` report what the
mail server accepted, not what was delivered: `accepted_by_mail_server: true`
and `delivery_confirmed: false`. When `OUTBOUND_TRACKING_HEADER` names a
header, newly composed messages carry a random `tracking_id` in that header
for lookup in raw MIME (`get_email_mime`) or a downstream bounce. It is off
by default (`tracking_id: null`). A saved-draft send preserves the draft
as-is rather than adding a tracking header. Graph uses native reply actions,
so a routed Outlook/Microsoft 365 reply reports `tracking_id: null` instead
of an ID that never reached the wire.
For SMTP, inspect `accepted_recipients`, `refused_recipients` and
`partial_failure`: an accepted submission may exclude some recipients.
Sent-copy or draft-cleanup warnings do not undo acceptance. Never retry the
whole message merely because a recipient was refused or local cleanup failed;
that can duplicate mail to accepted recipients. A transport failure can leave
submission uncertain, and `sent:false` is not proof of non-delivery when an
uncertainty indicator is present.

### `schedule_email`

**SmarterMail-backed Exchange only.** This uses the provider's native
Scheduled queue, not a portable scheduler for Graph/IMAP.

| Parameter | Meaning |
|---|---|
| `send_at` | Required future ISO-8601 timestamp; normalized to UTC, naive timestamps treated as UTC |
| `draft_id` | Saved EWS draft ID in the selected mailbox; omit compose fields in this mode |
| `to`, `subject`, `body` | Required in compose mode; optional `cc`/`bcc` and `body_format` |
| `files` | Canonical attachment sources in either mode; appended to stored draft attachments |
| `target_mailbox` | Selected SmarterMail-backed Exchange mailbox |

With `draft_id`, omit `to`, `cc`, `bcc`, `subject`, `body` and `body_format`
even when empty/default. Edit first with `update_draft` if needed. Scheduling
preserves the stored recipients, subject, body, attachments and inline CID
metadata; additional `files` append rather than replacing saved attachments.
The provider consumes the source draft into Scheduled; it is not left as an
independent reviewable Drafts copy.

```python
scheduled = schedule_email(
    draft_id=draft["message_id"], send_at="2026-10-01T09:00:00Z",
    files=[{"file_path": "/server/reports/final.pdf"}],
    target_mailbox=mailbox,
)
# Use scheduled["uid"], not the EWS draft ID, to cancel:
cancel_scheduled_email(uid=scheduled["uid"], target_mailbox=mailbox)
```

Success returns `uid` (numeric **Scheduled-folder** UID), `send_at`,
`is_scheduled:true`, source `draft_id`, `sent_time_utc`, `time_left_ms`,
`folder`, `mailbox` and `delivery_confirmed:false`. Queued is not delivered;
the source EWS ID and Scheduled UID are not interchangeable.

**Recovery.** Post-mutation failures return `success:false`, `error`,
`error_type`, `draft_id`, `uid`, `folder`, `stage`, `submission_uncertain` and
`recovery_hint`. Stages include `prepare_draft`, `append_files`, `resolve_draft`,
`schedule_submission` and `verify_scheduled`. `submission_uncertain` becomes
true once native submission is attempted; a failed response may mean the
source was already consumed into Scheduled. Failure `uid` is the returned
Scheduled UID only, or null if no submission response supplied it.

Inspect both Drafts and Scheduled before retrying. Appending to an existing
draft may have persisted some files even if scheduling failed; the tool never
deletes that caller-owned source as failure cleanup. Failed new draft
preparation may attempt best-effort cleanup. Validation/file-fetch failures
before mutation can return an ordinary error without recovery fields.

### `list_scheduled_emails` / `cancel_scheduled_email`

Use the numeric `uid` returned by scheduling/listing with the same mailbox.
The tool searches the entire Scheduled queue, not just its first page, and
requests removal. `permanent=false` (default) requests a move to Deleted Items;
true requests permanent deletion.

**Queue removal is not confirmed cancellation.** A successful observed removal
reports `removed_from_queue:true`, `cancelled:null`,
`cancellation_confirmed:false` and `delivery_prevented:null`. A UID absent
beforehand may already have sent. Queue absence alone cannot establish that
native submission was prevented, and `success:true` does not mean recall.
`moved_to_deleted` remains null without destination-ID evidence; the provider
can perform a move while returning an empty `newUIDs` list.
`cleanup_confirmed:true` likewise confirms only queue removal. If the UID was
absent at the initial check, `cancel_requested:false`, `delete_submitted:false`,
`removed_from_queue:null` and `remaining_in_scheduled_folder:false` describe
that observation without a cancellation claim. Post-mutation errors preserve
the `uid`, `stage` and `delete_submitted` state for recovery.

`undo_send` is a separate short provider-delay request. Its acknowledgment is
also insufficient proof of cancellation: the native endpoint can acknowledge
an absent UID without establishing a stopped send. Neither acknowledgment
nor queue removal guarantees prevention across the submission race or recalls
delivered mail. Ordinary `send_email` does not return the numeric SmarterMail
UID required by `undo_send`.
An acknowledged undo response reports `undo_requested:true`,
`undo_acknowledged:true`, `undone:null` and `delivery_prevented:null`.
An expired-window HTTP 400 reports the UID with `undo_acknowledged:false` and
`undone:false`; do not reinterpret the failure as a successful recall.

### `read_emails`

Read messages from a folder, newest first. Returns previews (no full
body) by default — use `get_email_details` for the full message.

**Key parameters**

| Name | Type | Default | Notes |
|---|---|---|---|
| `folder` | `string` | `"inbox"` | standard name (`inbox` / `sent` / `drafts` / `deleted` / `junk`) or path (`Inbox/Projects`) or folder ID |
| `max_results` | `int` | 50 | max 1000 |
| `unread_only` | `bool` | false | |
| `format` | `"html"\|"markdown"\|"text"` | `markdown` | body format when `include_body=true` (Exchange) |
| `trim_quoted` | `bool` | false | strips `On …, X wrote:` thread history from that body (Exchange) |
| `include_body` | `bool` | false | Exchange: add the full `body` and `body_format` beside the preview. Graph/IMAP ignore it |

### `search_emails`

Three modes selected via `mode`:

| Mode | Description | Best for |
|---|---|---|
| `quick` (default) | Direct EWS filter on Inbox. Up to ~3 filters. | `subject_contains` / `from_address` / `is_read` style triage |
| `advanced` | Combinable filters + sort + scope across multiple folders. | Multi-criterion queries |
| `full_text` | Exchange `searchquery` engine. | Prose / phrase search |

**v4-relevant parameters**

| Name | Type | Notes |
|---|---|---|
| `is_flagged` | `bool` | filter by Outlook follow-up flag (`PR_FLAG_STATUS=2`). New in v4. |
| `is_read` | `bool` | |
| `has_attachments` | `bool` | |
| `categories` | `string[]` | the message must carry every listed category (all modes) |
| `importance` | `"Low"\|"Normal"\|"High"` | |
| `subject_contains`, `body_contains`, `from_address`, `to_address` | `string` | |
| `start_date`, `end_date` | ISO 8601 `string` | |
| `offset` / `max_results` | `int` | pagination via `next_offset` in response |
| `search_scope` | `string[]` | folders to search (advanced/full_text) |
| `sort_by`, `sort_order` | enums | `ascending`/`descending` |
| `query`, `search_query`, `keywords`, `exact_phrase`, `search_in` | full_text mode |

### `get_email_details`

Full message including body, attachments metadata, threading.

**v4 parameters (highlight)**

| Name | Type | Default | Notes |
|---|---|---|---|
| `format` | `"html"\|"markdown"\|"text"` | `markdown` | **the marquee v4 read-side feature.** `markdown` runs the Outlook MSO body through `markdownify` server-side (cached in SQLite forever — Exchange messages are immutable post-send), returning ~12× fewer tokens. Pass `html` for the HTML body. |
| `trim_quoted` | `bool` | false | strips `On …, X wrote:` thread history. Big savings on long threads. |
| `include_body` | `bool` | true | when false, both `body` and `body_html` drop from the response |
| `fields` | `string[]` | — | response projection |
| `target_mailbox` | `string` | — | |

**Exchange response shape (relevant fields).** Routed Graph/IMAP details use `email.id` rather than `email.message_id`.

```jsonc
{
  "email": {
    "message_id": "AAMk...",
    "subject": "...",
    "from": "alice@example.com",
    "to": ["..."],
    "cc": ["..."],
    "body": "# Subject ...",            // shape governed by `format`
    "body_format": "markdown",          // actual format returned
    "body_html": "",                    // empty when format != "html"
    "received_time": "2026-04-30T...",
    "has_attachments": true,
    "attachments": ["report.pdf", "thread.msg"]
    // ...
  }
}
```

### `get_emails_bulk`

Fetch multiple messages by ID in one call. Same per-item shape as
`get_email_details` for each input ID. Useful for the agent's
"give-me-context" pattern after a search.

### `delete_email`

Soft-delete to Trash by default; pass `permanent=true` (or alias
`hard_delete=true`) to bypass.

### `move_email`

Move to another folder. Exchange accepts `destination_folder` (name/path/alias)
or `destination_folder_id` (Exchange ID). On Graph/IMAP, use
`destination_folder`; the separate `destination_folder_id` parameter is rejected.
For IMAP, pass the returned native folder `id`/`path` as `destination_folder`,
and supply the source `folder`. After a move, re-list the destination to obtain
the message's current ID.

### `copy_email`

Exchange-only. Same shape as `move_email` but copies. Returns
`copied_message_id` of the new copy in the destination folder.

### `update_email`

Update read status and flags on all three providers. Categories, importance
and `flag_status="Complete"` are Exchange-only. This tool does not edit a
draft's subject/body; use [`update_draft`](#update_draft) instead.

| Name | Notes |
|---|---|
| `is_read` | `bool` |
| `flag_status` | `"NotFlagged" \| "Flagged" \| "Complete"` |
| `categories` | `string[]` — replace existing |
| `importance` | `"Low" \| "Normal" \| "High"` |

### `reply_email`

Reply on Exchange, Graph or IMAP. Exchange composes quoted content locally
with available threading headers; Graph uses native reply actions. IMAP
composes a new MIME reply with threading headers but does not automatically
quote the original body or copy its inline images.

| Name | Notes |
|---|---|
| `body_format` | `html` / `markdown` / `text` (v4) |
| `reply_all` | bool, default false. The sender and original To go in To, the original Cc stays in Cc, without your own address or duplicates |
| `files` | canonical attachments on **all three providers**; same schema as `send_email`. All files are loaded before sending. |
| `attachments`, `inline_attachments` | **deprecated, Exchange only**; still additive to `files` on existing supported paths |
| `cleanup_on_failure` | Graph multi-step send recovery; default false |
| `folder` | source folder for IMAP messages outside Inbox; use a standard alias or native folder `id`/`path` |
| `dry_run` | bool, default false: validate and preview without sending. Exchange also reads the original and returns `would_send_to`, `would_cc`, `subject`, `message_checked: true`; Graph/IMAP do not touch the provider (`message_checked: false`). Returns `would_attach`, `files_validated`, `files_fetched`. |
| `verify_files` | with `dry_run`: load every `files` entry to prove it resolves (same as `send_email`) |

On Exchange, the reply body is wrapped in the standard Outlook `WordSection1` div +
border-top thread separator + `From:/Sent:/To:/Cc:/Subject:` header
block. Inline attachments from the original message (signature
graphics, embedded images) are copied to the reply automatically. The
response includes `inline_attachments_preserved` so the caller can
verify the count.

### `forward_email`

Forward on Exchange, Outlook/Graph or iCloud/IMAP with `to`/`cc`/`bcc`,
a body prelude (`body_format`: `html` / `markdown` / `text`) and the
original attachments preserved. Add files in the same call with `files`
(same schema as `send_email`); all files are loaded before sending.
Exchange also accepts deprecated `attachments` and `inline_attachments`
on existing supported paths; these remain additive to canonical `files`.
Graph multi-step sends accept optional `cleanup_on_failure` (default false).
`dry_run` / `verify_files` behave as on `reply_email`; the preview reports
`would_send_to`, `would_cc`, `would_bcc` and `would_attach`.

---

## Drafts

| Tool | Purpose |
|---|---|
| `create_draft` | Save a compose draft to Drafts for later review/send |
| `create_reply_draft` | Save a reply to `message_id`, optionally `reply_all`, with an optional body and extra `cc`/`bcc` |
| `create_forward_draft` | Save a forward of `message_id` to `to`, preserving original content/attachments and adding an optional body and `cc`/`bcc` |
| `update_draft` | Partially replace a saved draft's subject/body without sending |
| `attach_email_to_draft` | Attach an existing message as `.eml` to a draft |

The creators and `update_draft` use the same public names on Exchange, Graph
and IMAP. Select the account with `target_mailbox`. Reply/forward creators
accept `folder` for the IMAP source (default `inbox`); their result is in Drafts,
not the source folder. All creators accept `body_format` (`html` / `markdown`
/ `text`, default `html`) and canonical `files`. `categories` and legacy
attachment inputs remain Exchange-only where already supported; unsupported
provider options are rejected, not silently dropped.

`create_draft` requires `to`, `subject` and `body`; reply creation requires
`message_id`; forward creation requires `message_id` and `to`. All creators
accept `cc`/`bcc`. Optional `cleanup_on_failure` defaults to false and controls
Graph/IMAP newly created draft recovery, never an existing caller draft.

Creators resolve file sources before creating a draft and report success only
after attachment persistence. Graph uses native reply/forward drafts to retain
provider threading and forwarded attachments. IMAP replies retain available
threading headers but do not automatically quote original body/inline images;
forwards preserve original content and attachments.

Creator results include `id`, its `message_id` alias, `mailbox`, `folder`,
`backend` and attachment metadata. Use that ID in
`add_attachment(message_id=…)`, `attach_email_to_draft(draft_id=…)`,
`update_draft(draft_id=…)` or `send_email(draft_id=…)`.
After any IMAP rewrite, use the returned fresh message and attachment part IDs,
not cached IDs. A successful replacement may still report old-draft cleanup
warnings; do not repeat the rewrite just to remove the old copy.

### `update_draft`

Draft-only partial update across Exchange, Graph and IMAP. Recipients,
attachments and threading metadata are preserved; this is not a recipient
editor. Use attachment tools separately to add/remove files.

| Parameter | Type | Default / meaning |
|---|---|---|
| `draft_id` | `string` | Required current draft message ID |
| `subject` | `string` | Omitted preserves; `""` clears; explicit null is rejected |
| `body` | `string` | Omitted preserves; `""` clears; explicit null is rejected |
| `body_format` | `"markdown"\|"html"\|"text"` | `markdown`; applies only to a supplied body |
| `folder` | `string` | `drafts`; IMAP must resolve to native Drafts |
| `target_mailbox` | `string` | Configured mailbox; keep paired with the ID |

Exchange updates only supplied fields; Graph patches the draft; IMAP appends
a replacement MIME message before retiring the old one. Always take the
returned current ID into subsequent calls.

### Draft recovery

Graph create/upload/send chains retain newly created drafts on failure by
default. `cleanup_on_failure=true` requests cleanup of a draft created by
that call only, never a caller's existing saved draft. Cleanup is skipped when
submission is uncertain. Exchange's existing attachment-save failure path
attempts cleanup of its newly created draft.

IMAP draft creators also honor `cleanup_on_failure`: if metadata/relisting
fails after creation and the new draft's ID is known, false retains it for
recovery and true requests targeted cleanup. Without an authoritative new ID,
do not guess a cleanup target; re-list Drafts. This option never authorizes
deleting a caller's existing draft after an edit failure.

Structured Graph failures return `success:false`, `error`, `mailbox` and a
`details` object, also exposed as top-level recovery fields:

| Field | Meaning |
|---|---|
| `draft_id`, `folder`, `backend` | Recovery identity when known; a failed creation may have no ID |
| `stage` | Failed step, such as `patch`, `attachments`, `metadata` or `send` |
| `sent` | `false` on failure; not proof of non-submission when uncertainty is flagged |
| `accepted_by_mail_server` | `false` for known non-acceptance, null when uncertain |
| `delivery_confirmed` | `false`; no claim of recipient delivery |
| `submission_uncertain` | Whether send acceptance is unknown |
| `retry_safe` | `false`; do not repeat a multi-step call blindly |
| `draft_retained` | true/false/null according to whether retention is known |
| `cleanup` | `requested`, `attempted`, `deleted`, plus `error` or `reason` when applicable |
| `recovery` | Guidance for inspection/reconciliation |

If retained, inspect the draft and attachments before choosing whether to
resume, send, or delete it; only some files may have uploaded. If send
acceptance is uncertain, do not blindly resend or delete the recovery draft:
reconcile with the provider first. A missing recovery ID is not proof that
the provider created nothing.

`attach_email_to_draft` embeds the source message from its **raw MIME**,
so the original `From`/`Date`/`Message-ID` and the source's own
attachments travel with it (verified against SmarterMail EWS: a source
with two attachments arrives with both). The response says which route
was taken — `embedded_from: "mime"`, or `"fields"` plus a `note` when the
server withheld the MIME and only subject/body/recipients could be
copied. Outside Exchange the same tool attaches the source as a
`message/rfc822` `.eml` file, and on IMAP the draft id changes.

The same review/edit/send workflow applies on all three providers. No tool
automatically sends a created or updated draft; submission requires an explicit
`send_email(draft_id=…)` call with no compose overrides.

### Efficient attachment flows

Send immediately on **Exchange, Outlook/Graph or iCloud/IMAP**; no draft
creation or separate attachment call is needed. These are alternative calls:

```python
# Remote agent (e.g. via an MCP gateway) - no filesystem shared with the Email server:
files = [{"file_content": jpeg_b64, "file_name": "photo.jpg"}]   # small payloads
# or: files = [{"file_url": "https://cdn.example.com/photo.jpg"}]
# or stage once and reuse (see "Remote agents" below):
#     staged = stage_outgoing_file(file_content=jpeg_b64, file_name="photo.jpg")
#     files = [{"file_path": staged["file_path"]}]
send_email(to=["alice@example.com"], subject="Report", body="Please review.",
           body_format="text", files=files, target_mailbox=mailbox)
reply_email(message_id=original_id, body="Here is the report.",
            body_format="text", files=files, target_mailbox=mailbox)
forward_email(message_id=original_id, to=["alice@example.com"], body="For review.",
              body_format="text", files=files, target_mailbox=mailbox)
```

For **review first on any provider**, create a draft with its files, then
optionally edit and send the current returned ID. Reply/forward creators
likewise accept `files` at creation:

```python
draft = create_draft(to=["alice@example.com"], subject="Report",
                     body="Please **review**.", body_format="markdown",
                     files=files, target_mailbox=mailbox)
draft = update_draft(draft_id=draft["message_id"], body="Please review today.",
                     body_format="text", folder="drafts", target_mailbox=mailbox)
send_email(draft_id=draft["message_id"], folder="drafts",
           target_mailbox=mailbox, dry_run=True)
# After review; no compose fields or files in this call:
send_email(draft_id=draft["message_id"], folder="drafts", target_mailbox=mailbox)
```

Use `add_attachment(message_id=…, …)` only for follow-on additions to an
existing draft. Choose one [source](#attachment-sources) per file;
`storage_file_id` requires SmarterMail-backed EWS, and `file_path` is
a path on the Email server (e.g. from `stage_outgoing_file`), not the caller's
filesystem. Inline files use `is_inline=True`
and a matching `cid:<content_id>` reference in the HTML body.

A saved-draft send takes no compose fields (including `files`) and preserves
the saved content. Its `dry_run=True` authenticates and reads the draft for a
body preview: no send, edit or delete. Missing or unreadable drafts can fail
during preview; attachment counts in the response vary by provider.
A compose-mode `send_email(dry_run=True, files=…)` validates the file
entries without fetching them, reporting `would_attach` and
`files_fetched: false` (add `verify_files=True` to load them).
`reply_email` and `forward_email` accept the same `dry_run` / `verify_files`;
draft-creation tools have no `dry_run` parameter.

For existing Graph/IMAP drafts, the same preview/send calls apply.
Graph submits the stored message through its send endpoint. IMAP requires
an undeleted message carrying the `\Draft` flag in the native Drafts
folder (`folder="drafts"` by default). It submits the stored MIME through
SMTP, stripping only `Bcc`/`Resent-Bcc` headers from the SMTP message;
the envelope still includes stored To/Cc/Bcc recipients. After SMTP
acceptance it attempts to save a Sent copy and remove the draft safely.
Attachment and subject/body mutations rewrite an IMAP draft: use the fresh
returned `id`/`message_id` and its `mailbox`/`folder` for the next call, and
use returned attachment part IDs or re-list them. If no new ID is available,
re-list Drafts before continuing; the old message is not an authoritative
replacement handle.

IMAP partial recipient acceptance, Sent-copy failures and draft-cleanup
failures return warnings with accepted status. Do not blindly retry an
accepted submission: that can send duplicates even if the draft remains.

### Remote agents: attaching bytes you hold

Agents reached through an MCP gateway share **no filesystem** with the
Email container, so `/workspace/...` or `file://` paths from the agent's box
never work. Two supported recipes, no workarounds needed:

```python
# 1. One call: base64 inline in files (fine up to a few MB)
reply_email(message_id=mid, body="Photo attached.", body_format="text",
            files=[{"file_content": jpeg_b64, "file_name": "photo.jpg"}],
            dry_run=True, verify_files=True)          # smoke test, sends nothing
reply_email(message_id=mid, body="Photo attached.", body_format="text",
            files=[{"file_content": jpeg_b64, "file_name": "photo.jpg"}])

# 2. Stage once, attach by server path (large or reused files)
staged = stage_outgoing_file(file_content=jpeg_b64, file_name="photo.jpg",
                             ttl_seconds=3600)       # or file_url=...
# -> {staging_id, file_path: "/tmp/ews-mcp-staged/<id>/photo.jpg",
#     file_name, size_bytes, content_type, expires_at, attach_with}
reply_email(message_id=mid, body="Photo attached.", files=staged["attach_with"]["files"])
delete_staged_file(staging_id=staged["staging_id"])  # optional; expiry cleans up
```

`stage_outgoing_file` takes exactly one of `file_content` (+ `file_name`),
`file_url` (same SSRF guard and 25 MiB cap as `files[].file_url`) or a server
`file_path`; the decoded size is capped at 25 MiB. `file_name` must be a bare
name — path separators, `..` and control characters are rejected. `ttl_seconds`
defaults to 3600 and is clamped to 60–86400; expired entries are deleted on the
next stage/delete and refused if still referenced. Files live under
`EWS_MCP_STAGING_DIR` (default `/tmp/ews-mcp-staged`, dir 0700, files 0600)
and are lost on container restart. Tool logs redact base64 (`file_content`
and its aliases), including inside `files` lists.

Field aliases are accepted in every `files` entry and in `add_attachment`:
`filename` → `file_name`; `content` / `base64` / `data` → `file_content`;
`path` / `filepath` → `file_path`; `url` → `file_url`. An alias only fills an
absent canonical field, and `filename` is never treated as content.

A missing server path fails with `File not found on the Email MCP server:
…` plus a `hint` field in the error response listing the remote-friendly
sources (error messages are capped at 200 characters; `hint` is not).

---

## Attachments

9 tools. The v4 changes: `read_attachment` text extraction and remote
content sources for `add_attachment`; `stage_outgoing_file` /
`delete_staged_file` put outbound bytes on the server for remote agents.

### `read_attachment`

Extract human/LLM-readable text from binary attachments.

**Parameters**

| Name | Type | Required | Notes |
|---|---|---|---|
| `message_id` | `string` | yes | parent message |
| `attachment_name` | `string` | yes | filename on the message |
| `extract_tables` | `bool` | no | PDF/DOCX table extraction |
| `max_pages` | `int` | no | PDF only |
| `target_mailbox` | `string` | no | |

**Supported file types (extension dispatch)**

| Extension | Library | Output |
|---|---|---|
| `pdf` | `pdfplumber` | text + per-page boundaries; tables when `extract_tables=true` |
| `docx` | `python-docx` | text + tables |
| `xlsx`, `xls` | `openpyxl`, `xlrd` | sheet-by-sheet rows |
| `pptx` | `python-pptx` | slide-by-slide text + speaker notes + embedded tables |
| **`msg`** | `extract-msg` | Outlook compound-file: subject / from / to / cc / date envelope + body (HTML→markdown) + recursive nested attachment listing |
| `eml` | stdlib `email` | RFC-822: same shape as `.msg` |
| `html`, `htm` | `markdownify` | RTL-safe GFM markdown |
| `csv` | stdlib | BOM-aware UTF-8 / UTF-16 decode |
| `txt`, `log`, `json`, `xml`, `md` | stdlib | as-is text |

**Response**

```jsonc
{
  "file_name": "thread.msg",
  "file_type": "msg",
  "file_size": 84321,
  "content_length": 12450,
  "content": "Subject: Re: Q1 review\nFrom: ...",
  "supports_arabic": true
}
```

### `list_attachments`

List per-attachment metadata for a message.

```jsonc
{
  "attachments": [
    {
      "id": "AAMk...",
      "name": "thread.msg",
      "size": 84321,
      "content_type": "application/vnd.ms-outlook",
      "is_inline": false,
      "attachment_type": "FileAttachment"
    }
  ],
  "count": 1
}
```

### `download_attachment`

Return an attachment's bytes as base64 (default) or save to a path
under the server's `EWS_DOWNLOAD_DIR` jail. Accepts either
`attachment_id` or `attachment_name`.

### `add_attachment`

Attach a file to a draft (Exchange also accepts a message already in
`inbox`/`sent`; iCloud/IMAP and Outlook/Graph refuse — delivered mail is
immutable there, and on IMAP the draft id changes, so use the `id` from
the response for the next call). Prefer `files` in the initial send, reply,
forward or draft-creation call; use this tool for follow-on additions.

### `stage_outgoing_file` / `delete_staged_file`

Store one outbound file on the Email server from `file_content` (+
`file_name`), `file_url` or a server `file_path`, and get back `staging_id`,
an absolute `file_path`, `file_name`, `size_bytes`, `content_type`,
`expires_at` and a ready-made `attach_with.files`. Pass that `file_path` in
`files` (or to `add_attachment`) until it expires; `delete_staged_file
(staging_id=…)` removes it early. See
[remote agents](#remote-agents-attaching-bytes-you-hold) for limits.

### Attachment sources

`files` is the canonical input on compose/reply/forward and draft creators.
Each entry, or `add_attachment` call, takes exactly one content source.
Remote agents without shared storage use base64 `file_content`, `file_url`,
or a path returned by `stage_outgoing_file`; see
[remote agents](#remote-agents-attaching-bytes-you-hold).

| source | use it when |
|---|---|
| `file_content` | Caller supplies base64 bytes inline (requires `file_name`; `data:` prefix allowed). Aliases `content`, `base64`, `data`. |
| `file_url` | Remote caller has a downloadable HTTP(S) URL. The server fetches it: ≤25 MiB per file, public hosts only (loopback, link-local incl. `169.254.169.254`, private and other non-global addresses refused; redirects re-validated). File name from `Content-Disposition` or the URL path. Alias `url`. |
| `file_path` | A path on the **Email server's** filesystem — e.g. from `stage_outgoing_file` or `download_attachment(return_as="file_path")` — never the caller's machine. Aliases `path`, `filepath`. |
| `storage_file_id` | File already in personal SmarterMail storage (`manage_file_storage`); ≤25 MiB per file, no bytes in the conversation. **SmarterMail-backed EWS only**. |

Deprecated `attachments` (paths) and `inline_attachments` (base64 objects)
remain supported only on their existing Exchange paths, additive to `files`.
New callers should use `files`; Graph/IMAP reject these legacy parameters.

The 25 MiB remote-fetch cap does not apply to `file_path`/`file_content`.
Provider limits still apply, including Graph's 3 MiB aggregate limit on
new `files` for send/reply/forward and draft creation. Standalone Graph `add_attachment` uses
a direct attachment POST, not that aggregate guard or a large-file upload
session. `MAX_ATTACHMENT_SIZE` is not enforced by these outbound paths.

**Inline images.** `is_inline: true` embeds the part in the body instead
of listing it as an attachment; it is only visible if the HTML body
references it as `cid:<content_id>`. `content_id` is derived from the
filename when omitted and preserved in the attachment. `add_attachment`
reports the resolved value; send/draft response fields vary. For deterministic
inline references, supply `content_id` explicitly and use the same `cid:`
value in the HTML body. Supply the body and inline `files` together when
composing, replying, forwarding or creating a draft on any provider. For later
`add_attachment` calls, add the corresponding reference to the draft body with
`update_draft` if needed; `update_email` cannot change the body.

### File storage vs. a real attachment

`manage_file_storage` is SmarterMail's personal drive, and
`attach_file_link` returns a **public download link** for a stored file.
Neither adds content to a message: explicitly put the link in the body.
The bytes become a MIME attachment when a `files` entry with
`storage_file_id`, or `add_attachment(storage_file_id=…)`, copies them into it.
Oversized files are not automatically converted to links. For Graph/IMAP,
supply an externally hosted download link instead.

### `delete_attachment`

Delete an attachment by `attachment_id` or `attachment_name`. Uses EWS
`DeleteAttachment` on Exchange; Graph/IMAP require a draft. IMAP rewrites
the message, so use the returned new `id` and re-list attachment IDs.

### `get_email_mime`

Return the raw RFC-822 MIME content of a message as base64. Useful
for archiving, audit, and forensics.

---

## Calendar

7 tools spanning create / read / update / delete + RSVP + free-busy.

| Tool | Highlights |
|---|---|
| `create_appointment` | `subject`, `start_time`, `end_time`, `location`, `body`, `attendees`, `is_all_day`, `reminder_minutes`, **`categories`** (v4) |
| `get_calendar` | range query, returns events with `item_id`, `subject`, `start`, `end`, `location`, `organizer`, `attendees`, `is_all_day` |
| `update_appointment` | `item_id` plus optional fields to update; `categories` v4 |
| `delete_appointment` | optional `send_cancellation: bool` (default true) |
| `respond_to_meeting` | `response: "Accept" / "Decline" / "Tentative"` + optional body |
| `check_availability` | `email_addresses[]`, `start_time`, `end_time`, returns free/busy |
| `find_meeting_times` | `attendees[]`, `duration_minutes`, `date_range_start`, `date_range_end`, scored suggestions |

**Graph scope and account limits.** Native Graph routes implement event
creation, calendar listing, partial update, deletion and meeting response under
the selected `target_mailbox`. Calendar/contact permissions and consent are
separate from mail access; capability discovery does not confirm the token has
those grants. Calendar attachments remain Exchange-only.
The calendar write workflow requires `Calendars.ReadWrite`, and contact writes
require `Contacts.ReadWrite`. A valid mail token can still return HTTP 403 for
both event and contact endpoints when the app or user has not consented to
them. With a delegated refresh token the backend requests the account's
`settings.graph_scope` (`OUTLOOK_GRAPH_SCOPE` for the `OUTLOOK_EMAIL`
account). Without it, the default is `offline_access` plus `User.Read`,
`Mail.ReadWrite`, `Mail.Send`, `Calendars.ReadWrite` and `Contacts.ReadWrite`,
so calendar and contact access is already requested. A custom
`settings.graph_scope` must list those scopes as well, for example:

```json
{
  "settings": {
    "graph_scope": "offline_access https://graph.microsoft.com/Mail.ReadWrite https://graph.microsoft.com/Mail.Send https://graph.microsoft.com/Calendars.ReadWrite https://graph.microsoft.com/Contacts.ReadWrite"
  }
}
```

App-only (client-credential) accounts request
`https://graph.microsoft.com/.default`, so the app's granted application
permissions apply. Obtain user/admin consent before relying on any of these
scopes, and do not silently change stored credentials or scopes.
Implemented support is not proof that a configured mailbox has granted access.

Graph availability uses native `getSchedule` for organization accounts.
Personal Microsoft accounts do not support this path and return an explicit
unsupported result rather than fabricated free/busy. Meeting suggestions are
deterministically scored from availability, not AI-generated. For an organizer
meeting, `delete_appointment(send_cancellation=false)` is refused on Graph
because Graph deletion sends cancellation; the tool cannot promise a silent
organizer deletion. IMAP has no calendar route.

Graph `getSchedule` accepts at most 20 schedules (including `include_self`)
and a time range shorter than 62 days. Suggestions use deterministic
15-minute availability slots: tentative may be considered available as on the
Exchange path, unknown availability blocks a slot, and preferences/breaks
affect scoring without allowing a meeting past the working-hours end.
`find_meeting_times` exposes `availability_errors` entries with `email` and
`error` for unknown/unreadable schedules. Inspect these before interpreting
an empty suggestion list as "everyone is busy." Slot durations use elapsed
UTC time and are rendered locally, preserving duration across DST transitions.
Deleting an attendee's own event copy reports `cancellation_sent:false`;
it is not an organizer cancellation.

All-day Graph events require midnight bounds in configured `TIMEZONE` and an
exclusive end (the midnight after the last included day). Ordinary event IDs
remain paired with the selected mailbox; they are not IMAP message UIDs.

Extended recurrence/occurrence management, calendar collections/ICS and
address-book/vCard operations remain separate SmarterMail REST tools. The
Graph core calendar/contact routes do not imply support for those extensions.

---

## Contacts

5 tools, including the `find_person` unified lookup.

### `find_person`

Exchange multi-source person search uses the `source` selector below. Graph and
iCloud/CardDAV support contacts-only lookup: `source="contacts"` searches stored
contacts; default `source="all"` reports partial, contacts-only coverage, not an
empty GAL or completed history scan. GAL, email-history and domain modes are
unsupported on Graph/DAV, as is `analyze_contacts`.

On Graph or CardDAV, omit `query` to list contacts, or supply a
case-insensitive substring matched against names, email addresses, company, job
title and department.
`max_results` is 1–1000. The backend scans the default `/contacts` collection
in pages up to 2,000 records; `truncated` signals the scan cap, so a no-match
result is not an exhaustive search when truncated. Other contact folders,
GAL and mail history are not searched.

Responses expose `partial`, `unsupported_sources`, `stats_included:false`
and limitations rather than fabricating communication statistics.
Explicit `source="gal"`, `"email_history"` or `"domain"` is unsupported.
Directory/GAL permissions are not a workaround for an unimplemented route.

| `source` | Searches |
|---|---|
| `all` (default) | GAL + local Contacts + email history |
| `gal` | Global Address List only (GAL fuzzy + exact) |
| `contacts` | Local Contacts folder only |
| `email_history` | Inbox/Sent senders/recipients seen in the last N days |
| `domain` | Everyone at a specified email domain |

On Exchange, `include_stats=true` enriches each result with email counts
(received / sent in window) and a relationship score. Graph does not provide
email-history/relationship analytics.

### `analyze_contacts`

Exchange-only communication-graph analytics. The `analysis_type` enum controls
which pre-built report runs:

| `analysis_type` | What it returns |
|---|---|
| `communication_history` | Per-sender history with one specific person |
| `overview` | Total contacts, email volume, VIP count, dormant count |
| `top_contacts` | Top N by email count over `time_range_days` |
| `by_domain` | Aggregated by `@domain` |
| `dormant` | Senders you used to talk to but haven't recently |
| `vip` | High-frequency, recent senders |

### `create_contact` / `update_contact` / `delete_contact`

Contact CRUD is routed on Exchange and Graph, using IDs from the selected
mailbox. v4 adds `categories: list[str]` to create and update. Graph contact
access requires its own scopes and consent; a working Graph mail token is not
proof of contact authorization. IMAP has no contact route.

Graph uses `Contacts.ReadWrite` for the supported read/write contact workflow.
If authorization fails, grant or re-consent that permission for the mailbox's
app/account and re-authenticate. The default delegated refresh scope already
requests it, and a custom `settings.graph_scope` must include it (see the
calendar configuration example above). Read-only grants do not authorize
mutation. Do not silently rewrite credentials or scopes.

Create accepts `given_name`, `surname`, `email_address` and optional `full_name`,
`phone_number`, `company`, `job_title`, `department`, `categories`; update uses
`item_id` and supplied name/email/phone/company/job-title/category fields.
Graph updates the primary (first) email/business-phone slot while preserving
remaining slots and home/mobile numbers. Empty strings clear the primary slot;
`categories=[]` clears categories. Retain `item_id` with its `target_mailbox`:
an ID from another account is not an interchangeable contact handle.

---

## Tasks

5 tools — full CRUD plus a quick-mark-complete shortcut.

| Tool | Notes |
|---|---|
| `create_task` | `subject`, `body`, `due_date`, `start_date`, `importance`, `reminder_time`, **`categories`** (v4) |
| `get_tasks` | filter by status / completion |
| `update_task` | partial update; `categories` (v4) |
| `complete_task` | sets status to Completed |
| `delete_task` | |

---

## Folders

3 tools.

### `list_folders`

Folder hierarchy with item counts.

| Name | Notes |
|---|---|
| `parent_folder` | Exchange-only starting point (`root` / `inbox` / etc.) |
| `depth` | Exchange-only recursion limit |
| `include_hidden` | Exchange-only system-folder control |

Graph/IMAP return the complete flat folder list; these hierarchy controls
do not filter it, and `parent_folder_id` is rejected. IMAP folder `id` and
`path` are native folder paths, usable as `folder` or `destination_folder`.

### `find_folder`

Find a folder by `query` (name fragment or path). Returns the stable
folder ID for use with `move_email` / `copy_email` /
`manage_folder(action="delete")`.

### `manage_folder`

Unified create / delete / rename / move via `action`. Either
`folder_id` (Exchange ID) or `folder_name` resolves the target.
Soft-delete (default) moves to Deleted Items via `Folder.move_to_trash`
when supported by the deployed exchangelib version, else falls back
to permanent delete with an explicit response label
("permanently deleted (no move_to_trash)") so the caller knows.

---

## Search

### `search_by_conversation`

Walk every mail folder by default and gather every message in a
thread. Custom-label / archived messages are not invisible. v4 fix:
`conversation_id` is wrapped in a typed `ConversationId` before
`folder.filter(...)` so the EWS SOAP restriction builds correctly
(was emitting `TypeError` per folder in v3.4 against newer
exchangelib versions).

```jsonc
{
  "items": [
    {
      "message_id": "AAMk...",
      "subject": "Re: Q1 review",
      "from": "alice@example.com",
      "received_time": "2026-04-30T...",
      "folder": "Inbox"
    }
  ],
  "count": 9,
  "conversation_id": "AAQk...",
  "searched_folders": ["Inbox", "Sent Items", "Archive", ...],
  "skipped_folders": [
    {"folder": "Calendar", "reason": "permission_denied", "error_type": "ErrorAccessDenied"}
  ]
}
```

---

## Out-of-Office

| Tool | Purpose |
|---|---|
| `oof_settings` | Get / set the user's OOF state. v4 fixes: preserves omitted reply text, normalises Scheduled `start`/`end` to UTC (fixes `InvalidScheduledOofDuration`), returns `currently_active` in the set response |
| `configure_oof_policy` | Persist a templated policy (auto-replies + forward rules + VIP passthrough) into the agent-side memory store |
| `get_oof_policy` | Retrieve the stored policy |
| `apply_oof_policy` | Evaluate the policy against an inbound message — **creates DRAFTS, never sends**, so the user can review |

---

## AI — semantic search

### `semantic_search_emails`

Vector cosine search over the SQLite-cached embedding store.

| Name | Type | Default | Notes |
|---|---|---|---|
| `query` | `string` | — | natural-language query |
| `folder` | `string` | `inbox` | |
| `max_results` | `int` | 10 | |
| `threshold` | `float` | 0.7 | min cosine similarity, 0.0-1.0 |
| `fields` | `string[]` | — | response projection |
| `exclude_automated` | `bool` | true | filter `noreply@` / `mailer-daemon` / "Automatic reply:" before ranking |
| `target_mailbox` | `string` | — | |

The query is embedded once via the configured `AI_EMBEDDING_MODEL`,
candidate emails are loaded with `.only(...)` to avoid pulling full
bodies, and existing embeddings are bulk-fetched from the SQLite
cache. Per-call cap is 50 freshly embedded items so a single call
doesn't blow up against a huge mailbox.

```jsonc
{
  "query": "Q1 budget approval",
  "result_count": 3,
  "results": [
    {
      "message_id": "AAMk...",
      "subject": "Re: Q1 review",
      "from": "alice@example.com",
      "similarity_score": 0.81,
      "received_time": "2026-04-30T...",
      "snippet": "Approved on Q1 budget. Two changes: ..."
    }
  ]
}
```

---

## Agent-secretary primitives

These are deterministic typed-record CRUD tools that an agent can use
to maintain its own state across conversations. None of them call an
LLM — that's intentional (see README "MCP / skill boundary"). All are
gated behind `ENABLE_AGENT=true` (default).

### Memory KV

| Tool | Purpose |
|---|---|
| `memory_set` | per-mailbox, namespaced JSON KV. `namespace`, `key`, `value`, optional `ttl_seconds`, optional `metadata`. 1 MiB value cap. |
| `memory_get` | by `(namespace, key)` |
| `memory_list` | by `namespace`; pagination |
| `memory_delete` | by `(namespace, key)` |

### Commitments

Manual CRUD only. The auto-extraction tool (`extract_commitments`)
was removed in v4; the consuming agent should detect commitments
in-prompt and call `track_commitment` for each.

| Tool | Purpose |
|---|---|
| `track_commitment` | record "I owe X to Y by Z" or "Y owes me X". `description`, `owner`, `counterparty`, `due_at`, `thread_id`, `message_id`, `excerpt` |
| `list_commitments` | `scope: "open" / "overdue" / "done"`, filter by `owner` |
| `resolve_commitment` | by `commitment_id`, `outcome: "done" / "cancelled"`, optional note |

### Approvals

Two-phase commit for side-effectful actions. The agent submits an
action, a human approves/rejects, then `execute_approved_action`
consumes the approval and runs the original tool.

| Tool | Purpose |
|---|---|
| `submit_for_approval` | `action: <tool_name>`, `arguments: {...}`, `reason`, `ttl_seconds`. Allow-listed actions only. |
| `list_pending_approvals` | |
| `approve` / `reject` | by `approval_id`, atomic single-use |
| `execute_approved_action` | by `approval_id` — runs the tool, consumes the approval |

### Voice profile

`get_voice_profile` returns the stored style card (formality,
greetings, signoffs, sample examples). v4 removed
`build_voice_profile`; the agent builds the profile in-prompt from
sample sent items and persists it via `memory_set`.

### Rule engine

Declarative match → actions automation, evaluated against new mail.

| Tool | Purpose |
|---|---|
| `rule_create` | `name`, `match: {subject_contains, from_contains, ...}`, `actions: [{type: "categorize\|flag_importance\|mark_read\|move_to_folder\|notify_agent\|track_commitment", ...}]`, `enabled` |
| `rule_list` | |
| `rule_delete` | by `rule_id` |
| `rule_simulate` | preview which rules would fire against a `message_id` |
| `evaluate_rules_on_message` | actually apply matching rules; `dry_run` flag |

### OOF policy

See [Out-of-Office](#out-of-office) above. `configure_oof_policy` /
`get_oof_policy` / `apply_oof_policy` operate on the policy stored
in the memory KV.

---

## Compound tools

These two tools return **structured JSON context** so the consuming
agent can compose prose itself. They do not call an LLM. (In v3.x
they did — that's been deliberately moved skill-side per the v4
"MCP / skill boundary" principle.)

### `generate_briefing`

Pulls the structured ingredients for a daily/weekly briefing.

| Name | Notes |
|---|---|
| `scope` | `"today"` / `"week"` |
| `since` | optional ISO timestamp, overrides scope |
| `include` | `string[]` — any of `inbox_delta`, `meetings`, `commitments`, `overdue_tasks`, `vip_activity` |
| `max_per_section` | int |

Response is a structured JSON object the agent renders into prose
in-prompt.

### `prepare_meeting`

Returns the meeting + attendees + per-attendee email history +
attachment text for an upcoming appointment, so the agent has all the
context it needs to brief you on the meeting.

| Name | Notes |
|---|---|
| `appointment_id` | required |
| `depth` | `"quick"` (envelope only) or `"deep"` (recent thread per attendee) |
| `history_per_attendee` | int |
| `extract_attachment_text` | bool — runs `read_attachment` on each meeting attachment |

---

## Removed in v4 (reasoning moved skill-side)

The following tools existed in v3.x and have been deliberately
**removed** — the consuming agent already has an LLM and can do these
tasks in-prompt with the data this MCP returns, which is faster,
cheaper, and produces better results than a second LLM hop:

- `classify_email`
- `summarize_email`
- `suggest_replies`
- `extract_commitments` (the auto-extraction; manual `track_commitment` remains)
- `build_voice_profile` (the LLM tone analysis; storage via `memory_set` remains)

See the [v4.0.0 entry in CHANGELOG.md](../CHANGELOG.md#v400--2026-05-03)
for the rationale and the in-prompt replacement pattern.
