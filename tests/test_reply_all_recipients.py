"""Reply-all keeps the original To/Cc roles on Exchange.

Both ``reply_email`` and ``create_reply_draft`` used to put the original
sender, To and Cc all into To. The draft tool then replaced Cc with only the
caller's extra addresses, so the people who were merely copied were promoted
to direct recipients. These tests read the serialized EWS item, which is what
the server would store or send.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exchangelib import Account, Mailbox, Message  # noqa: E402
from exchangelib.folders import Drafts, Root  # noqa: E402

from src.tools import email_tools, email_tools_draft  # noqa: E402
from tests.support import ADDRESS, offline_account  # noqa: E402

_T = "{http://schemas.microsoft.com/exchange/services/2006/types}"


class _Client:
    """Only what BaseTool touches on the Exchange reply paths."""

    mailbox_registry = None

    def __init__(self, account: Account):
        self.account = account
        self.config = SimpleNamespace(ews_email=account.primary_smtp_address)

    def get_account(self, target_mailbox=None):
        return self.account


def _original():
    """A message from the boss to us and a teammate, copying an observer.

    Our own address and the teammate reappear in other fields with other
    letter case, as real headers do.
    """
    return SimpleNamespace(
        id="ORIG-1",
        subject="Quarterly numbers",
        sender=Mailbox(email_address="boss@example.com"),
        to_recipients=[
            Mailbox(email_address=ADDRESS.upper()),
            Mailbox(email_address="teammate@example.com"),
        ],
        cc_recipients=[
            Mailbox(email_address="observer@example.com"),
            Mailbox(email_address="Teammate@Example.com"),
            Mailbox(email_address=ADDRESS),
        ],
        text_body="original body",
        body="original body",
        datetime_received=None,
        has_attachments=False,
        attachments=[],
    )


def _wire_recipients(xml: str) -> dict:
    root = etree.fromstring(xml.encode())
    return {
        field: [
            address.text
            for container in root.iter(_T + tag)
            for address in container.iter(_T + "EmailAddress")
        ]
        for field, tag in (
            ("to", "ToRecipients"), ("cc", "CcRecipients"), ("bcc", "BccRecipients"),
        )
    }


def _serialize(message) -> str:
    return etree.tostring(
        message.to_xml(version=message.account.version), encoding="unicode"
    )


@pytest.fixture()
def account(monkeypatch):
    account = offline_account()
    # Saving a draft names account.drafts; keep that lookup offline.
    account.__dict__["drafts"] = Drafts(root=Root(account=account))
    original = _original()
    lookup = lambda account_, message_id: original  # noqa: E731
    monkeypatch.setattr(email_tools, "find_message_for_account", lookup)
    monkeypatch.setattr(email_tools_draft, "find_message_for_account", lookup)
    return account


@pytest.fixture()
def wire(monkeypatch):
    """Serialized items handed to Exchange by send() or save()."""
    payloads: list = []

    def send(self, *args, **kwargs):
        payloads.append(_wire_recipients(_serialize(self)))

    def save(self, *args, **kwargs):
        payloads.append(_wire_recipients(_serialize(self)))
        self.id, self.changekey = "DRAFT-1", "CK"
        return self

    monkeypatch.setattr(Message, "send", send)
    monkeypatch.setattr(Message, "save", save)
    return payloads


def _reply(account, **kwargs):
    return asyncio.run(
        email_tools.ReplyEmailTool(_Client(account)).execute(
            message_id="ORIG-1", body="answering", **kwargs
        )
    )


def _reply_draft(account, **kwargs):
    return asyncio.run(
        email_tools_draft.CreateReplyDraftTool(_Client(account)).execute(
            message_id="ORIG-1", body="answering", **kwargs
        )
    )


# ------------------------------------------------------------- reply_email
def test_reply_all_sends_original_cc_as_cc(account, wire):
    response = _reply(account, reply_all=True)

    assert wire == [{
        "to": ["boss@example.com", "teammate@example.com"],
        "cc": ["observer@example.com"],
        "bcc": [],
    }]
    assert response["reply_to"] == ["boss@example.com", "teammate@example.com"]
    assert response["cc"] == ["observer@example.com"]


def test_reply_all_preview_reports_the_same_to_and_cc(account, wire):
    response = _reply(account, reply_all=True, dry_run=True)

    assert wire == []
    assert response["would_send_to"] == ["boss@example.com", "teammate@example.com"]
    assert response["would_cc"] == ["observer@example.com"]


def test_plain_reply_still_goes_to_the_sender_only(account, wire):
    _reply(account)

    assert wire == [{"to": ["boss@example.com"], "cc": [], "bcc": []}]


def test_reply_all_to_a_note_to_self_still_has_a_recipient(monkeypatch, wire):
    account = offline_account()
    note = SimpleNamespace(
        id="NOTE-1",
        subject="Reminder",
        sender=Mailbox(email_address=ADDRESS),
        to_recipients=[Mailbox(email_address=ADDRESS)],
        cc_recipients=[],
        text_body="", body="", datetime_received=None,
        has_attachments=False, attachments=[],
    )
    monkeypatch.setattr(email_tools, "find_message_for_account", lambda a, m: note)

    _reply(account, reply_all=True)

    assert wire == [{"to": [ADDRESS], "cc": [], "bcc": []}]


# ------------------------------------------------------ create_reply_draft
def test_reply_all_draft_keeps_original_cc_beside_the_callers(account, wire):
    response = _reply_draft(
        account,
        reply_all=True,
        cc=["extra@example.com", "OBSERVER@example.com", "boss@example.com"],
        bcc=["hidden@example.com", "teammate@example.com", "Extra@example.com"],
    )

    saved = {
        "to": ["boss@example.com", "teammate@example.com"],
        "cc": ["observer@example.com", "extra@example.com"],
        "bcc": ["hidden@example.com"],
    }
    assert wire == [saved]
    assert {key: response[key] for key in ("to", "cc", "bcc")} == saved


def test_reply_all_draft_without_extras_still_copies_the_original_cc(account, wire):
    response = _reply_draft(account, reply_all=True)

    assert wire == [{
        "to": ["boss@example.com", "teammate@example.com"],
        "cc": ["observer@example.com"],
        "bcc": [],
    }]
    assert response["cc"] == ["observer@example.com"]


def test_plain_reply_draft_still_goes_to_the_sender_plus_callers_copies(account, wire):
    response = _reply_draft(
        account, cc=["extra@example.com", "boss@example.com"], bcc=["hidden@example.com"],
    )

    assert wire == [{
        "to": ["boss@example.com"],
        "cc": ["extra@example.com"],
        "bcc": ["hidden@example.com"],
    }]
    assert response["cc"] == ["extra@example.com"]
    assert response["bcc"] == ["hidden@example.com"]
