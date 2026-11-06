"""Authenticated HTTP client for SmarterMail's private JSON API."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

_log = logging.getLogger(__name__)

# SmarterMail parks scheduled messages in this hidden folder until their send
# time, then moves them to Sent Items. The suffix is a server constant, not a
# per-account id.
SCHEDULED_FOLDER = "Scheduled8c61c74014d4e679d52c0a6aca4342b"

# The access token is short-lived; refresh a little before it expires.
_TOKEN_SKEW_SECONDS = 60


class SmarterMailError(Exception):
    """A SmarterMail API call failed."""


class SmarterMailClient:
    """One authenticated session against SmarterMail for one mailbox.

    Tokens are fetched lazily, refreshed with the refresh token, and re-minted
    from the password if the refresh is rejected. Every response is checked for
    the API's own ``success``/``message`` envelope, because SmarterMail answers
    HTTP 200 with ``success: false`` for application-level failures.
    """

    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
        *,
        timeout: float = 30.0,
        verify: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.email = email
        self._password = password
        self._timeout = timeout
        self._client = httpx.Client(
            base_url=self.base_url, timeout=timeout, verify=verify
        )
        self._lock = threading.Lock()
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._expires_at: float = 0.0
        self.is_domain_admin = False
        self.is_admin = False

    # --- lifecycle ----------------------------------------------------
    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # pragma: no cover - shutdown best effort
            pass

    # --- auth ---------------------------------------------------------
    def _authenticate(self) -> None:
        payload = {"username": self.email, "password": self._password}
        try:
            resp = self._client.post("/api/v1/auth/authenticate-user", json=payload)
        except httpx.HTTPError as exc:
            raise SmarterMailError(f"SmarterMail login failed: {exc}") from exc
        data = self._payload(resp, "authenticate-user")
        token = data.get("accessToken")
        if not token:
            raise SmarterMailError(
                f"SmarterMail login for {self.email} returned no access token"
            )
        self._access_token = token
        self._refresh_token = data.get("refreshToken")
        self._expires_at = self._expiry(data.get("accessTokenExpiration"))
        self.is_domain_admin = bool(data.get("isDomainAdmin"))
        self.is_admin = bool(data.get("isAdmin"))

    def _refresh(self) -> None:
        if not self._refresh_token:
            self._authenticate()
            return
        try:
            resp = self._client.post(
                "/api/v1/auth/refresh-token", json={"token": self._refresh_token}
            )
        except httpx.HTTPError:
            self._authenticate()
            return
        if resp.status_code != 200:
            self._authenticate()
            return
        try:
            data = resp.json()
        except Exception:
            self._authenticate()
            return
        token = data.get("accessToken")
        if not token:
            self._authenticate()
            return
        self._access_token = token
        self._refresh_token = data.get("refreshToken") or self._refresh_token
        self._expires_at = self._expiry(data.get("accessTokenExpiration"))

    def _token(self) -> str:
        with self._lock:
            if not self._access_token:
                self._authenticate()
            elif time.time() >= self._expires_at - _TOKEN_SKEW_SECONDS:
                self._refresh()
            return self._access_token or ""

    @staticmethod
    def _expiry(value: Any) -> float:
        """Absolute expiry for the token.

        SmarterMail returns an ISO-8601 UTC timestamp
        (``2026-09-02T22:36:30.4562514Z``, sub-second precision beyond
        ``fromisoformat``'s taste on older Pythons), but be tolerant of a
        seconds-from-now number too.
        """
        default = time.time() + 3300
        if value in (None, ""):
            return default
        if isinstance(value, (int, float)):
            return time.time() + float(value)
        text = str(value).strip().replace("Z", "+00:00")
        # Trim fractional seconds to microseconds.
        if "." in text:
            head, _, tail = text.partition(".")
            digits = "".join(ch for ch in tail if ch.isdigit())[:6]
            offset = tail[len(tail) - 6:] if ("+" in tail or "-" in tail) else ""
            text = f"{head}.{digits or '0'}{offset}"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return default
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    # --- requests -----------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[Dict[str, Any]] = None,
        _retried: bool = False,
    ) -> Dict[str, Any]:
        """Call the API and return the decoded envelope.

        Raises :class:`SmarterMailError` for transport errors, non-2xx
        responses, and ``success: false`` envelopes.
        """
        if not path.startswith("/"):
            path = "/" + path
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Accept": "application/json",
        }
        try:
            resp = self._client.request(
                method.upper(), path, json=json, params=params, headers=headers
            )
        except httpx.HTTPError as exc:
            raise SmarterMailError(f"SmarterMail {path} failed: {exc}") from exc

        if resp.status_code in (401, 403) and not _retried:
            with self._lock:
                self._access_token = None
                self._authenticate()
            return self.request(
                method, path, json=json, params=params, _retried=True
            )
        if resp.status_code == 405:
            raise SmarterMailError(
                f"SmarterMail {path}: HTTP 405 — wrong verb for this endpoint "
                f"(tried {method.upper()})"
            )
        return self._payload(resp, path)

    def get(self, path: str, **kwargs) -> Dict[str, Any]:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> Dict[str, Any]:
        return self.request("POST", path, **kwargs)

    def download(self, method: str, path: str, *, json: Any = None,
                 params: Optional[Dict[str, Any]] = None) -> bytes:
        """Fetch a binary body (exports, attachments, archives)."""
        if not path.startswith("/"):
            path = "/" + path
        headers = {"Authorization": f"Bearer {self._token()}"}
        try:
            resp = self._client.request(
                method.upper(), path, json=json, params=params, headers=headers
            )
        except httpx.HTTPError as exc:
            raise SmarterMailError(f"SmarterMail {path} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise SmarterMailError(
                f"SmarterMail {path}: HTTP {resp.status_code} {resp.text[:200]}"
            )
        return resp.content

    # --- helpers ------------------------------------------------------
    @staticmethod
    def _payload(resp: httpx.Response, path: str) -> Dict[str, Any]:
        if resp.status_code >= 400:
            raise SmarterMailError(
                f"SmarterMail {path}: HTTP {resp.status_code} {resp.text[:300]}"
            )
        if not resp.content:
            return {}
        try:
            data = resp.json()
        except Exception as exc:
            raise SmarterMailError(
                f"SmarterMail {path}: non-JSON response {resp.text[:200]}"
            ) from exc
        if isinstance(data, dict) and data.get("success") is False:
            message = data.get("message") or data.get("resultCode") or "unknown error"
            raise SmarterMailError(f"SmarterMail {path}: {message}")
        return data if isinstance(data, dict) else {"value": data}


class SmarterMailRegistry:
    """Per-mailbox :class:`SmarterMailClient` cache.

    Credentials come from the same place EWS gets them: ``EWS_PASSWORD`` or the
    Supabase ``ews_accounts`` rows. The base URL defaults to the host of
    ``EWS_SERVER_URL`` so a single-server deployment needs no extra config.
    """

    def __init__(self, config) -> None:
        self.config = config
        self.logger = logging.getLogger(__name__)
        self._clients: Dict[str, SmarterMailClient] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "smartermail_enabled", False)) and bool(
            self.base_url
        )

    @property
    def base_url(self) -> Optional[str]:
        explicit = getattr(self.config, "smartermail_base_url", None)
        if explicit:
            return explicit.rstrip("/")
        server = getattr(self.config, "ews_server_url", None)
        if not server:
            return None
        parsed = urlparse(server if "//" in server else f"https://{server}")
        if not parsed.netloc:
            return None
        return f"{parsed.scheme or 'https'}://{parsed.netloc}"

    def password_for(self, email: str) -> Optional[str]:
        target = (email or "").lower()
        primary = (self.config.ews_email or "").lower()
        if target == primary:
            pw = self.config.get_primary_password()
            if pw:
                return pw
        for addr, pw in (self.config.ews_accounts or {}).items():
            if addr.lower() == target:
                return pw
        return None

    def get(self, email: Optional[str] = None) -> SmarterMailClient:
        address = (email or self.config.ews_email or "").strip()
        if not address:
            raise SmarterMailError("No mailbox address to authenticate with")
        if not self.enabled:
            raise SmarterMailError(
                "SmarterMail REST is disabled or SMARTERMAIL_BASE_URL is unset"
            )
        key = address.lower()
        with self._lock:
            client = self._clients.get(key)
            if client is not None:
                return client
        password = self.password_for(address)
        if not password:
            raise SmarterMailError(
                f"No password available for {address} — add it to the "
                "ews_accounts table to use SmarterMail REST tools"
            )
        client = SmarterMailClient(
            self.base_url,
            address,
            password,
            timeout=float(getattr(self.config, "request_timeout", 30) or 30),
            verify=not getattr(self.config, "ews_insecure_skip_verify", False),
        )
        with self._lock:
            self._clients[key] = client
        return client

    def close(self) -> None:
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            client.close()
