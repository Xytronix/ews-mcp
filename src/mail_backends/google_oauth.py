"""Standalone OAuth 2.0 sign-in for Gmail over IMAP/SMTP (SASL XOAUTH2).

Gmail accepts an OAuth access token instead of an app password on
``imap.gmail.com`` and ``smtp.gmail.com``. This module is self-contained: it
reads only the account's own ``client_id`` / ``client_secret`` /
``refresh_token`` settings, never another service's tokens or files.

* :class:`OAuthTokenSource` turns the long-lived refresh token into
  short-lived access tokens for the IMAP/SMTP sign-in.
* ``python -m src.mail_backends.google_oauth --email you@gmail.com
  --client-id ...`` runs the one-time consent (authorization code + PKCE) and
  prints the refresh token with ready-to-paste configuration.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import logging
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, Optional, Tuple

import httpx

from .accounts import GOOGLE_TOKEN_URL, MailAccount
from .base import MailBackendError

_log = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GMAIL_SCOPE = "https://mail.google.com/"
SIGN_IN_COMMAND = "python -m src.mail_backends.google_oauth"

_HTTP_TIMEOUT = 30.0
# Renew this long before the token endpoint's expires_in runs out.
_EXPIRY_MARGIN = 60.0
_LISTEN_TIMEOUT = 300.0


def xoauth2_string(user: str, access_token: str) -> str:
    """SASL XOAUTH2 client response, before the protocol's base64 step."""
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01"


def sign_in_hint(email: str) -> str:
    return f"`{SIGN_IN_COMMAND} --email {email} --client-id <client id>`"


class TokenEndpointError(MailBackendError):
    """The token endpoint refused a grant; ``error`` is the OAuth error code."""

    def __init__(self, url: str, status: int, payload: Dict[str, Any]) -> None:
        self.error = str(payload.get("error") or "")
        self.description = str(payload.get("error_description") or "")
        detail = ": ".join(p for p in (self.error, self.description) if p)
        super().__init__(
            f"token endpoint {url} answered HTTP {status}: "
            f"{detail or 'no access_token in the response'}"
        )


def _token_request(
    url: str, data: Dict[str, str], client: Optional[httpx.Client]
) -> Dict[str, Any]:
    """POST a token grant; return the JSON payload, which has an access_token."""
    try:
        if client is not None:
            resp = client.post(url, data=data, timeout=_HTTP_TIMEOUT)
        else:
            resp = httpx.post(url, data=data, timeout=_HTTP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise MailBackendError(f"token endpoint {url} unreachable: {exc}") from exc
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    if resp.status_code >= 400 or not payload.get("access_token"):
        raise TokenEndpointError(url, resp.status_code, payload)
    return payload


class OAuthTokenSource:
    """Thread-safe access-token cache for one account's refresh token."""

    def __init__(
        self,
        account: MailAccount,
        client: Optional[httpx.Client] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._email = account.email
        self._token_url = str(account.get("oauth_token_url") or GOOGLE_TOKEN_URL)
        self._client_id = str(account.get("client_id") or "")
        self._client_secret = str(account.get("client_secret") or "")
        self._refresh_token = str(account.get("refresh_token") or "")
        self._client = client
        self._clock = clock
        self._lock = threading.Lock()
        self._access_token: Optional[str] = None
        self._renew_at = 0.0
        self._rotated = False

    def access_token(self) -> Tuple[str, bool]:
        """Return ``(token, cached)``; ``cached`` is False for a token fetched now."""
        with self._lock:
            if self._access_token and self._clock() < self._renew_at:
                return self._access_token, True
            return self._refresh(), False

    def renew(self, rejected: str) -> str:
        """Drop the server-refused ``rejected`` token and return a fresh one."""
        with self._lock:
            if (
                self._access_token
                and self._access_token != rejected
                and self._clock() < self._renew_at
            ):
                return self._access_token  # another thread already renewed it
            self._access_token = None
            return self._refresh()

    def _refresh(self) -> str:
        if not self._client_id:
            raise MailBackendError(f"{self._email}: OAuth sign-in needs client_id")
        data = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "refresh_token": self._refresh_token,
        }
        if self._client_secret:
            data["client_secret"] = self._client_secret
        try:
            payload = _token_request(self._token_url, data, self._client)
        except MailBackendError as exc:
            error = getattr(exc, "error", "")
            if error == "invalid_grant":
                raise MailBackendError(
                    f"{self._email}: the OAuth refresh token was rejected ({exc}). "
                    f"It was revoked, expired or issued to another client id; sign in "
                    f"again with {sign_in_hint(self._email)} and store the new refresh token."
                ) from exc
            if error in ("invalid_client", "unauthorized_client"):
                raise MailBackendError(
                    f"{self._email}: the OAuth client was rejected ({exc}). Check "
                    f"client_id and client_secret; they must belong to the client "
                    f"that issued the refresh token."
                ) from exc
            raise MailBackendError(
                f"{self._email}: OAuth access-token refresh failed: {exc}"
            ) from exc

        self._access_token = str(payload["access_token"])
        try:
            lifetime = float(payload.get("expires_in") or 3600)
        except (TypeError, ValueError):
            lifetime = 3600.0
        self._renew_at = self._clock() + lifetime - _EXPIRY_MARGIN
        rotated = payload.get("refresh_token")
        if rotated and rotated != self._refresh_token:
            if not self._rotated:
                _log.warning(
                    "%s: the token endpoint rotates refresh tokens; the newest one is "
                    "kept in memory only, so the configured one may be refused after a "
                    "restart (sign in again then)",
                    self._email,
                )
            self._rotated = True
            self._refresh_token = str(rotated)
        return self._access_token


# ----------------------------------------------------------------------
# sign-in command
# ----------------------------------------------------------------------
class SignInError(Exception):
    """The consent step did not produce a usable authorization code."""


def pkce_pair() -> Tuple[str, str]:
    """``(code_verifier, S256 code_challenge)`` per RFC 7636."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def consent_url(
    auth_url: str,
    *,
    client_id: str,
    redirect_uri: str,
    email: str,
    state: str,
    code_challenge: str,
    scope: str = GMAIL_SCOPE,
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "access_type": "offline",
        "prompt": "consent",
        "login_hint": email,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    separator = "&" if "?" in auth_url else "?"
    return auth_url + separator + urllib.parse.urlencode(params)


def authorization_code(reply: str, state: str) -> str:
    """Code from a pasted redirect URL (its ``state`` must match) or a bare code."""
    text = reply.strip()
    if not text:
        raise SignInError("nothing was pasted")
    if "code=" not in text and "error=" not in text:
        return urllib.parse.unquote(text)
    query = urllib.parse.urlsplit(text).query if "?" in text else text
    params = urllib.parse.parse_qs(query)
    if params.get("state", [""])[0] != state:
        raise SignInError(
            "state mismatch: the redirect does not belong to this sign-in attempt "
            "(stale or forged); run the command again"
        )
    if "error" in params:
        detail = params.get("error_description", [""])[0]
        raise SignInError(
            f"consent was refused: {params['error'][0]}" + (f": {detail}" if detail else "")
        )
    code = params.get("code", [""])[0]
    if not code:
        raise SignInError("the redirect carries no authorization code")
    return code


class _RedirectListener:
    """One-shot loopback HTTP listener that captures the OAuth redirect."""

    def __init__(self) -> None:
        captured: Dict[str, str] = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            timeout = 10  # a browser pre-connect must not block the listener

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                query = urllib.parse.urlsplit(self.path).query
                params = urllib.parse.parse_qs(query)
                if "code" not in params and "error" not in params:
                    self.send_error(404)
                    return
                captured.setdefault("query", query)
                body = b"Sign-in response received. Return to the terminal.\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass  # the request line carries the authorization code

        self._captured = captured
        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self._server.timeout = 1.0
        self.redirect_uri = f"http://127.0.0.1:{self._server.server_port}/"

    def wait(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while "query" not in self._captured:
            if time.monotonic() >= deadline:
                raise SignInError(
                    f"no redirect reached {self.redirect_uri} within {timeout:.0f} s; "
                    f"if the browser runs on another machine, use --no-listen"
                )
            self._server.handle_request()
        return f"{self.redirect_uri}?{self._captured['query']}"

    def close(self) -> None:
        self._server.server_close()


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _config_text(
    email: str,
    client_id: str,
    client_secret: Optional[str],
    refresh_token: str,
    token_url: str,
) -> str:
    env = [f"GMAIL_EMAIL={email}", f"GMAIL_OAUTH_CLIENT_ID={client_id}"]
    entry: Dict[str, str] = {"email": email, "provider": "gmail", "client_id": client_id}
    if client_secret:
        env.append(f"GMAIL_OAUTH_CLIENT_SECRET={client_secret}")
        entry["client_secret"] = client_secret
    env.append(f"GMAIL_OAUTH_REFRESH_TOKEN={refresh_token}")
    entry["refresh_token"] = refresh_token
    if token_url != GOOGLE_TOKEN_URL:
        env.append(f"GMAIL_OAUTH_TOKEN_URL={token_url}")
        entry["oauth_token_url"] = token_url
    return "\n".join(
        [
            f"Refresh token for {email}:",
            refresh_token,
            "",
            "Environment variables:",
            *env,
            "",
            "Or this MAIL_ACCOUNTS entry (add it to the JSON array):",
            json.dumps(entry),
        ]
    )


def main(argv: Optional[list] = None, client: Optional[httpx.Client] = None) -> int:
    parser = argparse.ArgumentParser(
        prog=SIGN_IN_COMMAND,
        description=(
            "Sign a Gmail address in with OAuth 2.0 (scope https://mail.google.com/) "
            "and print the refresh token for IMAP/SMTP XOAUTH2."
        ),
    )
    parser.add_argument("--email", required=True, help="Gmail address to sign in")
    parser.add_argument("--client-id", required=True, help="Google OAuth client ID")
    parser.add_argument("--client-secret", help="Google OAuth client secret")
    parser.add_argument(
        "--redirect-uri",
        help="redirect URI registered on a Web-app client; the redirected URL is pasted back",
    )
    parser.add_argument(
        "--no-listen",
        action="store_true",
        help="do not start the loopback listener; paste the redirected URL instead",
    )
    parser.add_argument(
        "--auth-url", default=GOOGLE_AUTH_URL, help="authorization endpoint (default: Google)"
    )
    parser.add_argument(
        "--token-url", default=GOOGLE_TOKEN_URL, help="token endpoint (default: Google)"
    )
    parser.add_argument(
        "--scope", default=GMAIL_SCOPE, help=f"requested scope (default: {GMAIL_SCOPE})"
    )
    args = parser.parse_args(argv)

    out, err = sys.stdout, sys.stderr
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)

    def show(redirect_uri: str, then: str) -> None:
        url = consent_url(
            args.auth_url,
            client_id=args.client_id,
            redirect_uri=redirect_uri,
            email=args.email,
            state=state,
            code_challenge=challenge,
            scope=args.scope,
        )
        print(f"Open this URL in a browser and allow access for {args.email}:\n", file=err)
        print(url + "\n", file=err)
        print(then, file=err)
        err.flush()

    try:
        if args.redirect_uri or args.no_listen:
            redirect_uri = args.redirect_uri or f"http://127.0.0.1:{_unused_loopback_port()}/"
            show(
                redirect_uri,
                "After consent the browser is sent to the redirect URI (an error page "
                "there is fine). Paste that full URL from the address bar, or just the code:",
            )
            reply = sys.stdin.readline()
        else:
            listener = _RedirectListener()
            try:
                redirect_uri = listener.redirect_uri
                show(redirect_uri, f"Waiting for the redirect to {redirect_uri} ...")
                reply = listener.wait(_LISTEN_TIMEOUT)
            finally:
                listener.close()
        code = authorization_code(reply, state)
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": args.client_id,
            "code_verifier": verifier,
        }
        if args.client_secret:
            data["client_secret"] = args.client_secret
        try:
            tokens = _token_request(args.token_url, data, client)
        except TokenEndpointError as exc:
            if exc.error == "invalid_grant":
                raise SignInError(
                    f"the authorization code was refused ({exc}): it expired, was already "
                    f"used, or the redirect URI differs; run the command again"
                ) from exc
            raise
    except (SignInError, MailBackendError) as exc:
        print(f"Sign-in failed: {exc}", file=err)
        return 1

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print(
            "Sign-in failed: the token endpoint returned no refresh token. Google only "
            "issues one with access_type=offline on a consent screen (this command "
            "sends prompt=consent); remove this app's earlier grant at "
            "https://myaccount.google.com/permissions and run the command again.",
            file=err,
        )
        return 1
    print(
        _config_text(
            args.email, args.client_id, args.client_secret, str(refresh_token), args.token_url
        ),
        file=out,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
