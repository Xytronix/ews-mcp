"""The Gmail OAuth sign-in command, with the token endpoint faked over httpx."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.mail_backends.accounts import GOOGLE_TOKEN_URL, accounts_from_env, build_account
from src.mail_backends.google_oauth import GOOGLE_AUTH_URL, main


class TokenEndpoint:
    def __init__(self, response=None):
        self.requests = []
        self.response = response or httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rt-new", "expires_in": 3599}
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((str(request.url), dict(urllib.parse.parse_qsl(request.content.decode()))))
        return self.response

    @property
    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


class Terminal:
    """The user's terminal: reads the printed consent URL, answers on stdin."""

    def __init__(self, answer, auth_url=GOOGLE_AUTH_URL):
        self.err = io.StringIO()
        self.answer = answer
        self.auth_url = auth_url

    def consent_url(self):
        for line in self.err.getvalue().splitlines():
            if line.startswith(self.auth_url):
                return line
        return None

    def consent(self):
        query = urllib.parse.urlsplit(self.consent_url()).query
        return dict(urllib.parse.parse_qsl(query))

    def readline(self):
        return self.answer(self.consent()) + "\n"


@pytest.fixture
def terminal(monkeypatch):
    def attach(answer, **kwargs):
        term = Terminal(answer, **kwargs)
        monkeypatch.setattr(sys, "stderr", term.err)
        monkeypatch.setattr(sys, "stdin", term)
        return term

    return attach


def redirected(consent, **params):
    params.setdefault("state", consent["state"])
    # Google leaves the "/" in its codes unescaped in the redirect.
    return consent["redirect_uri"] + "?" + "&".join(f"{k}={v}" for k, v in params.items())


def env_lines(stdout):
    return dict(
        line.split("=", 1) for line in stdout.splitlines() if re.match(r"^GMAIL_[A-Z_]+=", line)
    )


def test_pasted_redirect_is_exchanged_with_pkce_and_prints_working_config(terminal, capsys):
    endpoint = TokenEndpoint()
    term = terminal(lambda c: redirected(c, code="4/0Abc-def_ghi", scope="https://mail.google.com/"))

    code = main(
        ["--email", "reader@example.com", "--client-id", "cid.apps.example.com",
         "--client-secret", "client-secret-value", "--no-listen"],
        client=endpoint.client,
    )

    assert code == 0
    consent = term.consent()
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/", consent["redirect_uri"])
    assert {k: consent[k] for k in ("client_id", "response_type", "scope", "access_type",
                                    "prompt", "login_hint", "code_challenge_method")} == {
        "client_id": "cid.apps.example.com",
        "response_type": "code",
        "scope": "https://mail.google.com/",
        "access_type": "offline",
        "prompt": "consent",
        "login_hint": "reader@example.com",
        "code_challenge_method": "S256",
    }
    assert len(consent["state"]) >= 16

    ((url, form),) = endpoint.requests
    assert url == GOOGLE_TOKEN_URL
    verifier = form.pop("code_verifier")
    assert 43 <= len(verifier) <= 128
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert digest.decode() == consent["code_challenge"]
    assert form == {
        "grant_type": "authorization_code",
        "code": "4/0Abc-def_ghi",
        "redirect_uri": consent["redirect_uri"],
        "client_id": "cid.apps.example.com",
        "client_secret": "client-secret-value",
    }

    out = capsys.readouterr().out
    (from_env,) = accounts_from_env(env_lines(out))
    entry = json.loads(out.strip().splitlines()[-1])
    from_json = build_account(entry)
    for account in (from_env, from_json):
        account.validate()
        assert account.uses_oauth
        assert account.get("imap_host") == "imap.gmail.com"
        assert account.get("refresh_token") == "rt-new"
        assert account.get("client_secret") == "client-secret-value"


def test_web_client_redirect_uri_with_a_bare_pasted_code(terminal, capsys):
    endpoint = TokenEndpoint()
    term = terminal(lambda c: "4/0Abc-def_ghi")

    code = main(
        ["--email", "reader@example.com", "--client-id", "cid",
         "--redirect-uri", "http://localhost:8000/oauth2callback"],
        client=endpoint.client,
    )

    assert code == 0
    assert term.consent()["redirect_uri"] == "http://localhost:8000/oauth2callback"
    ((_, form),) = endpoint.requests
    assert form["redirect_uri"] == "http://localhost:8000/oauth2callback"
    assert form["code"] == "4/0Abc-def_ghi"
    assert "client_secret" not in form
    env = env_lines(capsys.readouterr().out)
    assert "GMAIL_OAUTH_CLIENT_SECRET" not in env
    assert env["GMAIL_OAUTH_REFRESH_TOKEN"] == "rt-new"


def test_endpoint_overrides_are_used_and_carried_into_the_config(terminal, capsys):
    endpoint = TokenEndpoint()
    auth_url = "https://auth.example.com/authorize?tenant=mail"
    term = terminal(lambda c: redirected(c, code="abc"), auth_url=auth_url)

    code = main(
        ["--email", "reader@example.com", "--client-id", "cid", "--no-listen",
         "--auth-url", auth_url, "--token-url", "https://auth.example.com/token",
         "--scope", "urn:ietf:params:oauth:scope:mail"],
        client=endpoint.client,
    )

    assert code == 0
    assert term.consent()["tenant"] == "mail"
    assert term.consent()["scope"] == "urn:ietf:params:oauth:scope:mail"
    assert endpoint.requests[0][0] == "https://auth.example.com/token"
    out = capsys.readouterr().out
    assert env_lines(out)["GMAIL_OAUTH_TOKEN_URL"] == "https://auth.example.com/token"
    assert json.loads(out.strip().splitlines()[-1])["oauth_token_url"] == "https://auth.example.com/token"


def test_state_mismatch_is_refused_before_any_token_request(terminal, capsys):
    endpoint = TokenEndpoint()
    term = terminal(lambda c: redirected(c, code="abc", state="forged"))

    code = main(
        ["--email", "reader@example.com", "--client-id", "cid", "--no-listen"],
        client=endpoint.client,
    )

    assert code == 1
    assert endpoint.requests == []
    assert "state mismatch" in term.err.getvalue()
    assert capsys.readouterr().out == ""


def test_denied_consent_reports_the_error(terminal):
    endpoint = TokenEndpoint()
    term = terminal(lambda c: redirected(c, error="access_denied"))

    assert main(["--email", "reader@example.com", "--client-id", "cid", "--no-listen"],
                client=endpoint.client) == 1
    assert "access_denied" in term.err.getvalue()
    assert endpoint.requests == []


def test_missing_refresh_token_explains_how_to_get_one(terminal, capsys):
    endpoint = TokenEndpoint(httpx.Response(200, json={"access_token": "at", "expires_in": 3599}))
    term = terminal(lambda c: redirected(c, code="abc"))

    assert main(["--email", "reader@example.com", "--client-id", "cid", "--no-listen"],
                client=endpoint.client) == 1
    err = term.err.getvalue()
    assert "no refresh token" in err
    assert "prompt=consent" in err
    assert "https://myaccount.google.com/permissions" in err
    assert capsys.readouterr().out == ""


def test_refused_code_says_to_run_again(terminal):
    endpoint = TokenEndpoint(
        httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad Request"})
    )
    term = terminal(lambda c: redirected(c, code="abc"))

    assert main(["--email", "reader@example.com", "--client-id", "cid", "--no-listen"],
                client=endpoint.client) == 1
    assert "invalid_grant" in term.err.getvalue()
    assert "run the command again" in term.err.getvalue()


def test_loopback_listener_catches_the_browser_redirect(terminal, capsys):
    endpoint = TokenEndpoint()
    term = terminal(lambda c: pytest.fail("loopback mode must not read stdin"))
    result = {}
    worker = threading.Thread(
        target=lambda: result.setdefault(
            "code",
            main(["--email", "reader@example.com", "--client-id", "cid"], client=endpoint.client),
        )
    )
    worker.start()
    deadline = time.monotonic() + 10
    while term.consent_url() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    consent = term.consent()

    with httpx.Client(trust_env=False, timeout=5) as browser:
        assert browser.get(consent["redirect_uri"] + "favicon.ico").status_code == 404
        landing = browser.get(redirected(consent, code="4/0Abc-def_ghi"))
    worker.join(timeout=10)

    assert landing.status_code == 200
    assert result["code"] == 0
    ((_, form),) = endpoint.requests
    assert form["code"] == "4/0Abc-def_ghi"
    assert form["redirect_uri"] == consent["redirect_uri"]
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/", consent["redirect_uri"])
    assert env_lines(capsys.readouterr().out)["GMAIL_OAUTH_REFRESH_TOKEN"] == "rt-new"
