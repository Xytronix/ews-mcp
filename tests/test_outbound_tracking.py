"""Outbound identity/tracking and subject filtering.

Two live defects are pinned here:

1. SmarterMail accepted an EWS ``CreateItem`` with no ``message:From`` and
   later emitted RFC822 mail without a From header, which bounced at the
   recipient. ``Message.send()`` returns no server-side id either, so every
   outbound message now carries an ``X-EWS-MCP-Tracking-ID`` internet header and
   the tools report acceptance, never delivery.
2. SmarterMail accepts an EWS ``subject__contains`` restriction and then
   returns the whole folder anyway, so ``search_emails`` answered with
   unrelated mail and a bogus ``total_available``.
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

from src.mail_backends.accounts import MailAccount  # noqa: E402
from src.mail_backends.base import OutgoingMessage  # noqa: E402
from src.mail_backends.graph_backend import GraphBackend  # noqa: E402
from src.mail_backends.imap_backend import IMAPBackend  # noqa: E402
from src.tools import email_tools  # noqa: E402
from src.tools import mailbox_tools  # noqa: E402
from src.utils import OUTBOUND_TRACKING_HEADER  # noqa: E402
from tests.support import ADDRESS, offline_account  # noqa: E402


class _StubEWSClient:
    """Only what BaseTool touches on the Exchange send path."""

    mailbox_registry = None

    def __init__(self, account: Account):
        self.account = account
        self.config = SimpleNamespace(ews_email=account.primary_smtp_address)

    def get_account(self, target_mailbox=None):
        return self.account


@pytest.fixture()
def sent_payloads(monkeypatch):
    """Capture the serialized EWS payload instead of sending anything."""
    payloads: list = []

    def fake_send(self, *args, **kwargs):
        payloads.append(
            etree.tostring(
                self.to_xml(version=self.account.version), encoding="unicode"
            )
        )
        return None

    monkeypatch.setattr(Message, "send", fake_send, raising=True)
    return payloads


# ------------------------------------------------------- EWS outbound wire
def test_send_puts_the_sender_and_tracking_header_on_the_wire(sent_payloads):
    account = offline_account()
    tool = email_tools.SendEmailTool(_StubEWSClient(account))

    response = asyncio.run(
        tool.execute(to=["someone@example.com"], subject="probe", body="<p>hi</p>")
    )

    assert len(sent_payloads) == 1
    xml = sent_payloads[0]
    # The bounce cause: no <t:From> element at all.
    assert "<t:From>" in xml
    assert ADDRESS in xml
    assert OUTBOUND_TRACKING_HEADER in xml
    assert response["tracking_id"] in xml
    # SmarterMail rejects CreateItem when the client sets the message id.
    assert "InternetMessageId" not in xml


def test_send_reports_acceptance_and_never_claims_delivery(sent_payloads):
    tool = email_tools.SendEmailTool(_StubEWSClient(offline_account()))

    response = asyncio.run(
        tool.execute(to=["someone@example.com"], subject="probe", body="plain text")
    )

    assert response["accepted_by_mail_server"] is True
    assert response["delivery_confirmed"] is False
    assert response["tracking_id"].startswith("ews-mcp-")
    assert "not yet confirmed" in response["message"]


def test_dry_run_neither_sends_nor_invents_a_tracking_id(sent_payloads):
    tool = email_tools.SendEmailTool(_StubEWSClient(offline_account()))

    response = asyncio.run(
        tool.execute(
            to=["someone@example.com"], subject="probe", body="text", dry_run=True
        )
    )

    assert sent_payloads == []
    assert response["sent"] is False
    assert "tracking_id" not in response


def test_reply_and_forward_also_identify_the_sender(monkeypatch, sent_payloads):
    account = offline_account()
    original = SimpleNamespace(
        id="ORIG-1",
        subject="Quarterly numbers",
        sender=Mailbox(email_address="peer@example.com"),
        to_recipients=[Mailbox(email_address=ADDRESS)],
        cc_recipients=[],
        text_body="original body",
        body="original body",
        datetime_received=None,
        has_attachments=False,
        attachments=[],
    )
    monkeypatch.setattr(
        email_tools,
        "find_message_for_account",
        lambda account_, message_id: original,
    )

    reply = asyncio.run(
        email_tools.ReplyEmailTool(_StubEWSClient(account)).execute(
            message_id="ORIG-1", body="answering"
        )
    )
    forward = asyncio.run(
        email_tools.ForwardEmailTool(_StubEWSClient(account)).execute(
            message_id="ORIG-1", to=["third@example.com"], body="fyi"
        )
    )

    assert len(sent_payloads) == 2
    for xml, response in zip(sent_payloads, (reply, forward)):
        assert "<t:From>" in xml
        assert ADDRESS in xml
        assert response["tracking_id"] in xml
        assert response["accepted_by_mail_server"] is True
        assert response["delivery_confirmed"] is False


# --------------------------------------------------- subject verification
class _LyingQuery:
    """A folder query that ignores every restriction it is handed.

    That is what SmarterMail does with EWS ``Contains``: it answers 200 with
    the whole folder.
    """

    def __init__(self, subjects, fail_at=None):
        self.items = [
            SimpleNamespace(
                id=f"ID-{i}",
                subject=s,
                sender=Mailbox(email_address="peer@example.com"),
                to_recipients=[],
                text_body="",
                datetime_received=None,
                is_read=True,
                has_attachments=False,
                importance="Normal",
                categories=[],
            )
            for i, s in enumerate(subjects)
        ]
        self.fail_at = fail_at

    # every restriction is accepted and discarded
    def filter(self, *a, **k):
        return self

    exclude = filter

    def order_by(self, *a, **k):
        return self

    def only(self, *fields):
        return self

    def all(self):
        return self

    def __getitem__(self, item):
        if self.fail_at is not None and item.start >= self.fail_at:
            raise RuntimeError("FindItem blew up mid-scan")
        return self.items[item]


def _search_tool(query):
    tool = email_tools.SearchEmailsTool(_StubEWSClient(offline_account()))
    return tool, query


@pytest.fixture()
def lying_folder(monkeypatch):
    def install(query):
        async def resolve(account, folder_identifier):
            return SimpleNamespace(name="inbox", all=lambda: query)

        monkeypatch.setattr(email_tools, "resolve_folder_for_account", resolve)
        return query

    return install


def test_quick_search_returns_only_subjects_that_really_match(lying_folder):
    query = lying_folder(
        _LyingQuery(["Invoice 42", "unrelated newsletter", "re: INVOICE 42 copy"])
    )
    tool, _ = _search_tool(query)

    response = asyncio.run(
        tool.execute(mode="quick", subject_contains="invoice 42", max_results=10)
    )

    assert [item["subject"] for item in response["items"]] == [
        "Invoice 42",
        "re: INVOICE 42 copy",
    ]
    assert response["count"] == 2
    assert response["total_available"] == 2


def test_quick_search_paginates_over_verified_matches_only(lying_folder):
    query = lying_folder(
        _LyingQuery(["junk", "hit one", "junk", "hit two", "hit three"])
    )
    tool, _ = _search_tool(query)

    response = asyncio.run(
        tool.execute(mode="quick", subject_contains="hit", max_results=1, offset=1)
    )

    assert [item["subject"] for item in response["items"]] == ["hit two"]
    assert response["total_available"] == 3
    assert response["next_offset"] == 2


def test_unfiltered_search_is_left_to_the_server(lying_folder):
    query = lying_folder(_LyingQuery(["a", "b", "c"]))
    tool, _ = _search_tool(query)

    response = asyncio.run(tool.execute(mode="quick", from_address="peer@example.com"))

    assert response["count"] == 3


def test_a_failed_scan_never_reports_a_partial_count_as_the_total():
    outcome = email_tools._paginate_query_matching(
        _LyingQuery(["hit a", "hit b", "hit c"], fail_at=2),
        matcher=email_tools._subject_search_matcher("hit"),
        max_results=10,
        offset=0,
        chunk_size=2,
        logger=email_tools.logging.getLogger("test"),
        folder_label="inbox",
    )

    assert outcome.total_available is None
    assert outcome.error_code
    assert "FindItem blew up mid-scan" in outcome.error_message


def test_verification_needs_the_subject_in_the_projection():
    assert email_tools._subject_search_matcher(None) is None
    assert email_tools._subject_search_matcher("") is None
    assert "subject" in email_tools._with_subject_search_field(("id",), "needle")
    assert email_tools._with_subject_search_field(("id",), None) == ("id",)


# ------------------------------------------------------- routed backends
def test_imap_send_publishes_the_header_on_the_outgoing_mime():
    account = MailAccount(
        email="user@icloud.com",
        provider="icloud",
        password="app-specific",
        settings={"smtp_host": "smtp.mail.me.com"},
    )
    backend = IMAPBackend(account)
    sent: list = []
    backend._smtp_send = lambda outgoing, envelope: sent.append(outgoing) or {}
    backend._append_to_sent = lambda outgoing: "Sent Messages"

    result = backend.send_message(
        OutgoingMessage(
            to=["peer@example.com"],
            subject="probe",
            body="hello",
            tracking_id="ews-mcp-imap",
        )
    )

    assert result["tracking_id"] == "ews-mcp-imap"
    # add_alternative() restructures the payload; the header must stay on top.
    assert sent[0][OUTBOUND_TRACKING_HEADER] == "ews-mcp-imap"
    assert f"{OUTBOUND_TRACKING_HEADER}: ews-mcp-imap" in sent[0].as_string()


def test_graph_send_publishes_the_header_and_admits_it_cannot_on_replies():
    account = MailAccount(
        email="user@outlook.de",
        provider="outlook",
        settings={"client_id": "c", "client_secret": "s", "refresh_token": "r"},
    )
    backend = GraphBackend(account)
    calls: list = []

    def request(method, path, **kwargs):
        calls.append((path, kwargs))
        if path.endswith("/createReply"):
            return {"id": "draft", "body": {"contentType": "html", "content": "Original"}}
        if path.endswith("/attachments"):
            return {"value": []}
        return {}

    backend._request = request

    fresh = backend.send_message(
        OutgoingMessage(to=["peer@example.com"], subject="probe", tracking_id="ews-mcp-graph")
    )
    assert fresh["tracking_id"] == "ews-mcp-graph"
    assert calls[0][1]["json"]["message"]["internetMessageHeaders"] == [
        {"name": OUTBOUND_TRACKING_HEADER, "value": "ews-mcp-graph"}
    ]

    reply = backend.send_message(
        OutgoingMessage(
            to=[], body="answer", reply_to_message_id="M1", tracking_id="ews-mcp-graph"
        )
    )
    # Graph composes replies server-side and rejects custom headers there.
    assert reply["tracking_id"] is None
    assert "internetMessageHeaders" not in calls[1][1]["json"]["message"]


def test_routed_send_and_reply_speak_the_same_acceptance_contract():
    class _Backend:
        def send_message(self, message):
            return {"sent": True, "backend": "imap", "tracking_id": message.tracking_id}

    account = MailAccount(email="user@icloud.com", provider="icloud", settings={})
    client = _StubEWSClient(offline_account())

    send = asyncio.run(
        mailbox_tools.SendEmailTool(client).execute_routed(
            account, _Backend(), to=["peer@example.com"], subject="s", body="b"
        )
    )
    reply = asyncio.run(
        mailbox_tools.ReplyEmailTool(client).execute_routed(
            account, _Backend(), message_id="M1", body="b"
        )
    )

    for response in (send, reply):
        assert response["accepted_by_mail_server"] is True
        assert response["delivery_confirmed"] is False
        assert response["tracking_id"].startswith("ews-mcp-")
        assert "not yet confirmed" in response["message"]
