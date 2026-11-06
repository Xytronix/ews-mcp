"""Outbound identity/tracking and subject filtering.

Two live defects are pinned here:

1. SmarterMail accepted an EWS ``CreateItem`` with no ``message:From`` and
   later emitted RFC822 mail without a From header, which bounced at the
   recipient. ``Message.send()`` returns no server-side id either, so the
   tools report acceptance, never delivery, and an outbound message carries a
   tracking internet header when ``OUTBOUND_TRACKING_HEADER`` names one.
   Tracking is off by default: no header on any provider's wire and
   ``tracking_id: None`` in every send result.
2. SmarterMail accepts an EWS ``subject__contains`` restriction and then
   returns the whole folder anyway, so ``search_emails`` answered with
   unrelated mail and a bogus ``total_available``.
"""
from __future__ import annotations

import ast
import asyncio
import os
import re
import subprocess
import sys
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import pytest
from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from exchangelib import Account, Mailbox, Message  # noqa: E402

from src.mail_backends.accounts import MailAccount  # noqa: E402
from src.mail_backends.base import OutgoingMessage  # noqa: E402
from src.mail_backends.ews_backend import EWSBackend  # noqa: E402
from src.mail_backends.graph_backend import GraphBackend  # noqa: E402
from src.mail_backends.imap_backend import IMAPBackend  # noqa: E402
from src.tools import email_tools  # noqa: E402
from src.tools import mailbox_tools  # noqa: E402
from src.utils import TRACKING_HEADER_ENV, configure_outbound_tracking  # noqa: E402
from tests.support import ADDRESS, offline_account  # noqa: E402

TEST_HEADER = "X-Test-Tracking-ID"


def _plain_hex(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


class _StubEWSClient:
    """Only what BaseTool touches on the Exchange send path."""

    mailbox_registry = None

    def __init__(self, account: Account):
        self.account = account
        self.config = SimpleNamespace(ews_email=account.primary_smtp_address)

    def get_account(self, target_mailbox=None):
        return self.account


@pytest.fixture()
def tracking_on():
    """Name a tracking header for one test, then restore the setting."""
    previous = configure_outbound_tracking(TEST_HEADER)
    yield TEST_HEADER
    configure_outbound_tracking(previous)


@pytest.fixture()
def tracking_off():
    """Run one test with tracking off, whatever the environment says."""
    previous = configure_outbound_tracking(None)
    yield
    configure_outbound_tracking(previous)


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
    # The EWS mail backend files its copy through send_and_save().
    monkeypatch.setattr(Message, "send_and_save", fake_send, raising=True)
    return payloads


# ------------------------------------------------------ tracking setting
@pytest.mark.parametrize(
    "value, expected",
    [(None, None), ("", None), (" X-Env-Tracking-ID ", "X-Env-Tracking-ID")],
)
def test_the_environment_alone_switches_tracking(value, expected):
    env = {key: val for key, val in os.environ.items() if key != TRACKING_HEADER_ENV}
    if value is not None:
        env[TRACKING_HEADER_ENV] = value
    probe = (
        "from src.utils import new_outbound_tracking_id, outbound_tracking_header;"
        "print(repr((outbound_tracking_header(), new_outbound_tracking_id())))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, env=env,
        capture_output=True, text=True, check=True,
    )
    header, tracking_id = ast.literal_eval(completed.stdout.strip())

    assert header == expected
    if expected is None:
        assert tracking_id is None
    else:
        assert _plain_hex(tracking_id)


# ------------------------------------------------------- EWS outbound wire
def test_send_puts_the_sender_and_tracking_header_on_the_wire(sent_payloads, tracking_on):
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
    assert TEST_HEADER in xml
    assert _plain_hex(response["tracking_id"])
    assert response["tracking_id"] in xml
    # SmarterMail rejects CreateItem when the client sets the message id.
    assert "InternetMessageId" not in xml


def test_send_reports_acceptance_and_never_claims_delivery(sent_payloads, tracking_on):
    tool = email_tools.SendEmailTool(_StubEWSClient(offline_account()))

    response = asyncio.run(
        tool.execute(to=["someone@example.com"], subject="probe", body="plain text")
    )

    assert response["accepted_by_mail_server"] is True
    assert response["delivery_confirmed"] is False
    assert _plain_hex(response["tracking_id"])
    assert "not yet confirmed" in response["message"]


def test_dry_run_neither_sends_nor_invents_a_tracking_id(sent_payloads, tracking_on):
    tool = email_tools.SendEmailTool(_StubEWSClient(offline_account()))

    response = asyncio.run(
        tool.execute(
            to=["someone@example.com"], subject="probe", body="text", dry_run=True
        )
    )

    assert sent_payloads == []
    assert response["sent"] is False
    assert "tracking_id" not in response


def _reply_and_forward(monkeypatch):
    """Reply to and forward one stub original through the Exchange tools."""
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

    return reply, forward


def test_reply_and_forward_also_identify_the_sender(monkeypatch, sent_payloads, tracking_on):
    reply, forward = _reply_and_forward(monkeypatch)

    assert len(sent_payloads) == 2
    for xml, response in zip(sent_payloads, (reply, forward)):
        assert "<t:From>" in xml
        assert ADDRESS in xml
        assert TEST_HEADER in xml
        assert _plain_hex(response["tracking_id"])
        assert response["tracking_id"] in xml
        assert response["accepted_by_mail_server"] is True
        assert response["delivery_confirmed"] is False


def test_exchange_tools_put_no_header_on_the_wire_while_tracking_is_off(
    monkeypatch, sent_payloads, tracking_off,
):
    send = asyncio.run(
        email_tools.SendEmailTool(_StubEWSClient(offline_account())).execute(
            to=["someone@example.com"], subject="probe", body="<p>hi</p>"
        )
    )
    reply, forward = _reply_and_forward(monkeypatch)

    assert len(sent_payloads) == 3
    for xml, response in zip(sent_payloads, (send, reply, forward)):
        assert "<t:From>" in xml
        # Message(outbound_tracking_id=None) must serialize no property at all.
        assert "ExtendedProperty" not in xml
        assert "InternetHeaders" not in xml
        assert response["tracking_id"] is None
        assert response["accepted_by_mail_server"] is True


def _ews_backend():
    account = offline_account()
    mail_account = MailAccount(email=account.primary_smtp_address, provider="ews", settings={})
    return EWSBackend(mail_account, _StubEWSClient(account))


def test_ews_backend_publishes_the_configured_header(sent_payloads, tracking_on):
    result = _ews_backend().send_message(
        OutgoingMessage(to=["peer@example.com"], subject="probe", body="hello")
    )

    assert _plain_hex(result["tracking_id"])
    assert len(sent_payloads) == 1
    assert TEST_HEADER in sent_payloads[0]
    assert result["tracking_id"] in sent_payloads[0]


def test_ews_backend_drops_even_a_caller_id_while_tracking_is_off(sent_payloads, tracking_off):
    result = _ews_backend().send_message(
        OutgoingMessage(
            to=["peer@example.com"], subject="probe", body="hello", tracking_id="caller-id",
        )
    )

    assert result["tracking_id"] is None
    assert len(sent_payloads) == 1
    assert "ExtendedProperty" not in sent_payloads[0]
    assert "caller-id" not in sent_payloads[0]


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
def _imap_backend():
    backend = IMAPBackend(MailAccount(
        email="user@icloud.com",
        provider="icloud",
        password="app-specific",
        settings={"smtp_host": "smtp.mail.me.com"},
    ))
    source = EmailMessage()
    source["From"] = "peer@example.com"
    source["To"] = "user@icloud.com"
    source["Subject"] = "Original"
    source.set_content("Original body")
    sent: list = []
    backend._fetch_full = lambda message_id, folder, writable=False: (
        "7", "42", {"raw": source.as_bytes()}
    )
    backend._smtp_send = lambda outgoing, envelope: sent.append(outgoing) or {}
    backend._append_to_sent = lambda outgoing: "Sent Messages"
    return backend, sent


def _custom_headers(outgoing) -> list:
    return [name for name in outgoing.keys() if name.lower().startswith("x-")]


def test_imap_send_and_forward_publish_the_configured_header(tracking_on):
    backend, sent = _imap_backend()

    fresh = backend.send_message(
        OutgoingMessage(to=["peer@example.com"], subject="probe", body="hello", tracking_id="imap-id")
    )
    forward = backend.forward_message(
        "7:42", OutgoingMessage(to=["third@example.com"], body="fyi", tracking_id="imap-fwd")
    )

    assert fresh["tracking_id"] == "imap-id"
    assert forward["tracking_id"] == "imap-fwd"
    # add_alternative() restructures the payload; the header must stay on top.
    assert sent[0][TEST_HEADER] == "imap-id"
    assert f"{TEST_HEADER}: imap-id" in sent[0].as_string()
    assert sent[1][TEST_HEADER] == "imap-fwd"


def test_imap_puts_no_header_on_the_mime_while_tracking_is_off(tracking_off):
    backend, sent = _imap_backend()

    routed = asyncio.run(
        mailbox_tools.SendEmailTool(_StubEWSClient(offline_account())).execute_routed(
            backend.account, backend, to=["peer@example.com"], subject="probe", body="hello"
        )
    )
    # A caller-supplied ID has no header to travel in either.
    fresh = backend.send_message(
        OutgoingMessage(to=["peer@example.com"], subject="probe", body="hello", tracking_id="caller-id")
    )
    forward = backend.forward_message(
        "7:42", OutgoingMessage(to=["third@example.com"], body="fyi", tracking_id="caller-id")
    )

    for result in (routed, fresh, forward):
        assert result["tracking_id"] is None
    assert len(sent) == 3
    for outgoing in sent:
        assert _custom_headers(outgoing) == []
        assert "caller-id" not in outgoing.as_string()


GRAPH_ACCOUNT = MailAccount(
    email="user@outlook.de",
    provider="outlook",
    settings={"client_id": "c", "client_secret": "s", "refresh_token": "r"},
)


def _graph_backend():
    backend = GraphBackend(GRAPH_ACCOUNT)
    calls: list = []

    def request(method, path, **kwargs):
        calls.append((path, kwargs))
        if path.endswith("/createReply"):
            return {"id": "draft", "body": {"contentType": "html", "content": "Original"}}
        if path.endswith("/messages"):
            return {"id": "new-draft"}
        if path.endswith("/attachments"):
            return {"value": []}
        return {}

    backend._request = request
    return backend, calls


def test_graph_send_publishes_the_header_and_admits_it_cannot_on_replies(tracking_on):
    backend, calls = _graph_backend()

    fresh = backend.send_message(
        OutgoingMessage(to=["peer@example.com"], subject="probe", tracking_id="graph-id")
    )
    assert fresh["tracking_id"] == "graph-id"
    assert calls[0][1]["json"]["message"]["internetMessageHeaders"] == [
        {"name": TEST_HEADER, "value": "graph-id"}
    ]

    reply = backend.send_message(
        OutgoingMessage(
            to=[], body="answer", reply_to_message_id="M1", tracking_id="graph-id"
        )
    )
    # Graph composes replies server-side and rejects custom headers there.
    assert reply["tracking_id"] is None
    assert "internetMessageHeaders" not in calls[1][1]["json"]["message"]


def test_graph_sends_no_header_while_tracking_is_off(tracking_off):
    backend, calls = _graph_backend()

    routed = asyncio.run(
        mailbox_tools.SendEmailTool(_StubEWSClient(offline_account())).execute_routed(
            GRAPH_ACCOUNT, backend, to=["peer@example.com"], subject="probe", body="hello"
        )
    )
    # A caller-supplied ID has no header to travel in either.
    fresh = backend.send_message(
        OutgoingMessage(to=["peer@example.com"], subject="probe", tracking_id="caller-id")
    )
    draft = backend.create_draft(
        OutgoingMessage(to=["peer@example.com"], subject="probe", tracking_id="caller-id")
    )

    for result in (routed, fresh, draft):
        assert result["tracking_id"] is None
    posted = [kwargs["json"] for _path, kwargs in calls if kwargs.get("json")]
    assert len(posted) == 3
    for payload in posted:
        assert "internetMessageHeaders" not in payload.get("message", payload)


@pytest.mark.parametrize("setting", ["tracking_on", "tracking_off"])
def test_routed_send_and_reply_speak_the_same_acceptance_contract(request, setting):
    request.getfixturevalue(setting)

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
        if setting == "tracking_on":
            assert _plain_hex(response["tracking_id"])
        else:
            assert response["tracking_id"] is None
        assert "not yet confirmed" in response["message"]
