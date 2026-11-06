# EWS MCP Server

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An MCP server that gives an LLM client typed access to mail, calendars and
contacts. An Exchange mailbox (EWS) is the primary account. More mailboxes can
be added over other protocols, and the same tools work on all of them.

- **Backends:** Exchange and SmarterMail over EWS, Outlook.com and
  Microsoft 365 over Microsoft Graph, iCloud, Gmail and any IMAP server over
  IMAP/SMTP, Fastmail and Stalwart over JMAP. CalDAV/CardDAV serves calendars
  and contacts for IMAP and JMAP accounts.
- **Multiple mailboxes:** `list_mailboxes` lists every account with its
  capabilities, and `target_mailbox` selects one per call.
- **Attachments:** `read_attachment` extracts text from PDF, Office,
  OpenDocument and image files (with OCR), archives, calendar and contact
  files, MSG and EML. Remote clients send files as base64, a URL or a staged
  upload.
- **SmarterMail REST tools (optional):** scheduled send, signatures, notes,
  recurring events, file storage and domain administration.
- **Transports:** stdio, SSE and streamable HTTP at `/mcp`.

## Quick start

### Docker

```bash
git clone https://github.com/Xytronix/ews-mcp.git
cd ews-mcp
cp .env.example .env        # fill in credentials and MCP_API_KEY
docker build -t ews-mcp .
docker run -d --name ews-mcp --env-file .env -p 8000:8000 ews-mcp
curl http://localhost:8000/health
```

The image serves streamable HTTP on `0.0.0.0:8000`. `/health` needs no key.
Every other request needs `MCP_API_KEY`, sent as `Authorization: Bearer <key>`,
`X-API-Key: <key>` or `?api_key=<key>`. The server refuses to start on a
non-loopback address without a key unless `MCP_ALLOW_UNAUTHENTICATED=true`,
which is only safe on a private network with no published port.

```bash
claude mcp add --transport http exchange http://localhost:8000/mcp \
  --header "Authorization: Bearer <MCP_API_KEY>"
```

`docker compose up -d` builds and runs the same image with host networking.
The image includes LibreOffice, Tesseract, ImageMagick, libheif, unar and
openssl for attachment parsing.

### From source (stdio)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m src.main
```

Python 3.11 or newer is required. Start the server from the repository root,
because it reads `.env` and its `data/` and `downloads/` directories from the
working directory:

```bash
claude mcp add exchange -- sh -c 'cd /path/to/ews-mcp && exec .venv/bin/python -m src.main'
```

## Configuration

[`.env.example`](.env.example) lists every setting with comments, including
the feature toggles, cache and AI options not repeated here.

The account variables (`MAIL_ACCOUNTS`, `ICLOUD_*`, `GMAIL_*`, `OUTLOOK_*`,
`GRAPH_*`) and the variables marked *env* below are read from the process
environment only. A `.env` file does not set them when running from source,
so export them or pass them in the client's `env` block. Docker
`--env-file` sets them.

### Primary Exchange mailbox

| Variable | Default | Meaning |
|---|---|---|
| `EWS_EMAIL` | required | Primary mailbox address |
| `EWS_SERVER_URL` | unset | EWS endpoint as a URL or host name |
| `EWS_AUTODISCOVER` | `true` | Use autodiscover when `EWS_SERVER_URL` is unset |
| `EWS_AUTH_TYPE` | `oauth2` | `oauth2`, `basic` or `ntlm` |
| `EWS_CLIENT_ID`, `EWS_CLIENT_SECRET`, `EWS_TENANT_ID` | unset | Required for `oauth2` |
| `EWS_USERNAME`, `EWS_PASSWORD` | unset | Required for `basic` and `ntlm`. The password can also come from the accounts table |
| `EWS_VERSION` | `auto` | EWS schema version with `EWS_SERVER_URL`. `auto` detects it once per endpoint. Pin it with a product name (`2016`, `2019`, `2013_SP1`, `O365`) or a build number (`15.1.1779.4`) |
| `TIMEZONE` | `UTC` | IANA time zone for dates and calendar events |

For `oauth2` on Exchange Online, register an app in Microsoft Entra ID, add
the application permission `full_access_as_app` of Office 365 Exchange
Online, grant admin consent and create a client secret. Set
`EWS_SERVER_URL=https://outlook.office365.com/EWS/Exchange.asmx`. Access to
other Exchange mailboxes without their own password needs impersonation or
delegate rights, see [`docs/IMPERSONATION.md`](docs/IMPERSONATION.md).

### Server

| Variable | Default | Meaning |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `stdio`, `sse` or `streamable-http`. The image sets `streamable-http` |
| `MCP_HOST`, `MCP_PORT` | `127.0.0.1`, `8000` | HTTP bind. The image sets `0.0.0.0` |
| `MCP_API_KEY` | unset | Key for HTTP clients. Required on a non-loopback bind |
| `MCP_ALLOW_UNAUTHENTICATED` | `false` | Allow a non-loopback bind without a key |
| `RATE_LIMIT_REQUESTS_PER_MINUTE` | `120` | Tool calls per minute. Calls wait for a slot |
| `RATE_LIMIT_MAX_WAIT_SECONDS` | `60` | *env.* Longest wait for a slot before a call fails |
| `STAGE_UPLOAD_TOKEN` | unset | *env.* Enables `POST /stage` for raw uploads up to 25 MiB |
| `EWS_MCP_STAGING_DIR` | `/tmp/ews-mcp-staged` | *env.* Directory for staged files |
| `OUTBOUND_TRACKING_HEADER` | unset | *env.* Header name for a random tracking ID on new outbound mail, returned as `tracking_id`. Recipients can see it |
| `EWS_SEARCH_ALL_MAX_FOLDERS` | `50` | *env.* Most folders `search_scope=["all"]` searches on Exchange |
| `EWS_THROTTLE_MAX_RETRIES`, `EWS_THROTTLE_BASE_DELAY`, `EWS_THROTTLE_MAX_DELAY` | `4`, `3`, `30` | *env.* Backoff for EWS throttling in `search_emails` (seconds) |

### Mailbox accounts

Accounts come from four sources. A later source wins on the same address:
the primary `EWS_EMAIL`, the Supabase table, `MAIL_ACCOUNTS`, then the
shortcut variables. An account with missing credentials is skipped with a
warning.

`MAIL_ACCOUNTS` holds a JSON array:

```bash
export MAIL_ACCOUNTS='[
 {"email":"me@icloud.com","provider":"icloud","password":"app-specific-password"},
 {"email":"me@gmail.com","provider":"gmail","client_id":"...","client_secret":"...","refresh_token":"..."},
 {"email":"me@example.com","provider":"imap","password":"...","imap_host":"imap.example.com","smtp_host":"smtp.example.com"},
 {"email":"me@outlook.com","provider":"outlook","client_id":"...","refresh_token":"..."},
 {"email":"me@fastmail.com","provider":"fastmail","api_token":"..."}
]'
```

In a Docker env file write `MAIL_ACCOUNTS=[...]` on one line without
surrounding quotes.

| `provider` | Protocol | Needs |
|---|---|---|
| `icloud`, `apple` | IMAP/SMTP, CalDAV/CardDAV | App-specific `password`. Hosts and DAV roots are preset |
| `gmail`, `google` | IMAP/SMTP | App `password`, or `client_id`, `client_secret` and `refresh_token` for OAuth |
| `imap` | IMAP/SMTP | `password`, `imap_host` and `smtp_host` |
| `outlook`, `graph`, `office365`, `o365`, `m365`, `microsoft365` | Microsoft Graph | `client_id` plus `refresh_token` (delegated) or `client_secret` (app-only) |
| `jmap`, `stalwart` | JMAP | `session_url` plus `api_token` (Bearer) or `password` (Basic) |
| `fastmail` | JMAP | `api_token`. The session URL is preset |
| `ews`, `exchange`, `smartermail` | EWS on the primary server | `password`, from the Supabase table only |

Optional keys, at the top level or in a `settings` object: `label`,
`imap_port` (993), `imap_ssl` (true), `imap_username`, `smtp_port` (587),
`smtp_starttls` (true), `smtp_ssl` (false), `smtp_username`, `sent_folder`,
`caldav_url`, `carddav_url`, `oauth_token_url`, `tenant_id`, `graph_scope`,
`graph_base_url`, `graph_token_url`, `shared_mailbox` and `jmap_username`.
Any IMAP account with `client_id` and `refresh_token` signs in with XOAUTH2
(set `oauth_token_url` outside Google). Fastmail DAV needs an app password as
`password`, not the API token.

Shortcut variables for one mailbox each:

| Variables | Adds |
|---|---|
| `ICLOUD_EMAIL`, `ICLOUD_APP_PASSWORD`, `ICLOUD_LABEL`, `ICLOUD_CALDAV_URL`, `ICLOUD_CARDDAV_URL` | iCloud mail, calendar and contacts |
| `GMAIL_EMAIL`, `GMAIL_APP_PASSWORD` or `GMAIL_OAUTH_CLIENT_ID`, `GMAIL_OAUTH_CLIENT_SECRET`, `GMAIL_OAUTH_REFRESH_TOKEN`, `GMAIL_OAUTH_TOKEN_URL`, `GMAIL_LABEL` | Gmail. OAuth wins when both are set |
| `OUTLOOK_EMAIL`, `OUTLOOK_CLIENT_ID`, `OUTLOOK_REFRESH_TOKEN` or `OUTLOOK_CLIENT_SECRET`, `OUTLOOK_TENANT_ID`, `OUTLOOK_GRAPH_SCOPE`, `OUTLOOK_LABEL`, `GRAPH_BASE_URL`, `GRAPH_TOKEN_URL` | Outlook.com or Microsoft 365 over Graph |

The Supabase table (`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`,
`EWS_ACCOUNTS_TABLE`, default `ews_accounts`) is read once at startup. It
needs `email` and `password` columns. Optional `provider` and `settings`
(jsonb) columns take the same keys as `MAIL_ACCOUNTS`. Keep row level
security on with no public policy.

### Gmail OAuth sign-in

With a refresh token, Gmail signs in to IMAP and SMTP with XOAUTH2 and the
server refreshes the access token itself. You need a Google Cloud OAuth
client (Desktop app, or Web app with a registered redirect URI) that may
request the `https://mail.google.com/` scope. Get the refresh token once:

```bash
python -m src.mail_backends.google_oauth --email me@gmail.com \
  --client-id ID.apps.googleusercontent.com --client-secret SECRET --no-listen
```

Open the printed URL, allow access, then paste the address the browser was
sent to (an error page there is fine). The command prints the
`GMAIL_OAUTH_*` lines and a `MAIL_ACCOUNTS` entry. Without `--no-listen` a
local listener catches the redirect, which only works when the browser runs
on the same machine. A Web-app client needs `--redirect-uri` with its
registered URI. In Docker, run it with `docker exec -it <container>`.

An app in Google's Testing status issues refresh tokens that expire after 7
days. Revoked or expired tokens fail with `invalid_grant`, and the error
names the command to run again.

### SmarterMail REST tools

| Variable | Default | Meaning |
|---|---|---|
| `SMARTERMAIL_ENABLED` | `false` | Register 31 SmarterMail tools |
| `SMARTERMAIL_ADMIN_TOOLS` | `false` | Also register 8 domain-administration tools |
| `SMARTERMAIL_BASE_URL` | host of `EWS_SERVER_URL` | SmarterMail web address |
| `SMARTERMAIL_TIMEZONE` | `TIMEZONE` | Zone for recurring events that name none |

The tools log in with each mailbox's own password and work only against a
SmarterMail server. The admin tools need a domain administrator and can
create, rename, disable and delete mailboxes.

## Tools

The server registers 70 tools by default, 101 with `SMARTERMAIL_ENABLED` and
109 with `SMARTERMAIL_ADMIN_TOOLS`. `ENABLE_EMAIL`, `ENABLE_CALENDAR`,
`ENABLE_CONTACTS`, `ENABLE_TASKS` and `ENABLE_AGENT` set to false remove
their tools. The runtime `tools/list` is authoritative.

| Category | Tools |
|---|---|
| Email | `send_email` · `read_emails` · `search_emails` · `get_email_details` · `get_emails_bulk` · `delete_email` · `move_email` · `copy_email` · `update_email` · `reply_email` · `forward_email` · `create_draft` · `create_reply_draft` · `create_forward_draft` · `update_draft` |
| Mailboxes | `list_mailboxes` |
| Attachments | `list_attachments` · `download_attachment` · `add_attachment` · `delete_attachment` · `read_attachment` · `get_email_mime` · `attach_email_to_draft` · `stage_outgoing_file` · `delete_staged_file` |
| Calendar | `create_appointment` · `get_calendar` · `update_appointment` · `delete_appointment` · `respond_to_meeting` · `check_availability` · `find_meeting_times` |
| Contacts | `create_contact` · `update_contact` · `delete_contact` · `find_person` · `analyze_contacts` |
| Tasks | `create_task` · `get_tasks` · `update_task` · `complete_task` · `delete_task` |
| Folders and search | `list_folders` · `find_folder` · `manage_folder` · `search_by_conversation` |
| Out of office | `oof_settings` · `configure_oof_policy` · `get_oof_policy` · `apply_oof_policy` |
| Agent | `memory_set` · `memory_get` · `memory_list` · `memory_delete` · `track_commitment` · `list_commitments` · `resolve_commitment` · `submit_for_approval` · `list_pending_approvals` · `approve` · `reject` · `execute_approved_action` · `get_voice_profile` · `rule_create` · `rule_list` · `rule_delete` · `rule_simulate` · `evaluate_rules_on_message` · `generate_briefing` · `prepare_meeting` |
| AI | `semantic_search_emails`, with `ENABLE_AI=true` and `ENABLE_SEMANTIC_SEARCH=true` |
| SmarterMail | `schedule_email` · `list_scheduled_emails` · `cancel_scheduled_email` · `undo_send` · `bulk_update_emails` · `export_emails` · `unsubscribe_email` · `manage_mail_classification` · `manage_signatures` · `manage_senders` · `manage_auto_clean` · `manage_notes` · `manage_disposable_address` · `manage_notifications` · `manage_auto_complete` · `manage_account_security` · `manage_recurring_event` · `update_event_occurrence` · `manage_calendars` · `manage_internet_calendars` · `import_export_calendar` · `manage_address_books` · `manage_contacts_bulk` · `manage_virtual_folders` · `manage_folder_sharing` · `manage_file_storage` · `attach_file_link` · `manage_rss_feeds` · `search_chat_history` · `mailbox_usage_report` · `manage_connected_services` |
| SmarterMail admin | `manage_domain_users` · `manage_user_access` · `manage_domain_aliases` · `manage_mailing_lists` · `manage_user_groups` · `manage_domain_mail_policy` · `manage_event_hooks` · `manage_mailbox_migration` |

- Attach files with `files`. Each entry is a server `file_path`, base64
  `file_content` with `file_name`, a `file_url` the server fetches (public
  HTTP(S) only, 25 MiB), a SmarterMail `storage_file_id`, or a path from
  `stage_outgoing_file` (expires after one hour by default).
- `send_email`, `reply_email` and `forward_email` accept `dry_run=true`.
  Send results report `accepted_by_mail_server`, never delivery.
- `get_email_details` returns markdown bodies (`format="html"` for HTML).
  `read_attachment` returns 8000 characters per call (page with `offset`).
  Base64 downloads over 64 KiB need a higher `max_bytes`.

## Provider support

| Capability | Exchange | Graph | IMAP/SMTP | JMAP |
|---|---|---|---|---|
| Folders, read, search, move, delete | Yes | Yes | Yes. IDs are per folder | Yes. IDs survive moves |
| Send, reply, forward, drafts | Yes | Yes. New files up to 3 MiB | Yes. Draft edits change IDs | Yes. Draft edits change IDs |
| Attachment changes | Yes | Drafts only | Drafts only | Drafts only |
| Search by recipient, body or read mail | Yes | No | No | Yes |
| Search all folders | Yes | Yes | No | Yes |
| Flag states | Flagged, NotFlagged, Complete | Flagged, NotFlagged, Complete | Flagged, NotFlagged | Flagged, NotFlagged |
| Calendar | Yes, with recurring series | Yes, with calendar consent | Over CalDAV, no new series | Over CalDAV, no new series |
| Free/busy and meeting times | Yes | Organization accounts | No | No |
| Contacts | Yes, with directory | Yes, with contact consent | Over CardDAV | Over CardDAV |
| Tasks and contact analytics | Yes | No | No | No |
| Scheduled send | SmarterMail only | No | No | No |

Keep the folder with each IMAP message ID, and use the new IDs a draft edit
returns. See [`docs/API.md`](docs/API.md) for conventions, drafts and
attachment flows.

## Tests

```bash
docker build -t ews-mcp .
docker run --rm -v "$PWD:/app" -w /app --entrypoint sh ews-mcp \
  -c "pip install -q pytest pytest-asyncio && python -m pytest tests -q"
```

## Documentation

- [`docs/API.md`](docs/API.md): tool reference and conventions
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md): common failures
- [`docs/IMPERSONATION.md`](docs/IMPERSONATION.md): Exchange impersonation and delegate access
- [`CHANGELOG.md`](CHANGELOG.md): changes

`v5/` contains a separate server with its own [README](v5/README.md).
Nothing above applies to it.

## License

MIT, see [LICENSE](LICENSE).
