"""Mailbox registry: resolve an e-mail address to the backend that serves it.

The registry is the single place that knows *which protocol* a mailbox speaks.
It is built once per server process from :class:`~src.config.Settings` and
hands out cached, lazily-connected :class:`~src.mail_backends.base.MailBackend`
instances.

The primary Exchange mailbox (``EWS_EMAIL``) and every row of the Supabase
``ews_accounts`` table that has no ``provider`` column keep their existing
EWS behaviour — the registry simply wraps them so one set of tools can address
Exchange, iCloud and Outlook mailboxes uniformly.
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .accounts import (
    AccountConfigError,
    MailAccount,
    accounts_from_env,
    build_account,
    merge_accounts,
)
from .base import MailBackend, MailBackendError

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings
    from ..ews_client import EWSClient

_log = logging.getLogger(__name__)


class MailboxRegistry:
    """Accounts + their backends, keyed by lowercased e-mail address."""

    def __init__(self, config: "Settings", ews_client: "EWSClient") -> None:
        self.config = config
        self.ews_client = ews_client
        self._lock = threading.RLock()
        self._backends: Dict[str, MailBackend] = {}
        self._accounts: Dict[str, MailAccount] = self._load_accounts()
        self._log_inventory()

    # ------------------------------------------------------------------
    # loading
    # ------------------------------------------------------------------
    def _load_accounts(self) -> Dict[str, MailAccount]:
        primary_email = (getattr(self.config, "ews_email", "") or "").strip()
        primary: List[MailAccount] = []
        if primary_email:
            primary.append(
                build_account(
                    {
                        "email": primary_email,
                        "provider": "ews",
                        "password": self.config.get_primary_password(),
                        "label": "primary",
                    }
                )
            )

        # Rows from the Supabase table. ``mail_account_rows`` is populated by
        # Settings; a table without provider/settings columns yields plain EWS
        # rows, exactly as before.
        table: List[MailAccount] = []
        for row in getattr(self.config, "mail_account_rows", None) or []:
            table.append(build_account(row))

        env_accounts = accounts_from_env()

        merged = merge_accounts(primary, table, env_accounts)

        usable: Dict[str, MailAccount] = {}
        for key, account in merged.items():
            try:
                account.validate()
            except AccountConfigError as exc:
                _log.warning("Ignoring mailbox %s: %s", account.email, exc)
                continue
            usable[key] = account
        return usable

    def _log_inventory(self) -> None:
        by_kind: Dict[str, int] = {}
        for account in self._accounts.values():
            by_kind[account.kind] = by_kind.get(account.kind, 0) + 1
        _log.info(
            "Mailbox registry: %d account(s) %s",
            len(self._accounts),
            by_kind or "{}",
        )

    def reload(self) -> None:
        """Re-read account config and drop every cached backend."""
        with self._lock:
            self.close()
            self._accounts = self._load_accounts()
            self._log_inventory()

    # ------------------------------------------------------------------
    # lookup
    # ------------------------------------------------------------------
    @property
    def default_email(self) -> Optional[str]:
        primary = (getattr(self.config, "ews_email", "") or "").strip().lower()
        if primary and primary in self._accounts:
            return self._accounts[primary].email
        for account in self._accounts.values():
            return account.email
        return None

    def accounts(self) -> List[MailAccount]:
        return list(self._accounts.values())

    def describe_accounts(self) -> List[Dict[str, Any]]:
        default = (self.default_email or "").lower()
        out: List[Dict[str, Any]] = []
        for account in self._accounts.values():
            info = account.describe()
            if account.key == default:
                info["default"] = True
            out.append(info)
        return out

    def find_account(self, email: Optional[str] = None) -> Optional[MailAccount]:
        """Non-raising lookup used by the routing layer in ``src/tools``.

        Returns ``None`` when the address is not a configured mailbox — the
        caller then treats it as an EWS impersonation / delegate target, which
        is what ``target_mailbox`` has always meant.
        """
        target = (email or self.default_email or "").strip().lower()
        if not target:
            return None
        return self._accounts.get(target)

    def get_account(self, email: Optional[str] = None) -> MailAccount:
        target = (email or self.default_email or "").strip()
        if not target:
            raise MailBackendError(
                "no mailboxes configured; set EWS_EMAIL, MAIL_ACCOUNTS, "
                "ICLOUD_EMAIL or OUTLOOK_EMAIL"
            )
        account = self._accounts.get(target.lower())
        if account is None:
            known = ", ".join(sorted(a.email for a in self._accounts.values())) or "<none>"
            raise MailBackendError(
                f"mailbox {target!r} is not configured. Configured mailboxes: {known}"
            )
        return account

    def get_backend(self, email: Optional[str] = None) -> MailBackend:
        """Return the cached backend for ``email`` (built on first use)."""
        account = self.get_account(email)
        with self._lock:
            backend = self._backends.get(account.key)
            if backend is None:
                backend = self._build_backend(account)
                self._backends[account.key] = backend
            return backend

    def _build_backend(self, account: MailAccount) -> MailBackend:
        kind = account.kind
        if kind == "imap":
            from .imap_backend import IMAPBackend

            return IMAPBackend(account)
        if kind == "graph":
            from .graph_backend import GraphBackend

            return GraphBackend(account)
        if kind == "ews":
            from .ews_backend import EWSBackend

            return EWSBackend(account, self.ews_client)
        raise MailBackendError(
            f"{account.email}: unsupported backend kind {kind!r}"
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            for key, backend in self._backends.items():
                try:
                    backend.close()
                except Exception as exc:  # pragma: no cover - best effort
                    _log.debug("closing backend for %s failed: %s", key, exc)
            self._backends.clear()
