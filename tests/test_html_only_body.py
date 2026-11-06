"""Regression: HTML-only mail where the EWS server omits TextBody/DateTimeSent.

Seen on HTML-only notification mails: text_body=None,
datetime_sent=None, body=HTML. read_emails/get_email_details returned an
empty body and get_email_details crashed on ``None.isoformat()``.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.body_format import html_to_text, render_body  # noqa: E402
from src.tools import email_tools as email_mod  # noqa: E402
from src.tools.mailbox_tools import GetEmailDetailsTool, ReadEmailsTool  # noqa: E402
from src.utils import (  # noqa: E402
    is_full_address, iso_or_none, item_text_body, mime_text_body, sender_matches,
)

LINK = "https://example.invalid/magic-link#abc"
HTML = (f'<!doctype html><html><head><style>p{{}}</style></head><body>'
        f'<p>Sign in</p><a href="{LINK}">Continue</a></body></html>')
RECEIVED = datetime(2026, 10, 11, 9, 20, 40, tzinfo=timezone.utc)


def _item():
    return SimpleNamespace(
        id="MID-HTML", subject="Dein sicherer Link", text_body=None, body=HTML,
        datetime_received=RECEIVED, datetime_sent=None, is_read=False,
        has_attachments=False, importance="Normal", attachments=[],
        to_recipients=[], cc_recipients=[], bcc_recipients=[],
        sender=SimpleNamespace(name="Anthropic",
                               email_address="no-reply-x@mail.anthropic.com"),
    )


class _Client:
    def __init__(self):
        self.config = SimpleNamespace(ews_email="user@example.com")
        self.mailbox_registry = SimpleNamespace(find_account=lambda a: None)
        self.sqlite_cache = None

    def get_account(self, target_mailbox=None):
        return SimpleNamespace()


def test_helpers():
    assert iso_or_none(None) is None
    assert iso_or_none(RECEIVED).startswith("2026-10-11")
    text = item_text_body(_item())
    assert "Sign in" in text and LINK in text and "<" not in text
    assert LINK in html_to_text(HTML)
    assert render_body(HTML, "", "text")[0] == html_to_text(HTML)
    mime = (b"Content-Type: text/html; charset=utf-8\r\n"
            b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
            b"<p>Code: <a href=3D\"" + LINK.encode() + b"\">go</a></p>\r\n")
    assert LINK in mime_text_body(mime)
    assert sender_matches(_item(), "anthropic.com")
    assert not sender_matches(_item(), "example.org")
    assert is_full_address("a@b.de") and not is_full_address("anthropic.com")


def test_get_email_details_html_only_no_sent_date(monkeypatch):
    monkeypatch.setattr(email_mod, "find_message_for_account", lambda *a, **k: _item())
    for fmt in ("text", "markdown", "html"):
        res = asyncio.run(GetEmailDetailsTool(_Client()).execute(
            message_id="MID-HTML", format=fmt))
        email = res["email"]
        assert email["body"].strip(), fmt
        assert LINK in email["body"]
        assert email["received_time"].startswith("2026-10-11")
        assert email["sent_time"] == email["received_time"]


def test_read_emails_include_body_html_only(monkeypatch):
    class _QS(list):
        def order_by(self, *_):
            return self

        def filter(self, **_):
            return self

    folder = SimpleNamespace(name="Inbox", all=lambda: _QS([_item()]))

    async def _resolve(*_a, **_k):
        return folder
    monkeypatch.setattr(email_mod, "resolve_folder_for_account", _resolve)
    for fmt in ("text", "html", "markdown"):
        res = asyncio.run(ReadEmailsTool(_Client()).execute(
            include_body=True, format=fmt))
        e = res["emails"][0]
        assert e["preview"].strip()
        assert LINK in e["body"]
    res = asyncio.run(ReadEmailsTool(_Client()).execute())
    assert "body" not in res["emails"][0] and res["emails"][0]["preview"]


def test_markdown_strips_css_head_and_comments():
    html = ("<!doctype html><html><head><title>T</title>"
            "<style>#outlook a { padding:0; } body { margin:0; }</style></head>"
            "<body><!--[if mso]><xml><o:x>junk</o:x></xml><![endif]-->"
            "<style type=\"text/css\">.x{color:red}</style>"
            f"<h1>Bei Claude.ai anmelden</h1><a href=\"{LINK}\">Go</a></body></html>")
    md, fmt = render_body(html, "", "markdown")
    assert fmt == "markdown"
    assert md.startswith("# Bei Claude.ai anmelden")
    assert "outlook" not in md and "{" not in md and "junk" not in md
    assert LINK in md


def test_markdown_flattens_layout_tables_keeps_data_tables():
    html = ("<html><body><table role=\"presentation\"><tr><td>"
            "<table><tr><td></td><td><table><tr><td>"
            "<h1>Bei Claude.ai anmelden</h1><p>Klicke unten.</p>"
            f"<a href=\"{LINK}\">Anmelden</a></td></tr></table></td><td></td></tr></table>"
            "</td></tr></table>"
            "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
            "</body></html>")
    md, fmt = render_body(html, "", "markdown")
    assert fmt == "markdown"
    assert md.startswith("# Bei Claude.ai anmelden"), md[:80]
    assert LINK in md
    import re as _re
    assert not any("|" in ln and _re.fullmatch(r"[\s|:\-]*", ln) for ln in md.splitlines())
    assert "| A | B |" in md and "| 1 | 2 |" in md
