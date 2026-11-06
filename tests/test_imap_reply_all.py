"""IMAP reply-all keeps the original Cc recipients in Cc.

The SMTP send path folded the original Cc into To, so everyone who was only
copied on a thread became a primary recipient of the reply. The reply draft
path already split them; both now agree. Only the IMAP fetch and SMTP
submission are faked.
"""
from __future__ import annotations

import asyncio
import email
import email.policy
import email.utils
import sys
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.mail_backends.accounts import MailAccount  # noqa: E402
from src.mail_backends.base import OutgoingMessage  # noqa: E402
from src.mail_backends.imap_backend import IMAPBackend  # noqa: E402
from src.tools.mailbox_tools import ReplyEmailTool  # noqa: E402

OWN = "me@example.com"


def _original() -> bytes:
    original = EmailMessage()
    original["From"] = "Author <author@example.com>"
    # Our own address in another case, and a To address repeated in Cc.
    original["To"] = "Me <ME@EXAMPLE.COM>, peer@example.com"
    original["Cc"] = "copied@example.com, PEER@example.com, me@example.com"
    original["Subject"] = "Plan"
    original["Message-ID"] = "<plan@example.com>"
    original.set_content("Original body")
    return original.as_bytes()


def _backend(monkeypatch):
    backend = IMAPBackend(MailAccount(
        email=OWN, provider="imap", password="unused",
        settings={"smtp_host": "smtp.example.com"},
    ))
    submissions: list = []

    def fetch(message_id, folder, writable=False):
        return "7", "42", {"raw": _original(), "flags": []}

    def smtp(outgoing, envelope):
        wire = outgoing.as_bytes(policy=email.policy.SMTP)
        submissions.append(
            (email.message_from_bytes(wire, policy=email.policy.default), list(envelope))
        )
        return {}

    monkeypatch.setattr(backend, "_fetch_full", fetch)
    monkeypatch.setattr(backend, "_smtp_send", smtp)
    monkeypatch.setattr(backend, "_append_to_sent", lambda outgoing: "Sent")
    return backend, submissions


def _addresses(header) -> list:
    return [address for _name, address in email.utils.getaddresses([str(header or "")])]


def test_reply_all_keeps_the_original_cc_in_cc(monkeypatch):
    backend, submissions = _backend(monkeypatch)
    client = SimpleNamespace(config=SimpleNamespace(ews_email=""), mailbox_registry=None)

    response = asyncio.run(ReplyEmailTool(client).execute_routed(
        backend.account, backend, message_id="7:42", body="Agreed", reply_all=True,
    ))

    assert len(submissions) == 1
    wire, envelope = submissions[0]
    assert _addresses(wire["To"]) == ["author@example.com", "peer@example.com"]
    assert _addresses(wire["Cc"]) == ["copied@example.com"]
    assert envelope == ["author@example.com", "peer@example.com", "copied@example.com"]
    assert response["to"] == ["author@example.com", "peer@example.com"]
    assert response["cc"] == ["copied@example.com"]


def test_reply_all_merges_caller_cc_without_repeating_anyone(monkeypatch):
    backend, submissions = _backend(monkeypatch)

    result = backend.send_message(OutgoingMessage(
        to=[], body="Agreed", reply_to_message_id="7:42", reply_all=True,
        cc=["extra@example.com", "Author@example.com", "COPIED@example.com"],
    ))

    wire, envelope = submissions[0]
    assert _addresses(wire["To"]) == ["author@example.com", "peer@example.com"]
    assert _addresses(wire["Cc"]) == ["extra@example.com", "COPIED@example.com"]
    assert result["to"] == ["author@example.com", "peer@example.com"]
    assert result["cc"] == ["extra@example.com", "COPIED@example.com"]
    assert envelope == [
        "author@example.com", "peer@example.com", "extra@example.com", "COPIED@example.com",
    ]


def test_reply_all_leaves_blind_copies_out_of_the_visible_headers(monkeypatch):
    backend, submissions = _backend(monkeypatch)

    result = backend.send_message(OutgoingMessage(
        to=[], body="Agreed", reply_to_message_id="7:42", reply_all=True,
        bcc=["copied@example.com"],
    ))

    wire, envelope = submissions[0]
    assert _addresses(wire["To"]) == ["author@example.com", "peer@example.com"]
    assert wire["Cc"] is None
    assert b"copied@example.com" not in wire.as_bytes()
    assert envelope == ["author@example.com", "peer@example.com", "copied@example.com"]
    assert result["cc"] == []
