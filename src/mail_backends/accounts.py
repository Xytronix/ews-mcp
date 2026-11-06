"""Mailbox account descriptors: parsing, provider presets, validation.

An "account" is one mailbox plus the credentials needed to reach it. Accounts
come from three places, merged in this order (later wins on duplicate email):

1. the Supabase ``ews_accounts`` table (optional ``provider`` / ``settings``
   columns — a table with only ``email``/``password`` keeps working and is
   treated as all-EWS),
2. the ``MAIL_ACCOUNTS`` env var (a JSON array),
3. the single-mailbox convenience env vars (``ICLOUD_EMAIL`` /
   ``ICLOUD_APP_PASSWORD``, ``OUTLOOK_EMAIL`` / ``OUTLOOK_CLIENT_ID`` / ...).

Nothing here touches the network, so it is fully unit-testable.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional

_log = logging.getLogger(__name__)

# provider spelling -> backend kind
_PROVIDER_KINDS: Dict[str, str] = {
    "ews": "ews",
    "exchange": "ews",
    "smartermail": "ews",
    "imap": "imap",
    "icloud": "imap",
    "gmail": "imap",
    "google": "imap",
    "apple": "imap",
    "graph": "graph",
    "outlook": "graph",
    "office365": "graph",
    "o365": "graph",
    "m365": "graph",
    "microsoft365": "graph",
}

# Connection defaults per provider spelling. Only IMAP needs host/port; Graph
# talks to one global endpoint.
_GMAIL_PRESET: Dict[str, Any] = {
    "imap_host": "imap.gmail.com",
    "imap_port": 993,
    "imap_ssl": True,
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_starttls": True,
}

_PRESETS: Dict[str, Dict[str, Any]] = {
    "gmail": _GMAIL_PRESET,
    "google": _GMAIL_PRESET,
    "icloud": {
        "imap_host": "imap.mail.me.com",
        "imap_port": 993,
        "imap_ssl": True,
        "smtp_host": "smtp.mail.me.com",
        "smtp_port": 587,
        "smtp_starttls": True,
        "caldav_url": "https://caldav.icloud.com/",
        "carddav_url": "https://contacts.icloud.com/",
    },
    "apple": {
        "imap_host": "imap.mail.me.com",
        "imap_port": 993,
        "imap_ssl": True,
        "smtp_host": "smtp.mail.me.com",
        "smtp_port": 587,
        "smtp_starttls": True,
        "caldav_url": "https://caldav.icloud.com/",
        "carddav_url": "https://contacts.icloud.com/",
    },
}

# Keys copied out of a flat account dict into ``settings``.
_SETTING_KEYS = (
    "imap_host",
    "imap_port",
    "imap_ssl",
    "imap_username",
    "smtp_host",
    "smtp_port",
    "smtp_starttls",
    "smtp_ssl",
    "caldav_url",
    "carddav_url",
    "smtp_username",
    "sent_folder",
    "client_id",
    "client_secret",
    "tenant_id",
    "refresh_token",
    "graph_base_url",
    "graph_token_url",
    "graph_scope",
    "shared_mailbox",
)

_SECRET_KEYS = frozenset({"client_secret", "refresh_token"})


class AccountConfigError(ValueError):
    """An account entry is missing credentials or is otherwise unusable."""


@dataclass
class MailAccount:
    """One mailbox and how to reach it."""

    email: str
    provider: str = "ews"
    password: Optional[str] = None
    label: Optional[str] = None
    settings: Dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        """Backend kind: ``ews`` | ``imap`` | ``graph``."""
        return _PROVIDER_KINDS.get(self.provider, "ews")

    @property
    def key(self) -> str:
        return self.email.lower()

    def get(self, name: str, default: Any = None) -> Any:
        return self.settings.get(name, default)

    def describe(self) -> Dict[str, Any]:
        """Secret-free summary for tool responses and logs."""
        out: Dict[str, Any] = {
            "email": self.email,
            "provider": self.provider,
            "backend": self.kind,
        }
        if self.label:
            out["label"] = self.label
        if self.kind == "imap":
            out["imap"] = f"{self.get('imap_host')}:{self.get('imap_port')}"
            out["smtp"] = f"{self.get('smtp_host')}:{self.get('smtp_port')}"
            if self.get("caldav_url"):
                out["caldav"] = self.get("caldav_url")
            if self.get("carddav_url"):
                out["carddav"] = self.get("carddav_url")
        elif self.kind == "graph":
            out["tenant"] = self.get("tenant_id") or "common"
            out["auth"] = (
                "delegated_refresh_token"
                if self.get("refresh_token")
                else "client_credentials"
            )
        return out

    def validate(self) -> None:
        """Raise :class:`AccountConfigError` when credentials can't work."""
        if not self.email or "@" not in self.email:
            raise AccountConfigError(f"invalid mailbox address: {self.email!r}")
        if self.kind == "imap":
            if not self.password:
                raise AccountConfigError(
                    f"{self.email}: IMAP mailbox needs a password "
                    f"(iCloud requires an app-specific password)"
                )
            if not self.get("imap_host"):
                raise AccountConfigError(
                    f"{self.email}: IMAP mailbox needs imap_host "
                    f"(or provider=icloud for the Apple defaults)"
                )
            if not self.get("smtp_host"):
                raise AccountConfigError(f"{self.email}: IMAP mailbox needs smtp_host")
        elif self.kind == "graph":
            if not self.get("client_id"):
                raise AccountConfigError(f"{self.email}: Graph mailbox needs client_id")
            if not self.get("refresh_token") and not self.get("client_secret"):
                raise AccountConfigError(
                    f"{self.email}: Graph mailbox needs either refresh_token "
                    f"(delegated) or client_secret (app-only)"
                )
        elif self.kind == "ews" and not self.password:
            raise AccountConfigError(f"{self.email}: EWS mailbox needs a password")


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _coerce_int(value: Any, default: Optional[int]) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_account(row: Mapping[str, Any]) -> MailAccount:
    """Build a :class:`MailAccount` from a flat dict (Supabase row or JSON).

    Recognised keys: ``email``, ``password``, ``provider``, ``label``, an
    optional nested ``settings`` object, plus any of :data:`_SETTING_KEYS` at
    the top level. Unknown keys are ignored so extra Supabase columns
    (``created_at``, notes, ...) are harmless.
    """
    email = str(row.get("email") or row.get("mailbox") or "").strip()
    provider = str(row.get("provider") or row.get("kind") or "ews").strip().lower()
    if provider not in _PROVIDER_KINDS:
        _log.warning(
            "Unknown mail provider %r for %s — treating it as EWS", provider, email
        )
        provider = "ews"

    settings: Dict[str, Any] = dict(_PRESETS.get(provider, {}))

    nested = row.get("settings") or row.get("config") or {}
    if isinstance(nested, str):
        try:
            nested = json.loads(nested) if nested.strip() else {}
        except ValueError:
            _log.warning("Ignoring unparseable settings JSON for %s", email)
            nested = {}
    if isinstance(nested, Mapping):
        for key, value in nested.items():
            if value not in (None, ""):
                settings[key] = value

    for key in _SETTING_KEYS:
        value = row.get(key)
        if value not in (None, ""):
            settings[key] = value

    kind = _PROVIDER_KINDS[provider]
    if kind == "imap":
        settings["imap_port"] = _coerce_int(settings.get("imap_port"), 993)
        settings["imap_ssl"] = _coerce_bool(settings.get("imap_ssl"), True)
        settings["smtp_port"] = _coerce_int(settings.get("smtp_port"), 587)
        settings["smtp_starttls"] = _coerce_bool(settings.get("smtp_starttls"), True)
        settings["smtp_ssl"] = _coerce_bool(settings.get("smtp_ssl"), False)

    password = row.get("password") or row.get("app_password") or None
    return MailAccount(
        email=email,
        provider=provider,
        password=str(password) if password else None,
        label=(str(row["label"]).strip() if row.get("label") else None),
        settings=settings,
    )


def accounts_from_json(blob: Optional[str], source: str = "MAIL_ACCOUNTS") -> List[MailAccount]:
    """Parse a JSON array (or single object) of account dicts. Never raises."""
    if not blob or not blob.strip():
        return []
    try:
        parsed = json.loads(blob)
    except ValueError as exc:
        _log.error("%s is not valid JSON: %s — ignoring it", source, exc)
        return []
    if isinstance(parsed, Mapping):
        parsed = [parsed]
    if not isinstance(parsed, list):
        _log.error("%s must be a JSON array of account objects — ignoring it", source)
        return []
    out: List[MailAccount] = []
    for entry in parsed:
        if not isinstance(entry, Mapping):
            _log.warning("%s: skipping non-object entry", source)
            continue
        out.append(build_account(entry))
    return out


def accounts_from_env(environ: Optional[Mapping[str, str]] = None) -> List[MailAccount]:
    """Build accounts from ``MAIL_ACCOUNTS`` plus the convenience env vars."""
    env = os.environ if environ is None else environ
    accounts = accounts_from_json(env.get("MAIL_ACCOUNTS"))

    icloud_email = (env.get("ICLOUD_EMAIL") or "").strip()
    icloud_password = (
        env.get("ICLOUD_APP_PASSWORD") or env.get("ICLOUD_PASSWORD") or ""
    ).strip()
    if icloud_email:
        accounts.append(
            build_account(
                {
                    "email": icloud_email,
                    "provider": "icloud",
                    "password": icloud_password,
                    "label": env.get("ICLOUD_LABEL"),
                    "caldav_url": env.get("ICLOUD_CALDAV_URL"),
                    "carddav_url": env.get("ICLOUD_CARDDAV_URL"),
                }
            )
        )

    # Gmail over IMAP/SMTP: needs a Google *App Password* (16 chars, account
    # must have 2-Step Verification). The normal account password is refused.
    gmail_email = (env.get("GMAIL_EMAIL") or "").strip()
    gmail_password = (env.get("GMAIL_APP_PASSWORD") or "").replace(" ", "").strip()
    if gmail_email and gmail_password:
        accounts.append(
            build_account(
                {
                    "email": gmail_email,
                    "provider": "gmail",
                    "password": gmail_password,
                    "label": env.get("GMAIL_LABEL") or "Gmail",
                }
            )
        )
    elif gmail_email:
        _log.warning(
            "GMAIL_EMAIL=%s set without GMAIL_APP_PASSWORD; mailbox skipped",
            gmail_email,
        )

    outlook_email = (env.get("OUTLOOK_EMAIL") or "").strip()
    if outlook_email:
        accounts.append(
            build_account(
                {
                    "email": outlook_email,
                    "provider": "outlook",
                    "label": env.get("OUTLOOK_LABEL"),
                    "client_id": env.get("OUTLOOK_CLIENT_ID"),
                    "client_secret": env.get("OUTLOOK_CLIENT_SECRET"),
                    "tenant_id": env.get("OUTLOOK_TENANT_ID"),
                    "refresh_token": env.get("OUTLOOK_REFRESH_TOKEN"),
                    "graph_scope": env.get("OUTLOOK_GRAPH_SCOPE"),
                    "graph_base_url": env.get("GRAPH_BASE_URL"),
                    "graph_token_url": env.get("GRAPH_TOKEN_URL"),
                }
            )
        )
    return accounts


def merge_accounts(*groups: Iterable[MailAccount]) -> Dict[str, MailAccount]:
    """Merge account groups into ``{lowercased email: account}``; later wins."""
    merged: Dict[str, MailAccount] = {}
    for group in groups:
        for account in group:
            if not account.email:
                continue
            merged[account.key] = account
    return merged


def redact(settings: Mapping[str, Any]) -> Dict[str, Any]:
    """Copy of ``settings`` with secrets masked — for logging only."""
    return {
        k: ("<set>" if k in _SECRET_KEYS and v else v) for k, v in settings.items()
    }
