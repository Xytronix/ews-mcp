"""Supabase-backed store for multi-account mailbox credentials.

Lets operators manage many mailboxes in a Supabase table (edited in Studio's
Table Editor) instead of one large EWS_ACCOUNTS env blob. Rows are read once at
startup over Supabase's REST API (PostgREST), so no Postgres driver or exposed
5432 port is needed — just the public Supabase URL and a service-role key.

Minimum table (schema ``public``):

    create table ews_accounts (
        email    text primary key,
        password text not null
    );

Multi-provider mailboxes (iCloud over IMAP, Outlook over Microsoft Graph) add
two optional columns; a table without them keeps working and is read as
all-Exchange:

    alter table ews_accounts add column provider text default 'ews';
    alter table ews_accounts add column settings jsonb default '{}'::jsonb;

``provider`` is one of ``ews`` | ``icloud`` | ``imap`` | ``outlook`` | ``graph``
and ``settings`` carries the provider-specific connection details (imap_host,
client_id, refresh_token, ...). See src/mail_backends/accounts.py.

Keep RLS enabled with no public policy — the service-role key bypasses RLS, so
only this server (not the anon key) can read the plaintext passwords.

The loaders never raise: a missing/unreachable Supabase or an absent table
yields an empty result and logs a warning, so the primary mailbox (EWS_EMAIL/
EWS_PASSWORD) still starts.
"""
from __future__ import annotations

import logging

import httpx

_log = logging.getLogger(__name__)


def load_account_rows(
    supabase_url: str,
    service_key: str,
    table: str = "ews_accounts",
    timeout: float = 10.0,
) -> list[dict]:
    """Return every mailbox row from a Supabase table via the REST API.

    Selects ``*`` so the optional ``provider`` / ``settings`` columns come
    along when they exist, and a legacy ``email``/``password``-only table still
    reads cleanly. Returns ``[]`` on any error — multi-account is optional and
    must never block startup of the primary mailbox.
    """
    url = f"{supabase_url.rstrip('/')}/rest/v1/{table}"
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Accept": "application/json",
    }
    try:
        resp = httpx.get(
            url,
            params={"select": "*", "order": "email.asc"},
            headers=headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        rows = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        _log.warning("Could not read accounts from Supabase table %r: %s", table, e)
        return []

    if not isinstance(rows, list):
        _log.warning("Unexpected Supabase response for table %r: %r", table, rows)
        return []

    out: list[dict] = []
    for row in rows:
        if isinstance(row, dict) and (row.get("email") or "").strip():
            out.append(row)
    return out


def ews_passwords(rows: list[dict]) -> dict[str, str]:
    """``{email: password}`` for the Exchange rows only.

    Rows whose ``provider`` names another protocol (iCloud/IMAP, Outlook/Graph)
    are excluded so :meth:`src.ews_client.EWSClient.get_account` never tries to
    open them over EWS. A row with no ``provider`` column is Exchange.
    """
    accounts: dict[str, str] = {}
    for row in rows:
        provider = str(row.get("provider") or "ews").strip().lower()
        if provider not in ("", "ews", "exchange", "smartermail"):
            continue
        email = (row.get("email") or "").strip()
        password = row.get("password") or ""
        if email and password:
            accounts[email] = password
    return accounts


def load_accounts(
    supabase_url: str,
    service_key: str,
    table: str = "ews_accounts",
    timeout: float = 10.0,
) -> dict[str, str]:
    """Return ``{email: password}`` for the Exchange mailboxes in the table."""
    return ews_passwords(
        load_account_rows(supabase_url, service_key, table, timeout)
    )