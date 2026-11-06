"""Forwarding preserves MIME content while replacing the transport envelope."""
from __future__ import annotations

import asyncio
import base64
import email
import email.policy
import sys
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.mail_backends.accounts import MailAccount
from src.mail_backends.base import MailBackendError, OutgoingMessage
from src.mail_backends.imap_backend import IMAPBackend
from src.mail_backends.registry import MailboxRegistry
from src.tools.mailbox_tools import ForwardEmailTool


def original_message():
    original = EmailMessage()
    original["From"] = 'Original Author <author@example.net>'
    original["To"] = "old-to@example.net"
    original["Cc"] = "old-cc@example.net"
    original["Bcc"] = "source-secret@example.net"
    original["Resent-Bcc"] = "source-resent-secret@example.net"
    original["Subject"] = "Original report"
    original["Date"] = "Tue, 15 Sep 2026 10:00:00 +0000"
    original["Message-ID"] = "<source@example.net>"
    original.set_content("Original plain body")
    original.add_alternative('<p>Original HTML body <img src="cid:original-logo"></p>', subtype="html")
    original.get_payload()[1].add_related(
        b"original-image", maintype="image", subtype="png",
        cid="<original-logo>", disposition="inline", filename="original.png",
    )
    original.add_attachment(b"%PDF original", maintype="application", subtype="pdf", filename="original.pdf")
    nested = EmailMessage()
    nested["From"] = "nested-author@example.net"
    nested["Subject"] = "Attached message"
    nested.set_content("Nested body, not the forward body")
    nested.add_attachment(b"%PDF nested", maintype="application", subtype="pdf", filename="nested.pdf")
    original.add_attachment(nested, filename="original.eml")
    return original


def backend_with_source(monkeypatch, raw):
    backend = IMAPBackend(MailAccount(email="sender@icloud.com", provider="icloud", settings={}))
    seen = {"fetches": [], "smtp": [], "sent": []}

    def fetch(message_id, folder, writable=False):
        seen["fetches"].append((message_id, folder, writable))
        return "7", "42", {"raw": raw}

    def smtp(outgoing, envelope):
        wire = outgoing if isinstance(outgoing, bytes) else outgoing.as_bytes(policy=email.policy.SMTP)
        seen["smtp"].append((wire, list(envelope)))
        return {}

    def sent(outgoing):
        seen["sent"].append(outgoing)
        return "Sent Messages"

    monkeypatch.setattr(backend, "_fetch_full", fetch)
    monkeypatch.setattr(backend, "_smtp_send", smtp)
    monkeypatch.setattr(backend, "_append_to_sent", sent)
    return backend, seen


def test_forward_preserves_original_mime_and_adds_files_without_private_envelope(monkeypatch):
    original = original_message()
    raw = original.as_bytes()
    backend, seen = backend_with_source(monkeypatch, raw)
    nested_new = EmailMessage()
    nested_new["Subject"] = "New attached message"
    nested_new.set_content("New nested body")
    client = SimpleNamespace(config=SimpleNamespace(
        ews_email="", mail_account_rows=[
            {"email": backend.email, "provider": "icloud", "password": "test-only"},
        ],
    ))
    monkeypatch.setattr("src.mail_backends.registry.accounts_from_env", lambda: [])
    client.mailbox_registry = MailboxRegistry(client.config, client)
    client.mailbox_registry._backends[backend.email] = backend
    result = asyncio.run(ForwardEmailTool(client).execute(
        target_mailbox=backend.email, message_id="7:42", folder="archive",
        to=["new-to@example.net"], cc=["new-cc@example.net"], bcc=["new-hidden@example.net"],
        body='<p><strong>Review this</strong><img src="cid:new-logo"></p>', body_format="html",
        files=[
            {"file_name": "new.pdf", "file_content": base64.b64encode(b"%PDF new").decode(), "content_type": "application/pdf"},
            {"file_name": "new.png", "file_content": base64.b64encode(b"new-image").decode(),
             "content_type": "image/png", "is_inline": True, "content_id": "new-logo"},
            {"file_name": "new.eml", "file_content": base64.b64encode(nested_new.as_bytes()).decode(), "content_type": "message/rfc822"},
        ],
    ))
    assert result["success"] is True

    assert seen["fetches"] == [("7:42", "archive", False)]
    assert len(seen["smtp"]) == 1
    wire, envelope = seen["smtp"][0]
    assert envelope == ["new-to@example.net", "new-cc@example.net", "new-hidden@example.net"]
    outgoing = email.message_from_bytes(wire, policy=email.policy.default)
    assert outgoing["From"] == "sender@icloud.com"
    assert outgoing["To"] == "new-to@example.net"
    assert outgoing["Cc"] == "new-cc@example.net"
    assert outgoing["Bcc"] is None and outgoing["Resent-Bcc"] is None
    assert b"source-secret" not in wire and b"source-resent-secret" not in wire
    assert b"new-hidden" not in wire
    assert outgoing["Message-ID"] and outgoing["Message-ID"] != original["Message-ID"]
    assert outgoing["Subject"] == "Fwd: Original report"
    html = outgoing.get_body(preferencelist=("html",)).get_content()
    plain = outgoing.get_body(preferencelist=("plain",)).get_content()
    assert "<strong>Review this</strong>" in html
    assert "Original HTML body" in html and 'src="cid:original-logo"' in html
    assert "Original plain body" in plain
    assert "Original Author" in html and "author@example.net" in html
    assert "author@example.net" in plain
    files = {part.get_filename(): part for part in outgoing.walk() if part.get_filename()}
    for name, content in {"original.pdf": b"%PDF original", "new.pdf": b"%PDF new", "original.png": b"original-image", "new.png": b"new-image", "nested.pdf": b"%PDF nested"}.items():
        assert files[name].get_payload(decode=True) == content
    assert files["original.png"]["Content-ID"] == "<original-logo>"
    assert files["new.png"]["Content-ID"] == "<new-logo>"
    assert files["original.png"].get_content_disposition() == "inline"
    assert files["new.png"].get_content_disposition() == "inline"
    nested = files["original.eml"].get_payload()[0]
    assert nested["From"] == "nested-author@example.net"
    assert nested.get_body(preferencelist=("plain",)).get_content().strip() == "Nested body, not the forward body"
    assert files["new.eml"].get_content_type() == "message/rfc822"
    assert files["new.eml"].get_payload()[0].get_content().strip() == "New nested body"
    assert original.as_bytes() == raw
    assert result["accepted_by_mail_server"] is True
    assert result["delivery_confirmed"] is False
    assert result["saved_to_sent"] == "Sent Messages"


def test_forward_plain_original_keeps_html_prelude_and_author(monkeypatch):
    original = EmailMessage()
    original["From"] = "author@example.net"
    original["Subject"] = "Fwd: already forwarded"
    original.set_content("Plain original <not-html>")
    backend, seen = backend_with_source(monkeypatch, original.as_bytes())
    backend.forward_message("42", OutgoingMessage(to=["to@example.net"], body="<b>HTML note</b>", body_format="html"))
    outgoing = email.message_from_bytes(seen["smtp"][0][0], policy=email.policy.default)
    html = outgoing.get_body(preferencelist=("html",)).get_content()
    assert "<b>HTML note</b>" in html
    assert "Plain original &lt;not-html&gt;" in html
    assert "author@example.net" in html
    assert outgoing["Subject"] == "Fwd: already forwarded"


def test_forward_lookup_failure_never_submits(monkeypatch):
    backend, seen = backend_with_source(monkeypatch, b"")

    def missing(*args, **kwargs):
        raise MailBackendError("message not found")

    monkeypatch.setattr(backend, "_fetch_full", missing)
    with pytest.raises(MailBackendError, match="not found"):
        backend.forward_message("42", OutgoingMessage(to=["to@example.net"]))
    assert seen["smtp"] == [] and seen["sent"] == []


@pytest.mark.parametrize("failure", [None, MailBackendError("Sent unavailable")])
def test_forward_sent_copy_failure_still_reports_smtp_acceptance(monkeypatch, failure):
    backend, seen = backend_with_source(monkeypatch, original_message().as_bytes())

    def append_failure(outgoing):
        if failure:
            raise failure
        return None

    monkeypatch.setattr(backend, "_append_to_sent", append_failure)
    result = backend.forward_message("42", OutgoingMessage(to=["to@example.net"]))
    assert len(seen["smtp"]) == 1
    assert result["sent"] is True
    assert result["accepted_by_mail_server"] is True
    assert result["delivery_confirmed"] is False
    assert result["saved_to_sent"] is None
    assert any("Sent" in warning for warning in result["warnings"])
