> **Legacy 4.0 documentation.** This page describes the `src/` server as
> extended by this fork. Upstream's `ghcr.io/azizmazrou/ews-mcp:latest` image
> does not include the fork's additions, and the fork publishes no image (see
> the [README](../../README.md#quick-start)). For upstream's 4.5 line see
> [`v5/README.md`](../../v5/README.md) and [`v5/docs/API.md`](../../v5/docs/API.md).

# Agent Secretary Guide

The EWS MCP Server ships a set of **agentic** tools that let an LLM act as
an executive secretary: remember things across sessions, track
commitments, hold side-effects for human approval, apply rules, keep a
policy for when the user is out of office, and produce deterministic
briefings and meeting prep.

This guide walks through the 24 new tools, the data model they share, and
the security boundaries that keep them safe for production.

- [Architecture](#architecture)
- [Memory primitives](#memory-primitives)
- [Commitments](#commitments)
- [Approval queue (human-in-the-loop)](#approval-queue-human-in-the-loop)
- [Voice profile](#voice-profile)
- [Rule engine](#rule-engine)
- [OOF policy](#oof-policy)
- [Briefing](#briefing)
- [Meeting prep](#meeting-prep)
- [Security model](#security-model)
- [Configuration](#configuration)
- [FAQ](#faq)

## Architecture

The agent features share a single foundation: a **per-mailbox SQLite
memory store** (`src/memory/store.py`). Every new tool talks to this store
via typed repositories (`src/memory/models.py`) — no tool reaches into
SQLite directly, so the data model is centrally described and validated.

```
┌─────────────────────────────────────────────────────────────┐
│ MCP client  (Claude Desktop / Open WebUI / custom)          │
└──────────────┬──────────────────────────────────────────────┘
               │  stdio | SSE (bearer auth)
┌──────────────▼──────────────────────────────────────────────┐
│  Agent-secretary tools                                      │
│                                                             │
│  memory_*              commitments_*      approvals_*       │
│  rule_*                voice_*            oof_policy_*      │
│  generate_briefing    prepare_meeting                      │
│                                                             │
│           ▼ typed repositories (src/memory/models.py) ▼     │
│                                                             │
│  CommitmentRepo  ApprovalRepo  RuleRepo  VoiceRepo          │
│  OOFPolicyRepo                                              │
│                                                             │
│           ▼           MemoryStore  (SQLite, WAL)            │
│                                                             │
│  data/memory/mailbox-<sha256_prefix>.sqlite3                │
│  (owner-only, 1 MiB value cap, 50 MiB namespace cap)        │
└─────────────────────────────────────────────────────────────┘
```

Every record is keyed by the *primary authenticated* mailbox — impersonated
target mailboxes do **not** get their own state (by design: the operator
is the primary account, and per-target state would either leak across
service accounts or balloon into a per-target-mailbox DB per call).

## Memory primitives

Four MCP tools expose the KV directly for ad-hoc agent state (use the
typed tools below for structured data).

| Tool | Purpose |
|------|---------|
| `memory_set` | Store a JSON value under `(namespace, key)`, optional TTL |
| `memory_get` | Fetch by namespace + key |
| `memory_list` | List entries in a namespace, optional key prefix |
| `memory_delete` | Remove an entry |

Reserved namespaces — `approval`, `rule`, `oof.policy` — are refused from
the generic tools so the repositories remain the only writers.

### Example

```jsonc
// The agent remembers how a colleague likes meetings.
memory_set {
  "namespace": "person.note",
  "key": "alice.al-rashid",
  "value": {
    "prefers": "morning meetings",
    "lang": "Arabic",
    "allergy": "shellfish"
  }
}

// Later, when drafting an invite:
memory_get { "namespace": "person.note", "key": "alice.al-rashid" }
```

## Commitments

A commitment is a tracked promise — "I owe X to Y by Z", or "Y owes me
X". Typical flow:

1. The agent (or user) creates a commitment with `track_commitment`.
2. `list_commitments` surfaces what's open, overdue, or done.
3. `resolve_commitment` closes one out when complete.

> **v4 — auto-extraction is now skill-side.** v3.x exposed
> `extract_commitments`, which proxied the email body through an LLM
> on the MCP and saved any detected commitments. v4 removed that tool
> per the [MCP / skill boundary principle](../docs/ARCHITECTURE.md#mcp--skill-boundary):
> the consuming agent already has an LLM, so detection happens
> in-prompt and the agent then issues one or more `track_commitment`
> calls per item it found. Stored fields include the message/thread
> ID, the counterparty, a due timestamp, and a short excerpt.

### Example

```jsonc
track_commitment {
  "description": "Send Alice the Q1 budget numbers",
  "owner": "me",
  "counterparty": "alice@corp.com",
  "due_at": "2026-04-25T17:00:00Z",
  "thread_id": "AAQk...",
  "message_id": "AAMk...",
  "excerpt": "...will get back to you with the numbers by Friday..."
}

list_commitments { "scope": "overdue" }
resolve_commitment {
  "commitment_id": "…", "outcome": "done", "note": "sent via email"
}
```

## Approval queue (human-in-the-loop)

For side-effectful actions the agent **submits instead of executes**:

1. `submit_for_approval` queues an action + arguments, returns an
   approval id.
2. A human sees it via `list_pending_approvals`.
3. `approve` or `reject` decides.
4. `execute_approved_action` atomically consumes the approval and runs
   the underlying tool.

Approval IDs are UUID4 and single-use — the `MemoryStore.consume`
primitive guarantees no double-spend even under concurrent calls.

Allow-listed actions (see `src/memory/models.py:ApprovalRepo`):

```
send_email, reply_email, forward_email, delete_email, move_email,
create_appointment, update_appointment, delete_appointment,
create_contact, update_contact, delete_contact,
create_task, update_task, delete_task, complete_task,
manage_folder, add_attachment, delete_attachment,
oof_settings, configure_oof_policy
```

### Example

```jsonc
submit_for_approval {
  "action": "send_email",
  "arguments": {
    "to": ["ceo@corp.com"],
    "subject": "Re: quarterly outlook",
    "body": "<p>Attached is the revised deck.</p>"
  },
  "ttl_seconds": 1800,
  "reason": "AI drafted reply — needs review before send"
}
// → { approval_id: "8a…" }

approve { "approval_id": "8a…", "reason": "LGTM" }

execute_approved_action { "approval_id": "8a…" }
// → actually sends the mail; approval record consumed.
```

### Dry-run flag on `send_email` / `reply_email` / `forward_email`

`dry_run=true` previews without sending or modifying a message. In compose
mode it creates no draft and validates `files` entries without fetching their
content (`files_validated: true`, `files_fetched: false`, `would_attach`);
add `verify_files=true` to load every file and get name/size/content_type per
entry. Deprecated Exchange `attachments` paths are still read. Exchange
`reply_email`/`forward_email` previews also read the original message and
return `would_send_to`. With `send_email(draft_id=…)`, it authenticates and
reads the existing draft, returning stored recipients, subject and a body
preview without sending, editing or deleting it. Invalid, missing or
unreadable drafts can therefore fail during preview. Attachment counts in the
preview vary by provider.

### Attaching files from a remote agent

The agent box and the Email container share no filesystem: never pass a
`/workspace/...` path. Either send the bytes inline —
`files=[{"file_content": b64, "file_name": "photo.jpg"}]` — or call
`stage_outgoing_file(file_content=b64, file_name="photo.jpg")` once and reuse
the returned `file_path` in `files` for the dry run and the real send. Aliases
such as `filename`, `content`, `base64`, `data` and `url` are accepted.

### Reviewable drafts across providers

Use `list_mailboxes` to select an account and check its capabilities; one
mailbox's calendar, contacts or scheduling access does not imply another's.
`create_draft`, `create_reply_draft`, `create_forward_draft` and `update_draft`
work on Exchange, Graph and IMAP with the same public tool names.

Use canonical `files` when creating a draft: remote agents use base64
`file_content`, `file_url`, or a `stage_outgoing_file` path (see above);
`file_path` only names files on the Email server. Legacy
`attachments`/`inline_attachments` remain supported only on their existing
Exchange paths and are deprecated.

Keep the returned current `id`/`message_id`, `mailbox`, `folder` and attachment
IDs together. `update_draft` changes only supplied `subject`/`body`:
omission preserves, `""` clears (explicit null is rejected); `body_format`
defaults to `markdown`.
Recipients and attachments remain unchanged. IMAP rewrites yield new message
and attachment part IDs, so never continue with an old ID after an edit.

After review, call `send_email(draft_id=…, target_mailbox=…, folder="drafts")`
with no compose overrides. An approval to send a draft is not a frozen-content
snapshot: avoid intervening edits and obtain renewed review if content changes.
Do not assume draft creators/editors are approval-queue allow-listed merely
because they are public tools.

Graph multi-step failures retain new drafts by default and return a recovery
`draft_id`, `folder`, `stage`, cleanup information and submission uncertainty
when available. IMAP draft creators also honor `cleanup_on_failure` if
metadata/relisting fails after creation and the new ID is known. The option
defaults to false and only applies to a draft created by that call, never a
caller's existing draft; Graph skips cleanup when submission is uncertain.
Inspect a retained
partial draft before resuming. Accepted SMTP recipients are not confirmed
deliveries: inspect `accepted_recipients`, `refused_recipients` and
`partial_failure`; do not resend to accepted recipients or blindly retry an
uncertain submission. See [draft recovery](API.md#draft-recovery).

Core calendar/contact operations support Exchange, Graph and iCloud/DAV. Graph
requires calendar/contact consent. Its default delegated refresh scope already
requests `Calendars.ReadWrite` and `Contacts.ReadWrite`, and a custom
per-account `settings.graph_scope` must include them (see [API](API.md#calendar)).
Graph free/busy and meeting suggestions require an organization account.
iCloud CalDAV/CardDAV uses the app-specific password and supports event/contact
CRUD behind the same tools, but not organization free/busy, meeting-time search,
GAL/history/domain lookup or communication statistics. `find_person` is
contacts-only on Graph/DAV (default `all` reports partial coverage).
Tasks and `analyze_contacts` remain Exchange-only. Scheduling/undo require
SmarterMail REST.

For SmarterMail, `schedule_email(draft_id=…, send_at=…, files=[…])`
schedules a reviewed saved Exchange draft without compose overrides; use
`update_draft` before scheduling if content must change. Files append, stored
attachments/CIDs remain, and native scheduling consumes the source into
Scheduled. Track the returned numeric Scheduled `uid` separately from the
source EWS `draft_id`; use it with `cancel_scheduled_email`.

On failure inspect `stage`, `submission_uncertain` and `recovery_hint`, then
check both Drafts and Scheduled before retrying. Appended files can persist,
and uncertain submission may already have consumed the source. A successful
queue operation is not delivery. Removal reports `removed_from_queue:true`,
not confirmed prevention: `cancelled:null`, `cancellation_confirmed:false`,
`delivery_prevented:null`. An absent UID or native undo acknowledgment cannot
prove delivery stopped. Do not tell the user a message was recalled or
definitively cancelled based on these responses.

## Voice profile

A voice profile is a JSON style card (formality, typical greetings,
typical sign-offs, structure notes, sample examples) that
draft-generation tools can use as few-shot material to produce text
that "sounds like the user".

> **v4 — building the card is skill-side.** v3.x exposed
> `build_voice_profile`, which sampled up to 200 sent messages and
> asked the configured AI provider to emit the style card. v4 removed
> that tool. The consuming agent now reads sample sent items via
> `read_emails(folder="sent")`, derives the style card in-prompt, and
> persists it via the memory KV under namespace `voice_profile`:
>
> ```jsonc
> memory_set {
>   "namespace": "voice_profile",
>   "key": "current",
>   "value": { "formality": "professional", "greetings": [...], ... }
> }
> ```

`get_voice_profile` reads the stored card back. The same JSON shape
v3.x produced is used so existing skill prompts that consume the card
keep working.

### Example

```jsonc
get_voice_profile
// → { "has_profile": true, "profile": {...} }
```

## Rule engine

Declarative automations: "when X, do Y". Rules have:

- **Match**: allow-listed keys — `from`, `to`, `subject_contains`,
  `body_contains`, `has_attachment`, `is_unread`, `categories_any`,
  `categories_all`, `importance`. `from`/`to` support `fnmatch` wildcards.
- **Actions**: allow-listed types — `flag_importance`, `categorize`,
  `move_to_folder`, `mark_read`, `track_commitment`, `notify_agent`.

There is **no background watcher** today — rules fire when the agent
invokes `evaluate_rules_on_message` on a specific message. `rule_simulate`
reports which rules *would* fire, without mutating anything.

### Example

```jsonc
rule_create {
  "name": "CEO urgent handler",
  "match": {
    "from": "ceo@corp.com",
    "subject_contains": "urgent",
    "is_unread": true
  },
  "actions": [
    { "type": "flag_importance", "importance": "High" },
    { "type": "categorize", "categories": ["CEO", "Action"] },
    { "type": "notify_agent", "note": "Draft acknowledgement reply" },
    {
      "type": "track_commitment",
      "description": "Acknowledge CEO urgent message",
      "owner": "me"
    }
  ]
}

rule_simulate { "message_id": "AAMkAGI…" }
evaluate_rules_on_message { "message_id": "AAMkAGI…", "dry_run": false }
```

## OOF policy

`configure_oof_policy` stores a policy alongside the native Exchange OOF:
internal / external reply templates, a `vip_passthrough` flag, and
**forward rules** — `{match, to, reason}` triples that reuse the rule
engine's match vocabulary.

`apply_oof_policy` evaluates the forward rules against one message. When a
match fires and `dry_run=false`, it creates a **draft** forward (never
sends). The user reviews drafts on return.

### Example

```jsonc
configure_oof_policy {
  "internal_template": "I'm out until Monday. For urgent items contact Alice.",
  "external_template": "Thanks for your note — I'm out of office until Monday.",
  "vip_passthrough": true,
  "forward_rules": [
    { "match": { "from": "*@bigcustomer.com" }, "to": "alice@corp.com", "reason": "Key account" },
    { "match": { "categories_any": ["Finance"] }, "to": "finance-ops@corp.com" }
  ]
}

apply_oof_policy { "message_id": "AAMkAGI…", "dry_run": true }
```

## Briefing

`generate_briefing` composes a single JSON payload the LLM renders for the
user. It is **compound** — the tool calls several primitives and returns
the aggregate. Sections (each independently toggle-able via `include`):

| Section | Source |
|---------|--------|
| `inbox_delta` | unread messages since `since` (or "today") |
| `meetings` | calendar view for the briefing window |
| `commitments` | open commitments due in the window + overdue |
| `overdue_tasks` | Exchange tasks with `due_date < today` |
| `vip_activity` | messages from frequent recent senders |

Fully deterministic; no AI call. Truncates preview fields to 200 chars so
payloads stay small.

### Example

```jsonc
generate_briefing { "scope": "today" }
generate_briefing { "scope": "weekly", "include": ["meetings", "commitments"] }
```

## Meeting prep

`prepare_meeting` takes an appointment ID and returns a brief:

- Meeting metadata (subject, start/end, location, organizer, attendees)
- Per attendee — stored `person.note` + last emails with them (in both
  directions) + open commitments involving them
- Related thread — a recent subject-match search
- Attachments on the invite — name + size; optionally extracted text
  snippet for PDF/DOCX/XLSX when `extract_attachment_text=true`

### Example

```jsonc
prepare_meeting {
  "appointment_id": "AAMkAGI…",
  "depth": "deep",
  "history_per_attendee": 10,
  "extract_attachment_text": true
}
```

## Security model

Every design choice in the agent stack maps to a threat:

| Threat | Mitigation |
|--------|------------|
| Cross-mailbox data leak | Per-mailbox DB file; hash-derived filename; primary-key includes mailbox |
| SQL injection | All queries use `?` placeholders only |
| Path traversal in DB filename | File path resolved and verified inside `EWS_MEMORY_DIR` |
| Path traversal in namespace/key | Alphabet restricted to `[A-Za-z0-9._:-]{1,128}` |
| Unbounded value growth | 1 MiB per value, 50 MiB per namespace (LRU prune) |
| Approval double-spend | `MemoryStore.consume` is atomic and re-checks status |
| Arbitrary code in rule actions | Explicit allow-list in `_ALLOWED_ACTION_TYPES` — no `eval` |
| Arbitrary match keys (SQL bait) | `RuleRepo.validate_match` allow-list |
| Sensitive fields in audit log | Reuses `redact_sensitive` from logging middleware |
| AI cost explosion | Voice: 200 samples × ~1.5 KB; extraction: ~6 KB body cap |
| Forward-rule spoofing | Forwards create DRAFTS (never send); basic email regex on destination |

Tools inherit the existing middleware: rate limit, circuit breaker,
structured audit log, and `json` serialisation over the MCP transport.
See [Security & Ops](../README.md#security--operations-notes) for the
transport-level defaults (bearer auth, TLS verification, jailed
downloads).

## Configuration

All agent features are on by default; toggle with `ENABLE_AGENT=false`
to exclude the agent-secretary tools from the tool registry entirely.

| Variable | Default | Purpose |
|----------|---------|---------|
| `ENABLE_AGENT` | `true` | Register the agent-secretary tools |
| `EWS_MEMORY_DIR` | `data/memory` | Jail for per-mailbox memory store |

`ENABLE_AI` is no longer required for any agent-secretary tool in v4 —
only `semantic_search_emails` (in the AI category) needs an embedding
provider configured. The auto-extraction tools that did need an LLM
(`extract_commitments`, `build_voice_profile`) were removed.
| `AI_PROVIDER` | `openai` | `openai` / `anthropic` / `local` |

## FAQ

**Q: Can the agent act on my mailbox without me seeing it?**
By default the rule engine only fires when you explicitly call
`evaluate_rules_on_message`. There is no background watcher yet, so
auto-actions require a prompt or a scheduled job outside ews-mcp. All
side-effectful tools go through the audit log.

**Q: Where are my secrets / API keys / tokens?**
Not in the memory store. `redact_sensitive` in the logging middleware
replaces `password`/`token`/`secret`/`body`/`file_content` fields with
placeholders before they reach the audit log. Keys live in env vars only.

**Q: Can I wipe state for a single mailbox?**
Delete `data/memory/mailbox-<prefix>.sqlite3` (the file name is a
SHA-256 prefix of the mailbox, not the email itself). There's no
cross-mailbox data in a single file.

**Q: Is this Exchange Online only?**
No — the memory and rule layers are Exchange-agnostic. They talk to
`exchangelib`'s account object, which handles both on-prem EWS and
Exchange Online.

**Q: How do I call an approved action twice?**
You don't. `consume()` is atomic and `execute_approved_action` rejects
any redemption of a consumed approval. To retry after a transient error,
submit a fresh approval.
