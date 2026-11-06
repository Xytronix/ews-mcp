# EWS MCP Server (fork)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

This repository is a fork of
[azizmazrou/ews-mcp](https://github.com/azizmazrou/ews-mcp), a Model Context
Protocol server that gives an LLM client typed access to a Microsoft Exchange
mailbox over EWS. The fork extends upstream's 4.0 server in [`src/`](src/) and
keeps its tool names. It adds:

- **More mailbox backends behind the same tools.** Microsoft Graph serves
  Outlook.com and Microsoft 365 mailboxes. IMAP/SMTP serves iCloud, Gmail and
  generic IMAP servers. iCloud CalDAV/CardDAV serves iCloud calendars and
  contacts. Mail, draft and attachment tools route to every backend, and the
  core calendar and contact tools route to Graph and iCloud.
- **Multi-mailbox accounts.** Accounts come from the `MAIL_ACCOUNTS`
  environment variable, single-account convenience variables or a Supabase
  table. `list_mailboxes` lists them with their capabilities, and
  `target_mailbox` selects one per call.
- **Opt-in SmarterMail REST tools.** 39 tools for scheduled send,
  signatures, notes, recurring events, file storage and domain administration
  on a SmarterMail server.
- **Liteparse attachment parsing.** `read_attachment` extracts text from PDF,
  Office, OpenDocument and image files (with OCR), and from archives, calendar
  and contact files, Outlook MSG, EML and many other formats.
- **Attachments from remote agents.** A client that shares no filesystem with
  the server can send files as base64, as a URL the server fetches, as a
  SmarterMail storage file, or stage them on the server first.
- **Native streamable HTTP** at `/mcp`, which is the Docker image default.
- **Tool-call pacing and EWS throttle backoff.** Calls wait for a rate-limit
  slot instead of failing, and `search_emails` retries EWS throttling.

The fork also fixes a set of EWS defects (meeting invitations, task and
contact access, folder rename and move, attachment payloads, HTML-only
messages) and reports outbound mail as accepted by the mail server rather than
delivered. [CHANGELOG.md](CHANGELOG.md) lists every change.

Upstream's 4.5 line in [`v5/`](v5/) is included unchanged. It is a separate
server with its own tool surface, and none of the fork's changes apply to it.
Its documentation is [`v5/README.md`](v5/README.md).

The fork lives at https://github.com/Xytronix/ews-mcp. The `dev` branch
carries the fork and is the default branch. `main` mirrors upstream.

## Quick start

The fork publishes no container image. Build it from source.

### Docker

```bash
git clone https://github.com/Xytronix/ews-mcp.git
cd ews-mcp
cp .env.example .env        # fill in credentials and set MCP_API_KEY
docker build -t ews-mcp .
docker run -d --name ews-mcp --env-file .env -p 8000:8000 ews-mcp
curl http://localhost:8000/health
```

The image runs `python -m src.main` with `MCP_TRANSPORT=streamable-http`,
`MCP_HOST=0.0.0.0` and `MCP_PORT=8000`. The MCP endpoint is `/mcp`, and
`/health` answers without authentication. Because the bind is not loopback,
the server refuses to start unless `MCP_API_KEY` is set. Clients send the key
as `Authorization: Bearer <key>`, as `X-API-Key: <key>` or as
`?api_key=<key>`. `MCP_ALLOW_UNAUTHENTICATED=true` starts the server without a
key. Use it only when the port is reachable on a private docker network and is
never published.

Register the server with any client that speaks streamable HTTP, for example
Claude Code:

```bash
claude mcp add --transport http exchange http://localhost:8000/mcp \
  --header "Authorization: Bearer <your MCP_API_KEY>"
```

`docker compose up -d` builds and runs the same image from
[`docker-compose.yml`](docker-compose.yml). That file uses host networking, so
the server listens on port 8000 of the host and `.env` needs `MCP_API_KEY`.

The image installs LibreOffice (Writer, Calc, Impress), Tesseract, ImageMagick,
libheif, unar and openssl for attachment parsing.

### stdio from source

```bash
git clone https://github.com/Xytronix/ews-mcp.git
cd ews-mcp
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in credentials
python -m src.main
```

Outside the image `MCP_TRANSPORT` defaults to `stdio`. Python 3.11 or newer is
required, and the image uses 3.12. The server reads `.env` and resolves its
default `data/` and `downloads/` directories relative to the working
directory, so an MCP client should start it from the repository root:

```bash
claude mcp add exchange -- sh -c 'cd /path/to/ews-mcp && exec .venv/bin/python -m src.main'
```

Variables marked `env` in the [fork variable table](#fork-added-variables) are
read from the process environment only. A `.env` file does not set them for
`python -m src.main`, so export them or pass them in the client's `env` block.
The Dockerfile installs system packages for attachment parsing (listed above).
Install them too if you need those formats outside Docker.

## Configuration

### Core Exchange settings

The 4.0 server always starts from a primary Exchange mailbox, even when the
other accounts use Graph or IMAP. The most important settings:

| Variable | Default | Meaning |
|---|---|---|
| `EWS_EMAIL` | required | Primary mailbox address |
| `EWS_SERVER_URL` | unset | EWS endpoint as a full URL or a host name |
| `EWS_AUTODISCOVER` | `true` | Use autodiscover when `EWS_SERVER_URL` is unset |
| `EWS_AUTH_TYPE` | `oauth2` | `oauth2`, `basic` or `ntlm` |
| `EWS_CLIENT_ID`, `EWS_CLIENT_SECRET`, `EWS_TENANT_ID` | unset | Required for `oauth2` |
| `EWS_USERNAME`, `EWS_PASSWORD` | unset | Required for `basic` and `ntlm`. The password may come from the Supabase table instead |
| `TIMEZONE` | `UTC` | IANA time zone name |
| `MCP_TRANSPORT` | `stdio` | `stdio`, `sse` or `streamable-http`. The image sets `streamable-http` |
| `MCP_HOST`, `MCP_PORT` | `127.0.0.1`, `8000` | HTTP bind. The image sets `0.0.0.0` |
| `MCP_API_KEY` | unset | Key for HTTP clients. Required for a non-loopback bind |

[`docs/legacy/SETUP.md`](docs/legacy/SETUP.md) walks through Exchange
authentication. [`.env.example`](.env.example) lists the remaining upstream
settings (feature toggles, cache, AI, impersonation) and every fork variable.

### Fork-added variables

Source `settings` means the value is read from the environment or from `.env`.
Source `env` means it is read from the process environment only.

| Variable | Default | Source | Meaning |
|---|---|---|---|
| `MCP_TRANSPORT` | `stdio` | settings | The fork adds the `streamable-http` value |
| `MCP_ALLOW_UNAUTHENTICATED` | `false` | settings | Allow a non-loopback `sse` or `streamable-http` bind without `MCP_API_KEY` |
| `STAGE_UPLOAD_TOKEN` | unset | env | Enables `POST /stage` on streamable-http. Unset, the route returns 404 |
| `MAIL_ACCOUNTS` | unset | env | JSON array of mailbox accounts, see [Mailbox accounts](#mailbox-accounts) |
| `ICLOUD_EMAIL` | unset | env | Adds an iCloud mailbox (IMAP/SMTP and CalDAV/CardDAV) |
| `ICLOUD_APP_PASSWORD` | unset | env | iCloud app-specific password. `ICLOUD_PASSWORD` is accepted as well |
| `ICLOUD_LABEL` | unset | env | Display label for the iCloud mailbox |
| `ICLOUD_CALDAV_URL` | `https://caldav.icloud.com/` | env | CalDAV discovery root |
| `ICLOUD_CARDDAV_URL` | `https://contacts.icloud.com/` | env | CardDAV discovery root |
| `GMAIL_EMAIL` | unset | env | Adds a Gmail mailbox over IMAP/SMTP. Skipped without `GMAIL_APP_PASSWORD` |
| `GMAIL_APP_PASSWORD` | unset | env | Google app password. Spaces are removed |
| `GMAIL_LABEL` | `Gmail` | env | Display label for the Gmail mailbox |
| `OUTLOOK_EMAIL` | unset | env | Adds an Outlook.com or Microsoft 365 mailbox over Graph |
| `OUTLOOK_CLIENT_ID` | unset | env | Entra app client ID. Required |
| `OUTLOOK_REFRESH_TOKEN` | unset | env | Delegated refresh token. Requests go to `/me` |
| `OUTLOOK_CLIENT_SECRET` | unset | env | Client secret. Without a refresh token the backend uses app-only client credentials and `/users/<email>` |
| `OUTLOOK_TENANT_ID` | `common` | env | Tenant used in the token URL |
| `OUTLOOK_GRAPH_SCOPE` | `offline_access` plus Graph `User.Read`, `Mail.ReadWrite`, `Mail.Send`, `Calendars.ReadWrite`, `Contacts.ReadWrite` | env | Scope sent on delegated token refresh. App-only uses `https://graph.microsoft.com/.default` |
| `OUTLOOK_LABEL` | unset | env | Display label for the Graph mailbox |
| `GRAPH_BASE_URL` | `https://graph.microsoft.com` | env | Graph endpoint for the `OUTLOOK_EMAIL` mailbox |
| `GRAPH_TOKEN_URL` | `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token` | env | Token endpoint for the `OUTLOOK_EMAIL` mailbox |
| `SUPABASE_URL` | unset | settings | Supabase project URL for the accounts table |
| `SUPABASE_SERVICE_KEY` | unset | settings | Supabase service-role key |
| `EWS_ACCOUNTS_TABLE` | `ews_accounts` | settings | Accounts table name |
| `SMARTERMAIL_ENABLED` | `false` | settings | Register the 31 SmarterMail REST tools |
| `SMARTERMAIL_BASE_URL` | scheme and host of `EWS_SERVER_URL` | settings | SmarterMail web address |
| `SMARTERMAIL_ADMIN_TOOLS` | `false` | settings | Also register the 8 domain-administration tools |
| `EWS_MCP_STAGING_DIR` | `/tmp/ews-mcp-staged` | env | Directory for staged outbound files |
| `RATE_LIMIT_REQUESTS_PER_MINUTE` | `120` | settings | Tool calls per minute. Upstream's default is 25 |
| `RATE_LIMIT_MAX_WAIT_SECONDS` | `60` | env | How long a tool call waits for a rate-limit slot before it fails |
| `EWS_THROTTLE_MAX_RETRIES` | `4` | env | `search_emails` retries after EWS throttling |
| `EWS_THROTTLE_BASE_DELAY` | `3` | env | First backoff delay in seconds, doubled on each retry |
| `EWS_THROTTLE_MAX_DELAY` | `30` | env | Longest backoff delay in seconds |
| `OUTBOUND_TRACKING_HEADER` | unset (off) | env | Header name that carries a per-message tracking ID on fresh outbound mail, for example `X-Mail-Tracking-ID`. Recipients can see it. Unset or empty adds no header, and sends report `tracking_id: null` |
| `EWS_SEARCH_ALL_MAX_FOLDERS` | `50` | env | Most mail folders `search_scope=["all"]` searches on Exchange. A mailbox with more fails with a clear error |

### Mailbox accounts

Accounts merge in this order, and a later source wins on the same address:
the primary `EWS_EMAIL` mailbox, the Supabase table, then `MAIL_ACCOUNTS` and
the `ICLOUD_*`, `GMAIL_*` and `OUTLOOK_*` variables. An account with missing
credentials is skipped with a warning.

`MAIL_ACCOUNTS` holds a JSON array on one line. As a shell export:

```bash
export MAIL_ACCOUNTS='[{"email":"icloud@example.com","provider":"icloud","password":"app-specific-password"},{"email":"imap@example.com","provider":"imap","password":"secret","imap_host":"imap.example.com","smtp_host":"smtp.example.com"},{"email":"outlook@example.com","provider":"outlook","client_id":"app-client-id","refresh_token":"refresh-token"}]'
```

In a file for `docker run --env-file`, write `MAIL_ACCOUNTS=[...]` without
the surrounding quotes, because Docker keeps them as part of the value.

Each object takes `email`, `password` (or `app_password`), `provider`, an
optional `label` and connection keys either at the top level or inside a
`settings` object: `imap_host`, `imap_port` (993), `imap_ssl` (true),
`imap_username`, `smtp_host`, `smtp_port` (587), `smtp_starttls` (true),
`smtp_ssl` (false), `smtp_username`, `sent_folder`, `caldav_url`,
`carddav_url`, `client_id`, `client_secret`, `tenant_id`, `refresh_token`,
`graph_base_url`, `graph_token_url`, `graph_scope` and `shared_mailbox`.

| `provider` | Backend | Preset or requirement |
|---|---|---|
| `icloud`, `apple` | IMAP/SMTP plus CalDAV/CardDAV | `imap.mail.me.com:993` SSL, `smtp.mail.me.com:587` STARTTLS, iCloud DAV roots |
| `gmail`, `google` | IMAP/SMTP | `imap.gmail.com:993` SSL, `smtp.gmail.com:587` STARTTLS |
| `imap` | IMAP/SMTP | none, set `imap_host` and `smtp_host` |
| `outlook`, `graph`, `office365`, `o365`, `m365`, `microsoft365` | Microsoft Graph | needs `client_id` plus `refresh_token` or `client_secret` |
| `ews`, `exchange`, `smartermail` | Exchange (EWS) on the primary server | own passwords only from Supabase table rows, see below |

The Supabase table is read once at startup over the Supabase REST API with
the service-role key. It needs `email` and `password` columns, and optional
`provider` (default `ews`) and `settings` (jsonb) columns hold the same keys as
`MAIL_ACCOUNTS`. Keep row level security enabled with no public policy.
Exchange rows give additional mailboxes on the primary EWS server their own
passwords, and they are the only way to do so. Without such a row,
`target_mailbox` on Exchange needs impersonation or delegate access
(`EWS_IMPERSONATION_ENABLED=true`, see
[`docs/legacy/IMPERSONATION.md`](docs/legacy/IMPERSONATION.md)).

## Tool surface

With default settings the server registers 70 tools.
`SMARTERMAIL_ENABLED=true` adds 31 (101 in total), and
`SMARTERMAIL_ADMIN_TOOLS=true` adds the remaining 8 (109). Setting
`ENABLE_EMAIL`, `ENABLE_CALENDAR`, `ENABLE_CONTACTS`, `ENABLE_TASKS` or
`ENABLE_AGENT` to false removes the matching tools. The runtime `tools/list`
is authoritative.

| Category | # | Tools |
|---|---|---|
| **Email** | 15 | `send_email` · `read_emails` · `search_emails` · `get_email_details` · `get_emails_bulk` · `delete_email` · `move_email` · `copy_email` · `update_email` · `reply_email` · `forward_email` · `create_draft` · `create_reply_draft` · `create_forward_draft` · `update_draft` |
| **Mailboxes** | 1 | `list_mailboxes` |
| **Attachments** | 9 | `list_attachments` · `download_attachment` · `add_attachment` · `delete_attachment` · `read_attachment` · `get_email_mime` · `attach_email_to_draft` · `stage_outgoing_file` · `delete_staged_file` |
| **Calendar** | 7 | `create_appointment` · `get_calendar` · `update_appointment` · `delete_appointment` · `respond_to_meeting` · `check_availability` · `find_meeting_times` |
| **Contacts** | 5 | `create_contact` · `update_contact` · `delete_contact` · `find_person` · `analyze_contacts` |
| **Tasks** | 5 | `create_task` · `get_tasks` · `update_task` · `complete_task` · `delete_task` |
| **Folders** | 3 | `list_folders` · `find_folder` · `manage_folder` |
| **Search** | 1 | `search_by_conversation` |
| **Out of office** | 4 | `oof_settings` · `configure_oof_policy` · `get_oof_policy` · `apply_oof_policy` |
| **Memory** | 4 | `memory_set` · `memory_get` · `memory_list` · `memory_delete` |
| **Commitments** | 3 | `track_commitment` · `list_commitments` · `resolve_commitment` |
| **Approvals** | 5 | `submit_for_approval` · `list_pending_approvals` · `approve` · `reject` · `execute_approved_action` |
| **Voice** | 1 | `get_voice_profile` |
| **Rules** | 5 | `rule_create` · `rule_list` · `rule_delete` · `rule_simulate` · `evaluate_rules_on_message` |
| **Compound** | 2 | `generate_briefing` · `prepare_meeting` |
| **AI** | 1 | `semantic_search_emails`, registered only with `ENABLE_AI=true` and `ENABLE_SEMANTIC_SEARCH=true` |

The per-tool reference is [`docs/legacy/API.md`](docs/legacy/API.md).

### SmarterMail REST tools

These tools call SmarterMail's private JSON API at `SMARTERMAIL_BASE_URL`
and log in with each mailbox's own password: `EWS_PASSWORD` (or its table row)
for the primary mailbox and the Supabase table for the others. They only work
against a SmarterMail server and refuse Graph and IMAP mailboxes.

| Group | Tools |
|---|---|
| Mail | `schedule_email` · `list_scheduled_emails` · `cancel_scheduled_email` · `undo_send` · `bulk_update_emails` · `export_emails` · `unsubscribe_email` · `manage_mail_classification` |
| Settings | `manage_signatures` · `manage_senders` · `manage_auto_clean` · `manage_notes` · `manage_disposable_address` · `manage_notifications` · `manage_auto_complete` · `manage_account_security` |
| Calendar and contacts | `manage_recurring_event` · `update_event_occurrence` · `manage_calendars` · `manage_internet_calendars` · `import_export_calendar` · `manage_address_books` · `manage_contacts_bulk` |
| Workspace | `manage_virtual_folders` · `manage_folder_sharing` · `manage_file_storage` · `attach_file_link` · `manage_rss_feeds` · `search_chat_history` · `mailbox_usage_report` · `manage_connected_services` |
| Domain admin | `manage_domain_users` · `manage_user_access` · `manage_domain_aliases` · `manage_mailing_lists` · `manage_user_groups` · `manage_domain_mail_policy` · `manage_event_hooks` · `manage_mailbox_migration` |

- `SMARTERMAIL_ENABLED=true` registers the first four groups.
- `SMARTERMAIL_ADMIN_TOOLS=true` also registers the domain-admin group. Those
  tools need a domain administrator account and can create, rename, disable
  and delete mailboxes.
- When a call names no time zone, the recurring-event tools use the server
  `TIMEZONE` setting, which is UTC unless set.
- `storage_file_id` attachment sources also need `SMARTERMAIL_ENABLED=true`.

## Provider scope and limits

Support is per operation, not full Exchange parity. Use `list_mailboxes` to see
each configured account and its `capabilities`, and pass `target_mailbox` (or
its alias `mailbox`) to select one. Omitting it uses the primary mailbox.

| Capability | Exchange | Graph | IMAP/SMTP |
|---|---|---|---|
| Folders, read, search, details, move, delete | Yes | Yes | Yes. IDs are folder-scoped |
| Send, reply, forward with `files` and send a saved draft | Yes | Yes. New `files` up to 3 MiB in total | Yes. SMTP acceptance is not delivery |
| Create compose, reply and forward drafts and `update_draft` | Yes | Yes | Yes. Edits rewrite the message and change IDs |
| Attachments and raw MIME | Yes | Yes. Changes only on drafts | Yes. Changes only in Drafts |
| Calendar event CRUD | Yes | Yes, with calendar consent | iCloud through CalDAV |
| Recurring series and reminders | Yes. Occurrence vs `scope="series"` guard | Yes | Reminders yes; creating a series is refused |
| Whole-mailbox search (`search_scope=["all"]`) | Advanced and full_text | Quick and full_text | No |
| Follow-up flag state (`flag_status`) | NotFlagged, Flagged, Complete | NotFlagged, Flagged, Complete | Flagged and NotFlagged only |
| Free/busy and meeting-time suggestions | Yes | Organization accounts only | No |
| Contact CRUD and `find_person` | Yes, including GAL and mail history | Contacts only, with contact consent | iCloud through CardDAV, contacts only |
| Tasks, `analyze_contacts` and other EWS-only tools | Yes | No | No |
| Scheduled send and undo | SmarterMail REST tools only | No | No |

- For IMAP messages outside Inbox, pass the source `folder`. Keep the
  mailbox, folder and current message ID together, and use the fresh IDs a
  draft edit or attachment change returns.
- Use canonical `files` for attachments. `file_path` names a file on the
  server, never on the caller's machine. A remote client sends base64
  `file_content` with `file_name`, a `file_url` the server fetches (HTTP(S)
  only, private and loopback addresses refused, 25 MiB), a SmarterMail
  `storage_file_id`, or a `file_path` returned by `stage_outgoing_file`.
  Staged files expire after one hour by default (one minute to 24 hours).
  `POST /stage` stages a raw request body of up to 25 MiB when
  `STAGE_UPLOAD_TOKEN` is set. Legacy `attachments` and `inline_attachments`
  remain on Exchange only and are deprecated.
- `send_email`, `reply_email` and `forward_email` accept `dry_run=true`, and
  `verify_files=true` loads every file during the dry run.
- Send results report `accepted_by_mail_server` and
  `delivery_confirmed: false`, never delivery. With `OUTBOUND_TRACKING_HEADER`
  set, fresh outbound messages carry that header with a random ID, returned as
  `tracking_id`. It is off by default, and messages the provider composes
  server-side (Graph replies and forwards) always report `tracking_id: null`.
- Queue removal or an undo acknowledgment from SmarterMail does not prove that
  delivery was prevented and cannot recall delivered mail.
- `search_emails` runs one search at a time per process and retries EWS
  throttling (`ErrorServerBusy`, HTTP 429) with exponential backoff.

Details: [conventions and provider scope](docs/legacy/API.md#conventions),
[drafts](docs/legacy/API.md#drafts),
[draft recovery](docs/legacy/API.md#draft-recovery),
[attachment flows](docs/legacy/API.md#efficient-attachment-flows) and
[attachment sources](docs/legacy/API.md#attachment-sources).

### Defaults that differ from upstream 4.0

- `get_email_details` returns bodies as markdown by default. Pass
  `format="html"` for the previous output.
- `read_attachment` returns up to 8000 characters per call. Page with
  `offset` or raise `max_chars`. Base64 `download_attachment` and
  `get_email_mime` refuse payloads over 64 KiB unless `max_bytes` is raised.
- The rate limit is 120 tool calls per minute, and calls wait for a slot.
- Startup does not test the Exchange connection. It opens on the first tool
  call.
- With `EWS_SERVER_URL` set, the EWS version is pinned to Exchange 2016
  (15.1) instead of detected.

## Tests

Upstream has no `tests/` directory. The fork's suite runs inside the image:

```bash
docker build -t ews-mcp .
docker run --rm -v "$PWD:/app" -w /app --entrypoint sh ews-mcp \
  -c "pip install -q pytest pytest-asyncio && python -m pytest tests -q"
```

## Documentation

| | |
|---|---|
| [`docs/README.md`](docs/README.md) | Documentation map for both server lines |
| [`docs/legacy/API.md`](docs/legacy/API.md) | 4.0 tool reference, including provider scope, drafts, attachments and the SmarterMail scheduling tools |
| [`docs/legacy/SETUP.md`](docs/legacy/SETUP.md) | Exchange setup and authentication |
| [`docs/legacy/CONNECTION_GUIDE.md`](docs/legacy/CONNECTION_GUIDE.md) | Client connection recipes |
| [`docs/legacy/TROUBLESHOOTING.md`](docs/legacy/TROUBLESHOOTING.md) | Common failures, including provider and attachment issues |
| [`docs/legacy/REPLY_FORWARD.md`](docs/legacy/REPLY_FORWARD.md) | Reply and forward behaviour per provider |
| [`docs/legacy/AGENT_SECRETARY.md`](docs/legacy/AGENT_SECRETARY.md) | Memory, commitments, approvals, rules, briefings |
| [`docs/legacy/IMPERSONATION.md`](docs/legacy/IMPERSONATION.md) | Exchange impersonation and delegate access |
| [`v5/README.md`](v5/README.md) | Upstream's 4.5 server |
| [CHANGELOG.md](CHANGELOG.md) | Fork changes, then upstream history |

## Credits and license

The server is the work of the upstream
[azizmazrou/ews-mcp](https://github.com/azizmazrou/ews-mcp) project. This fork
builds on it. See [CONTRIBUTING.md](CONTRIBUTING.md) for upstream's
contribution guide. MIT, see [LICENSE](LICENSE).
