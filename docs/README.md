# Documentation map

Two server lines live in this repository — make sure you read the docs
for the one you run.

## This fork's additions (4.0 line, `src/`)

This fork extends the legacy 4.0 server in `src/` with Graph, IMAP/SMTP and
iCloud CalDAV/CardDAV backends, multi-mailbox accounts, opt-in SmarterMail
REST tools, Liteparse attachment parsing, remote attachment sources and
staging, and native streamable HTTP. It publishes no container image, and
upstream's `:latest` image does not include these additions. The 4.5 line in
`v5/` is unchanged.

| Document | What it covers |
|---|---|
| [Root README, configuration](../README.md#configuration) | Build and run, every fork-added environment variable with its default, mailbox accounts, the tool surface and provider limits |
| [`legacy/API.md`](legacy/API.md#conventions) | Provider scope per mailbox, drafts and draft recovery, attachment sources and remote-agent flows, the SmarterMail scheduling tools |
| [`legacy/TROUBLESHOOTING.md`](legacy/TROUBLESHOOTING.md) | Provider, attachment, draft recovery, SMTP acceptance and scheduling problems |
| [`legacy/REPLY_FORWARD.md`](legacy/REPLY_FORWARD.md) | Reply and forward behaviour on Exchange, Graph and IMAP |
| [`legacy/AGENT_SECRETARY.md`](legacy/AGENT_SECRETARY.md) | Dry runs, remote attachments and reviewable drafts across providers |
| [`../CHANGELOG.md`](../CHANGELOG.md) | Every fork change |

## Current line — 4.5 (`v5/`, images `ghcr.io/…:v4.5*`)

| Document | What it covers |
|---|---|
| [`v5/README.md`](../v5/README.md) | Install & use: **stdio quick start (no Docker)** for Claude Code / Claude Desktop / any MCP client, HTTP transport, Docker, full configuration reference, the send flow, health endpoints |
| [`v5/docs/API.md`](../v5/docs/API.md) | **Full API reference, generated from the registry**: every tool with parameters, the canonical envelope, id aliases, error taxonomy, two-phase confirmation, cache freshness contract, v3→4.5 rename map, intentionally-dropped list |
| [`v5/DESIGN.md`](../v5/DESIGN.md) | Architecture and rationale: dispatcher gate chain, alias store, cache mirror, body cleaning, audit chain |
| [`v5/.env.example`](../v5/.env.example) | Annotated configuration template |
| [`examples/skills/exchange-assistant/`](../examples/skills/exchange-assistant/) | Example assistant skill composed on top of the tool surface |

## Legacy line — 4.0 (`src/`, ships as `:latest`)

Everything under [`legacy/`](legacy/) describes the legacy server only:

| Document | What it covers |
|---|---|
| [`legacy/SETUP.md`](legacy/SETUP.md) | Installation and first run |
| [`legacy/CONNECTION_GUIDE.md`](legacy/CONNECTION_GUIDE.md) | Claude Desktop / Open WebUI / SSE connection recipes |
| [`legacy/API.md`](legacy/API.md) | Legacy tool reference |
| [`legacy/ARCHITECTURE.md`](legacy/ARCHITECTURE.md) | Legacy internals |
| [`legacy/DEPLOYMENT.md`](legacy/DEPLOYMENT.md) | Docker / compose deployment |
| [`legacy/TROUBLESHOOTING.md`](legacy/TROUBLESHOOTING.md) | Common failures and fixes |
| [`legacy/AGENT_SECRETARY.md`](legacy/AGENT_SECRETARY.md) | The legacy agent-secretary feature stack |
| [`legacy/IMPERSONATION.md`](legacy/IMPERSONATION.md) | Multi-mailbox / delegate access |
| [`legacy/REPLY_FORWARD.md`](legacy/REPLY_FORWARD.md) | Reply/forward formatting details |
| [`legacy/COMMON_PITFALLS.md`](legacy/COMMON_PITFALLS.md) | Recurring regression patterns (developer notes) |

The 4.5 line intentionally does not implement some legacy features
(impersonation, OAuth2, folder management, MIME export, the
agent-secretary stack) — the full list and reasoning are in
[`v5/docs/API.md`](../v5/docs/API.md).
