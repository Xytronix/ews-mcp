"""Gmail OAuth 2.0 (XOAUTH2) sign-in for the IMAP/SMTP backend.

Only the network boundaries are faked: the token endpoint (httpx
``MockTransport``) and the imaplib/smtplib connection classes, which follow
the stdlib's SASL callback contract.
"""
from __future__ import annotations

import imaplib
import logging
import smtplib
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.mail_backends.accounts import GOOGLE_TOKEN_URL, MailAccount, accounts_from_env
from src.mail_backends.base import MailBackendError, OutgoingMessage
from src.mail_backends.google_oauth import OAuthTokenSource
from src.mail_backends.imap_backend import IMAPBackend

OAUTH_SETTINGS = {
    "imap_host": "imap.example.com",
    "imap_port": 993,
    "imap_ssl": True,
    "smtp_host": "smtp.example.com",
    "smtp_port": 587,
    "smtp_starttls": True,
    "client_id": "cid.apps.example.com",
    "client_secret": "client-secret-value",
    "refresh_token": "refresh-token-value",
}


def oauth_account(**overrides) -> MailAccount:
    return MailAccount(
        email="reader@example.com",
        provider="gmail",
        settings={**OAUTH_SETTINGS, **overrides},
    )


class TokenEndpoint:
    """Fake OAuth token endpoint issuing ``at-1``, ``at-2``, ..."""

    def __init__(self, responses=None):
        self.requests = []
        self.responses = list(responses or [])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        self.requests.append((str(request.url), form))
        if self.responses:
            return self.responses.pop(0)
        return httpx.Response(
            200, json={"access_token": f"at-{len(self.requests)}", "expires_in": 3600}
        )

    def source(self, account: MailAccount, clock=time.monotonic) -> OAuthTokenSource:
        return OAuthTokenSource(
            account, client=httpx.Client(transport=httpx.MockTransport(self)), clock=clock
        )


# ----------------------------------------------------------------------
# token source
# ----------------------------------------------------------------------
def test_refresh_grant_is_posted_to_google_by_default_and_cached_until_60s_before_expiry():
    endpoint = TokenEndpoint()
    now = [1000.0]
    source = endpoint.source(oauth_account(), clock=lambda: now[0])

    assert source.access_token() == ("at-1", False)
    url, form = endpoint.requests[0]
    assert url == GOOGLE_TOKEN_URL
    assert form == {
        "grant_type": "refresh_token",
        "client_id": "cid.apps.example.com",
        "client_secret": "client-secret-value",
        "refresh_token": "refresh-token-value",
    }

    now[0] = 1000.0 + 3600 - 61
    assert source.access_token() == ("at-1", True)
    assert len(endpoint.requests) == 1

    now[0] = 1000.0 + 3600 - 59
    assert source.access_token() == ("at-2", False)
    assert len(endpoint.requests) == 2


def test_token_url_override_and_public_client_without_secret():
    endpoint = TokenEndpoint()
    account = oauth_account(oauth_token_url="https://auth.example.com/token", client_secret=None)
    endpoint.source(account).access_token()

    url, form = endpoint.requests[0]
    assert url == "https://auth.example.com/token"
    assert "client_secret" not in form


def test_rotated_refresh_token_is_kept_and_reported_once_without_the_token(caplog):
    endpoint = TokenEndpoint(
        [
            httpx.Response(
                200, json={"access_token": "at-1", "expires_in": 3600, "refresh_token": "rotated-1"}
            ),
            httpx.Response(
                200, json={"access_token": "at-2", "expires_in": 3600, "refresh_token": "rotated-2"}
            ),
        ]
    )
    now = [0.0]
    source = endpoint.source(oauth_account(), clock=lambda: now[0])
    with caplog.at_level(logging.DEBUG):
        for step in range(3):
            now[0] = step * 10_000.0
            source.access_token()

    assert [form["refresh_token"] for _, form in endpoint.requests] == [
        "refresh-token-value",
        "rotated-1",
        "rotated-2",
    ]
    assert len([r for r in caplog.records if "rotates refresh tokens" in r.getMessage()]) == 1
    for secret in ("refresh-token-value", "rotated-1", "rotated-2", "at-1", "at-2"):
        assert secret not in caplog.text


def test_invalid_grant_asks_for_a_new_sign_in_without_leaking_secrets():
    endpoint = TokenEndpoint(
        [
            httpx.Response(
                400,
                json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
            )
        ]
    )
    with pytest.raises(MailBackendError) as caught:
        endpoint.source(oauth_account()).access_token()

    message = str(caught.value)
    assert "revoked" in message
    assert "python -m src.mail_backends.google_oauth --email reader@example.com" in message
    assert "refresh-token-value" not in message
    assert "client-secret-value" not in message


@pytest.mark.parametrize(
    "failure, expected",
    [
        (httpx.Response(503, text="<html>unavailable</html>"), "HTTP 503"),
        (httpx.Response(200, json={"token_type": "Bearer"}), "no access_token"),
        (httpx.ConnectError("connection refused"), "unreachable"),
    ],
)
def test_token_endpoint_failures_say_what_failed(failure, expected):
    def handler(request):
        if isinstance(failure, Exception):
            raise failure
        return failure

    source = OAuthTokenSource(
        oauth_account(), client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(MailBackendError) as caught:
        source.access_token()
    assert expected in str(caught.value)
    assert "oauth2.googleapis.com/token" in str(caught.value)


def test_concurrent_callers_share_one_refresh():
    calls = []

    def slow(request):
        calls.append(request)
        time.sleep(0.05)
        return httpx.Response(200, json={"access_token": "shared", "expires_in": 3600})

    source = OAuthTokenSource(
        oauth_account(), client=httpx.Client(transport=httpx.MockTransport(slow))
    )
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(source.access_token()[0]))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == ["shared"] * 8
    assert len(calls) == 1


def test_renew_keeps_a_token_another_thread_already_renewed():
    endpoint = TokenEndpoint()
    source = endpoint.source(oauth_account())
    first, _ = source.access_token()
    second = source.renew(first)
    # A second worker that also saw ``first`` refused must not refresh again.
    assert source.renew(first) == second == "at-2"
    assert len(endpoint.requests) == 2


# ----------------------------------------------------------------------
# IMAP
# ----------------------------------------------------------------------
class FakeIMAPServer:
    """Plays the server side of imaplib's AUTHENTICATE / LOGIN contract."""

    def __init__(self):
        self.accepted_tokens = {"at-1"}
        self.sasl = []
        self.logins = []

    def factory(self, host, port, **kwargs):
        server = self

        class Connection:
            def __init__(self):
                self.untagged_responses = {"UIDVALIDITY": [b"7"]}

            def authenticate(self, mechanism, authobject):
                assert mechanism == "XOAUTH2"
                response = authobject(b"")
                server.sasl.append(response)
                token = response.split(b"auth=Bearer ", 1)[1].rstrip(b"\x01").decode()
                if token in server.accepted_tokens:
                    return "OK", [b"authenticated"]
                # Gmail sends an error challenge that the client must answer empty.
                assert authobject(b'{"status":"401","schemes":"Bearer"}') == b""
                raise imaplib.IMAP4.error("AUTHENTICATE command error: NO [AUTHENTICATIONFAILED] Invalid credentials")

            def select(self, mailbox, readonly=True):
                return "OK", [b"3"]

            def login(self, user, password):
                server.logins.append((user, password))
                return "OK", [b"logged in"]

            def list(self):
                return "OK", [b'(\\HasNoChildren) "/" "INBOX"']

            def status(self, mailbox, items):
                return "OK", [b'"INBOX" (MESSAGES 3 UNSEEN 1)']

            def noop(self):
                return "OK", []

            def logout(self):
                return "BYE", []

            def shutdown(self):
                pass

        return Connection()


@pytest.fixture
def imap_server(monkeypatch):
    server = FakeIMAPServer()
    monkeypatch.setattr(imaplib, "IMAP4_SSL", server.factory)
    return server


def test_oauth_account_without_password_signs_in_to_imap_with_xoauth2(imap_server):
    endpoint = TokenEndpoint()
    backend = IMAPBackend(oauth_account())
    backend._oauth = endpoint.source(backend.account)

    folders = backend.list_folders()

    assert [f.well_known for f in folders] == ["inbox"]
    assert imap_server.sasl == [b"user=reader@example.com\x01auth=Bearer at-1\x01\x01"]
    assert imap_server.logins == []


def test_imap_xoauth2_uses_the_configured_imap_username(imap_server):
    endpoint = TokenEndpoint()
    backend = IMAPBackend(oauth_account(imap_username="login-name@example.com"))
    backend._oauth = endpoint.source(backend.account)

    backend.list_folders()

    assert imap_server.sasl[0].startswith(b"user=login-name@example.com\x01")


def test_password_account_still_uses_imap_login(imap_server):
    account = MailAccount(
        email="reader@example.com",
        provider="imap",
        password="app-password",
        settings={"imap_host": "imap.example.com", "imap_port": 993, "imap_ssl": True},
    )
    IMAPBackend(account).list_folders()

    assert imap_server.logins == [("reader@example.com", "app-password")]
    assert imap_server.sasl == []


def test_refused_cached_token_is_renewed_once_and_imap_reconnects(imap_server):
    endpoint = TokenEndpoint()
    backend = IMAPBackend(oauth_account())
    backend._oauth = endpoint.source(backend.account)
    backend.list_folders()
    backend.close()

    # The cached at-1 was revoked server-side before its expiry.
    imap_server.accepted_tokens = {"at-2"}
    result = backend.test_connection()

    assert result["connected"] is True
    assert [s.split(b"Bearer ")[1] for s in imap_server.sasl] == [
        b"at-1\x01\x01",
        b"at-1\x01\x01",
        b"at-2\x01\x01",
    ]
    assert len(endpoint.requests) == 2


def test_imap_retry_happens_only_once_then_reports_an_oauth_error(imap_server):
    endpoint = TokenEndpoint()
    backend = IMAPBackend(oauth_account())
    backend._oauth = endpoint.source(backend.account)
    backend.list_folders()
    backend.close()
    imap_server.accepted_tokens = set()

    result = backend.test_connection()

    assert result["connected"] is False
    assert len(imap_server.sasl) == 3
    assert len(endpoint.requests) == 2
    assert "XOAUTH2 sign-in failed" in result["error"]
    assert "https://mail.google.com/" in result["error"]
    assert "app-specific password" not in result["error"]
    assert "at-2" not in result["error"]


def test_freshly_fetched_token_refused_is_not_retried(imap_server):
    endpoint = TokenEndpoint()
    backend = IMAPBackend(oauth_account())
    backend._oauth = endpoint.source(backend.account)
    imap_server.accepted_tokens = set()

    with pytest.raises(MailBackendError, match="XOAUTH2 sign-in failed"):
        backend.list_folders()
    assert len(imap_server.sasl) == 1
    assert len(endpoint.requests) == 1


def test_revoked_refresh_token_surfaces_through_imap(imap_server):
    endpoint = TokenEndpoint([httpx.Response(400, json={"error": "invalid_grant"})])
    backend = IMAPBackend(oauth_account())
    backend._oauth = endpoint.source(backend.account)

    result = backend.test_connection()

    assert result["connected"] is False
    assert "sign in again" in result["error"]
    assert imap_server.sasl == []


# ----------------------------------------------------------------------
# SMTP
# ----------------------------------------------------------------------
class FakeSMTPServer:
    """Plays the server side of smtplib's ``auth()`` / ``login()`` contract."""

    def __init__(self):
        self.accepted_tokens = {"at-1"}
        self.events = []
        self.delivered = []

    def factory(self, host, port, **kwargs):
        server = self

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def ehlo(self):
                server.events.append("ehlo")

            def ehlo_or_helo_if_needed(self):
                server.events.append("ehlo")

            def starttls(self, context=None):
                server.events.append("starttls")

            def login(self, user, password):
                server.events.append(("login", user, password))

            def auth(self, mechanism, authobject, *, initial_response_ok=True):
                response = authobject()
                server.events.append(("auth", mechanism, response))
                token = response.split("auth=Bearer ", 1)[1].rstrip("\x01")
                if token in server.accepted_tokens:
                    return 235, b"2.7.0 Accepted"
                assert authobject(b'{"status":"400","schemes":"Bearer"}') == ""
                raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

            def send_message(self, message, to_addrs=None):
                server.delivered.append(list(to_addrs))
                return {}

            def sendmail(self, sender, to_addrs, payload):
                server.delivered.append(list(to_addrs))
                return {}

        return Connection()


@pytest.fixture
def smtp_server(monkeypatch):
    server = FakeSMTPServer()
    monkeypatch.setattr(smtplib, "SMTP", server.factory)
    monkeypatch.setattr(smtplib, "SMTP_SSL", server.factory)
    return server


def _send(backend):
    return backend.send_message(
        OutgoingMessage(to=["friend@example.com"], subject="hi", body="hello", body_format="text")
    )


@pytest.mark.parametrize("smtp_ssl", [False, True])
def test_oauth_account_sends_with_smtp_xoauth2(smtp_server, smtp_ssl):
    endpoint = TokenEndpoint()
    backend = IMAPBackend(oauth_account(smtp_ssl=smtp_ssl, smtp_port=465 if smtp_ssl else 587))
    backend._oauth = endpoint.source(backend.account)

    _send(backend)

    auths = [e for e in smtp_server.events if e[0] == "auth"]
    assert auths == [("auth", "XOAUTH2", "user=reader@example.com\x01auth=Bearer at-1\x01\x01")]
    assert not [e for e in smtp_server.events if e[0] == "login"]
    # AUTH needs EHLO first (smtplib.login does it implicitly, auth does not).
    assert smtp_server.events.index("ehlo") < smtp_server.events.index(auths[0])
    if not smtp_ssl:
        assert "starttls" in smtp_server.events
    assert smtp_server.delivered == [["friend@example.com"]]


def test_smtp_uses_smtp_username_for_xoauth2(smtp_server):
    endpoint = TokenEndpoint()
    backend = IMAPBackend(oauth_account(smtp_username="sender-login@example.com"))
    backend._oauth = endpoint.source(backend.account)

    _send(backend)

    auth = next(e for e in smtp_server.events if e[0] == "auth")
    assert auth[2].startswith("user=sender-login@example.com\x01")


def test_smtp_renews_a_refused_cached_token_once_and_sends_once(smtp_server):
    endpoint = TokenEndpoint()
    backend = IMAPBackend(oauth_account())
    backend._oauth = endpoint.source(backend.account)
    _send(backend)
    smtp_server.accepted_tokens = {"at-2"}

    _send(backend)

    tokens = [e[2].split("Bearer ")[1] for e in smtp_server.events if e[0] == "auth"]
    assert tokens == ["at-1\x01\x01", "at-1\x01\x01", "at-2\x01\x01"]
    assert smtp_server.delivered == [["friend@example.com"], ["friend@example.com"]]


def test_smtp_oauth_failure_after_retry_is_not_an_app_password_hint(smtp_server):
    endpoint = TokenEndpoint()
    backend = IMAPBackend(oauth_account())
    backend._oauth = endpoint.source(backend.account)
    _send(backend)
    smtp_server.accepted_tokens = set()

    with pytest.raises(MailBackendError) as caught:
        _send(backend)

    assert len([e for e in smtp_server.events if e[0] == "auth"]) == 3
    assert smtp_server.delivered == [["friend@example.com"]]
    assert "SMTP XOAUTH2 sign-in failed" in str(caught.value)
    assert "app-specific password" not in str(caught.value)


def test_password_account_still_uses_smtp_login(smtp_server):
    account = MailAccount(
        email="reader@example.com",
        provider="gmail",
        password="app-password",
        settings={"smtp_host": "smtp.example.com", "smtp_port": 587, "smtp_starttls": True},
    )
    _send(IMAPBackend(account))

    assert ("login", "reader@example.com", "app-password") in smtp_server.events
    assert not [e for e in smtp_server.events if e[0] == "auth"]


# ----------------------------------------------------------------------
# convenience env vars
# ----------------------------------------------------------------------
GMAIL_OAUTH_ENV = {
    "GMAIL_EMAIL": "reader@example.com",
    "GMAIL_OAUTH_CLIENT_ID": "cid.apps.example.com",
    "GMAIL_OAUTH_CLIENT_SECRET": "client-secret-value",
    "GMAIL_OAUTH_REFRESH_TOKEN": "refresh-token-value",
}


def test_gmail_oauth_env_builds_a_valid_account_without_app_password():
    (account,) = accounts_from_env(GMAIL_OAUTH_ENV)

    account.validate()
    assert account.uses_oauth
    assert account.describe()["auth"] == "oauth2"
    assert account.get("imap_host") == "imap.gmail.com"
    assert account.get("client_id") == "cid.apps.example.com"
    assert account.get("client_secret") == "client-secret-value"
    assert account.get("refresh_token") == "refresh-token-value"
    assert account.get("oauth_token_url") is None


def test_gmail_oauth_env_token_url_override():
    env = {**GMAIL_OAUTH_ENV, "GMAIL_OAUTH_TOKEN_URL": "https://auth.example.com/token"}
    (account,) = accounts_from_env(env)
    assert account.get("oauth_token_url") == "https://auth.example.com/token"


def test_oauth_wins_when_app_password_is_also_set(imap_server):
    (account,) = accounts_from_env({**GMAIL_OAUTH_ENV, "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop"})
    assert account.describe()["auth"] == "oauth2"

    backend = IMAPBackend(account)
    backend._oauth = TokenEndpoint().source(account)
    backend.list_folders()
    assert imap_server.logins == []
    assert len(imap_server.sasl) == 1


def test_gmail_app_password_env_is_unchanged():
    (account,) = accounts_from_env({"GMAIL_EMAIL": "reader@example.com", "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop"})
    assert account.password == "abcdefghijklmnop"
    assert not account.uses_oauth


def test_gmail_email_alone_is_skipped_with_both_options_named(caplog):
    with caplog.at_level(logging.WARNING):
        assert accounts_from_env({"GMAIL_EMAIL": "reader@example.com"}) == []
    assert "GMAIL_OAUTH_REFRESH_TOKEN" in caplog.text
    assert "GMAIL_APP_PASSWORD" in caplog.text
