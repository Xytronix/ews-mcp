"""``reply_email`` adds the caller's Cc and Bcc on every provider.

Exchange merges them through ``reply_recipients`` exactly as
``create_reply_draft`` does. Graph adds them to the createReply/createReplyAll
draft, because recipients passed to createReply replace the ones Graph derives.
IMAP merges them for a plain reply as it already did for reply-all.
"""
from __future__ import annotations

import asyncio
import email
import email.policy
import email.utils
import json
import sys
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exchangelib import Mailbox, Message  # noqa: E402
from exchangelib.folders import Drafts, Root  # noqa: E402

from src.exceptions import ValidationError  # noqa: E402
from src.mail_backends.accounts import MailAccount  # noqa: E402
from src.mail_backends.graph_backend import GraphBackend  # noqa: E402
from src.mail_backends.imap_backend import IMAPBackend  # noqa: E402
from src.tools import email_tools, email_tools_draft  # noqa: E402
from src.tools.mailbox_tools import ReplyEmailTool  # noqa: E402
from tests.support import ADDRESS, offline_account  # noqa: E402

_T = "{http://schemas.microsoft.com/exchange/services/2006/types}"


# ================================================================ Exchange
class _Client:
    mailbox_registry = None

    def __init__(self, account):
        self.account = account
        self.config = SimpleNamespace(ews_email=account.primary_smtp_address)

    def get_account(self, target_mailbox=None):
        return self.account


def _original():
    """From the boss to us and a teammate, copying an observer."""
    return SimpleNamespace(
        id="ORIG-1",
        subject="Quarterly numbers",
        sender=Mailbox(email_address="boss@example.com"),
        to_recipients=[Mailbox(email_address=ADDRESS), Mailbox(email_address="teammate@example.com")],
        cc_recipients=[Mailbox(email_address="observer@example.com")],
        text_body="original body",
        body="original body",
        datetime_received=None,
        has_attachments=False,
        attachments=[],
    )


def _wire_recipients(message) -> dict:
    """To/Cc/Bcc addresses of the item as serialized for Exchange."""
    root = etree.fromstring(etree.tostring(message.to_xml(version=message.account.version)))
    return {
        field: [a.text for c in root.iter(_T + tag) for a in c.iter(_T + "EmailAddress")]
        for field, tag in (("to", "ToRecipients"), ("cc", "CcRecipients"), ("bcc", "BccRecipients"))
    }


@pytest.fixture()
def exchange(monkeypatch):
    """The recipients of every item handed to Exchange by send() or save()."""
    account = offline_account()
    # Saving a draft names account.drafts; keep that lookup offline.
    account.__dict__["drafts"] = Drafts(root=Root(account=account))
    lookup = lambda a, m: _original()  # noqa: E731
    monkeypatch.setattr(email_tools, "find_message_for_account", lookup)
    monkeypatch.setattr(email_tools_draft, "find_message_for_account", lookup)
    sent: list = []

    def send(self, *args, **kwargs):
        sent.append(_wire_recipients(self))

    def save(self, *args, **kwargs):
        sent.append(_wire_recipients(self))
        self.id, self.changekey = "DRAFT-1", "CK"
        return self

    monkeypatch.setattr(Message, "send", send)
    monkeypatch.setattr(Message, "save", save)
    return account, sent


def _exchange_reply(account, **kwargs):
    return asyncio.run(email_tools.ReplyEmailTool(_Client(account)).execute(
        message_id="ORIG-1", body="answering", **kwargs,
    ))


def test_exchange_reply_draft_reads_a_cc_string_as_addresses(exchange):
    """create_reply_draft used to iterate a cc string character by character."""
    account, saved = exchange

    response = asyncio.run(email_tools_draft.CreateReplyDraftTool(_Client(account)).execute(
        message_id="ORIG-1", body="answering",
        cc="extra@example.com; second@example.com", bcc="hidden@example.com",
    ))

    expected = {
        "to": ["boss@example.com"],
        "cc": ["extra@example.com", "second@example.com"],
        "bcc": ["hidden@example.com"],
    }
    assert saved == [expected]
    assert {key: response[key] for key in ("to", "cc", "bcc")} == expected


def test_exchange_reply_draft_refuses_a_non_address_bcc(exchange):
    account, saved = exchange

    with pytest.raises(ValidationError, match="bcc must contain"):
        asyncio.run(email_tools_draft.CreateReplyDraftTool(_Client(account)).execute(
            message_id="ORIG-1", body="answering", bcc=[{"address": "x@example.com"}],
        ))
    assert saved == []


def test_exchange_plain_reply_sends_the_callers_cc_and_bcc(exchange):
    account, sent = exchange

    response = _exchange_reply(
        account, cc=["extra@example.com", "BOSS@example.com"], bcc=["hidden@example.com"],
    )

    expected = {"to": ["boss@example.com"], "cc": ["extra@example.com"], "bcc": ["hidden@example.com"]}
    assert sent == [expected]
    assert response["reply_to"] == expected["to"]
    assert response["cc"] == expected["cc"]
    assert response["bcc"] == expected["bcc"]


def test_exchange_reply_all_keeps_original_cc_beside_the_callers(exchange):
    account, sent = exchange

    response = _exchange_reply(
        account, reply_all=True,
        cc=["extra@example.com", "OBSERVER@example.com", "boss@example.com"],
        bcc=["hidden@example.com", "teammate@example.com", "Extra@example.com"],
    )

    expected = {
        "to": ["boss@example.com", "teammate@example.com"],
        "cc": ["observer@example.com", "extra@example.com"],
        "bcc": ["hidden@example.com"],
    }
    assert sent == [expected]
    assert (response["reply_to"], response["cc"], response["bcc"]) == (
        expected["to"], expected["cc"], expected["bcc"],
    )


def test_exchange_dry_run_previews_cc_and_bcc_without_sending(exchange):
    account, sent = exchange

    response = _exchange_reply(
        account, dry_run=True, cc="extra@example.com; second@example.com",
        bcc=["hidden@example.com"],
    )

    assert sent == []
    assert response["would_send_to"] == ["boss@example.com"]
    assert response["would_cc"] == ["extra@example.com", "second@example.com"]
    assert response["would_bcc"] == ["hidden@example.com"]


def test_exchange_reply_refuses_a_non_address_cc(exchange):
    account, sent = exchange

    with pytest.raises(ValidationError, match="cc must contain"):
        _exchange_reply(account, cc=[42])
    assert sent == []


# =================================================================== Graph
GRAPH = MailAccount(
    email="user@outlook.com", provider="outlook",
    settings={"client_id": "cid", "refresh_token": "rt"},
)
_STUB_CLIENT = SimpleNamespace(config=None, mailbox_registry=None)


def _graph(handler):
    backend = GraphBackend(GRAPH)
    backend._access_token = "token"
    backend._token_expires_at = 1 << 40
    backend._client = httpx.Client(transport=httpx.MockTransport(handler))
    return backend


def _people(*addresses):
    return [{"emailAddress": {"address": a}} for a in addresses]


class _GraphServer:
    """createReply(All) derives the recipients; PATCH and send are recorded."""

    def __init__(self, report_recipients=True):
        self.report_recipients = report_recipients
        self.calls: list = []
        self.draft = {
            "toRecipients": _people("boss@example.com", "teammate@example.com"),
            "ccRecipients": _people("observer@example.com"),
            "bccRecipients": [],
        }

    def __call__(self, request):
        payload = json.loads(request.content) if request.content else None
        path = request.url.path
        self.calls.append((request.method, path.rsplit("/", 1)[-1], payload, dict(request.url.params)))
        if path.endswith(("/createReply", "/createReplyAll")):
            body = {"id": "DRAFT", "body": {"contentType": "html", "content": "<html><body>Original</body></html>"}}
            if self.report_recipients:
                body.update(self.draft)
            return httpx.Response(201, json=body)
        if request.method == "GET" and path.endswith("/attachments"):
            return httpx.Response(200, json={"value": []})
        if request.method == "GET":
            return httpx.Response(200, json=self.draft)
        if request.method == "PATCH":
            self.draft.update({k: v for k, v in payload.items() if k.endswith("Recipients")})
            return httpx.Response(200, json={"id": "DRAFT"})
        assert request.method == "POST" and path.endswith("/send"), path
        return httpx.Response(202)


def _graph_reply(server, **kwargs):
    backend = _graph(server)
    try:
        return asyncio.run(ReplyEmailTool(_STUB_CLIENT).execute_routed(
            GRAPH, backend, message_id="ORIG", body="answering", **kwargs,
        ))
    finally:
        backend.close()


def _addresses(recipients):
    return [r["emailAddress"]["address"] for r in recipients]


def test_graph_reply_all_adds_the_callers_copies_to_the_derived_ones():
    server = _GraphServer()

    response = _graph_reply(
        server, reply_all=True,
        cc=["extra@example.com", "OBSERVER@example.com", "Boss@example.com"],
        bcc=["hidden@example.com", "EXTRA@example.com"],
    )

    create = [c for c in server.calls if c[1] == "createReplyAll"]
    # Recipients given to createReplyAll would replace the derived ones.
    assert create and "ccRecipients" not in create[0][2]["message"]
    assert "bccRecipients" not in create[0][2]["message"]
    patch = [c[2] for c in server.calls if c[0] == "PATCH"]
    assert len(patch) == 1
    assert _addresses(patch[0]["ccRecipients"]) == ["observer@example.com", "extra@example.com"]
    assert _addresses(patch[0]["bccRecipients"]) == ["hidden@example.com"]
    assert "Original" in patch[0]["body"]["content"]
    assert server.calls[-1][:2] == ("POST", "send")
    assert response["to"] == ["boss@example.com", "teammate@example.com"]
    assert response["cc"] == ["observer@example.com", "extra@example.com"]
    assert response["bcc"] == ["hidden@example.com"]


def test_graph_plain_reply_reads_the_draft_recipients_when_not_returned():
    server = _GraphServer(report_recipients=False)
    server.draft = {"toRecipients": _people("boss@example.com"), "ccRecipients": [], "bccRecipients": []}

    response = _graph_reply(server, cc=["extra@example.com"], bcc=["hidden@example.com"])

    gets = [c for c in server.calls if c[0] == "GET" and c[1] == "DRAFT"]
    assert gets and gets[0][3]["$select"] == "toRecipients,ccRecipients,bccRecipients"
    assert [c[1] for c in server.calls if c[0] == "POST"][0] == "createReply"
    assert response["to"] == ["boss@example.com"]
    assert response["cc"] == ["extra@example.com"]
    assert response["bcc"] == ["hidden@example.com"]
    assert _addresses(server.draft["ccRecipients"]) == ["extra@example.com"]


def test_graph_reply_without_copies_leaves_the_derived_recipients_alone():
    server = _GraphServer()

    response = _graph_reply(server, reply_all=True)

    patch = [c[2] for c in server.calls if c[0] == "PATCH"]
    assert [set(p) for p in patch] == [{"body"}]
    assert response["cc"] == ["observer@example.com"]
    assert response["bcc"] == []


def test_routed_dry_run_previews_the_callers_copies_without_calling_the_provider():
    server = _GraphServer()

    response = _graph_reply(server, dry_run=True, cc=["extra@example.com"], bcc="hidden@example.com")

    assert server.calls == []
    assert response["sent"] is False
    assert response["would_cc"] == ["extra@example.com"]
    assert response["would_bcc"] == ["hidden@example.com"]
    assert response["message_checked"] is False


# ==================================================================== IMAP
OWN = "me@example.com"


def _imap_original() -> bytes:
    original = EmailMessage()
    original["From"] = "Author <author@example.com>"
    original["To"] = "Me <me@example.com>, peer@example.com"
    original["Cc"] = "copied@example.com"
    original["Subject"] = "Plan"
    original["Message-ID"] = "<plan@example.com>"
    original.set_content("Original body")
    return original.as_bytes()


@pytest.fixture()
def imap(monkeypatch):
    backend = IMAPBackend(MailAccount(
        email=OWN, provider="imap", password="unused",
        settings={"smtp_host": "smtp.example.com"},
    ))
    submissions: list = []

    def smtp(outgoing, envelope):
        wire = outgoing.as_bytes(policy=email.policy.SMTP)
        submissions.append((email.message_from_bytes(wire, policy=email.policy.default), list(envelope)))
        return {}

    monkeypatch.setattr(backend, "_fetch_full", lambda *a, **k: ("7", "42", {"raw": _imap_original(), "flags": []}))
    monkeypatch.setattr(backend, "_smtp_send", smtp)
    monkeypatch.setattr(backend, "_append_to_sent", lambda outgoing: "Sent")
    return backend, submissions


def _header(value) -> list:
    return [address for _name, address in email.utils.getaddresses([str(value or "")])]


def test_imap_plain_reply_sends_the_callers_cc_and_blind_bcc(imap):
    backend, submissions = imap

    response = asyncio.run(ReplyEmailTool(_STUB_CLIENT).execute_routed(
        backend.account, backend, message_id="7:42", body="Agreed",
        cc=["extra@example.com", "AUTHOR@example.com", "Extra@example.com"],
        bcc=["hidden@example.com", "HIDDEN@example.com"],
    ))

    wire, envelope = submissions[0]
    assert _header(wire["To"]) == ["author@example.com"]
    assert _header(wire["Cc"]) == ["extra@example.com"]
    assert wire["Bcc"] is None
    assert b"hidden@example.com" not in wire.as_bytes()
    assert envelope == ["author@example.com", "extra@example.com", "hidden@example.com"]
    assert response["to"] == ["author@example.com"]
    assert response["cc"] == ["extra@example.com"]
    assert response["bcc"] == ["hidden@example.com"]


def test_imap_reply_all_adds_the_callers_bcc(imap):
    backend, submissions = imap

    response = asyncio.run(ReplyEmailTool(_STUB_CLIENT).execute_routed(
        backend.account, backend, message_id="7:42", body="Agreed", reply_all=True,
        cc=["extra@example.com"], bcc=["hidden@example.com"],
    ))

    wire, envelope = submissions[0]
    assert _header(wire["To"]) == ["author@example.com", "peer@example.com"]
    assert _header(wire["Cc"]) == ["extra@example.com", "copied@example.com"]
    assert envelope[-1] == "hidden@example.com"
    assert response["bcc"] == ["hidden@example.com"]
