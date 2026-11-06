"""Follow-up flags on the read side: a ``flag_status`` filter and output field.

``is_flagged`` could not select completed messages (false only excludes
Flagged), and no read path reported the flag at all. Exchange stores it as
PidTagFlagStatus (0x1090: absent = NotFlagged, 1 = Complete, 2 = Flagged),
Graph as ``flag.flagStatus``, IMAP as the ``\\Flagged`` system flag only.
"""
from __future__ import annotations

import asyncio
import email.utils
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exchangelib import Message  # noqa: E402
from exchangelib.folders import (  # noqa: E402
    DeletedItems, Drafts, Inbox, JunkEmail, Root, SentItems,
)
from exchangelib.services.common import EWSService  # noqa: E402

from src.exceptions import ValidationError  # noqa: E402
from src.mail_backends.accounts import MailAccount  # noqa: E402
from src.mail_backends.graph_backend import GraphBackend  # noqa: E402
from src.mail_backends.imap_backend import IMAPBackend  # noqa: E402
from src.tools import email_tools  # noqa: E402
from src.tools.mailbox_tools import (  # noqa: E402
    ReadEmailsTool as RoutedReadEmailsTool,
    SearchEmailsTool as RoutedSearchEmailsTool,
)
from tests.support import offline_account  # noqa: E402

_M = "{http://schemas.microsoft.com/exchange/services/2006/messages}"
_T = "{http://schemas.microsoft.com/exchange/services/2006/types}"
_NS = (
    'xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages" '
    'xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"'
)
# PidTagFlagStatus per stored message; None means the property is absent.
FLAGS = {"done": 1, "todo": 2, "never": None}


def _message_xml(ident):
    value = FLAGS[ident]
    flag = "" if value is None else (
        '<t:ExtendedProperty><t:ExtendedFieldURI PropertyTag="0x1090" PropertyType="Integer"/>'
        f"<t:Value>{value}</t:Value></t:ExtendedProperty>"
    )
    return (
        f'<t:Message><t:ItemId Id="{ident}" ChangeKey="ck"/><t:Subject>{ident}</t:Subject>'
        f"<t:DateTimeReceived>2026-03-02T10:00:00Z</t:DateTimeReceived>{flag}</t:Message>"
    )


@pytest.fixture()
def wire(monkeypatch):
    """FindItem/GetItem requests, answered with the three FLAGS messages."""
    requests: list = []

    def answer(self, payload, **parse_opts):
        requests.append(payload)
        if payload.tag == _M + "GetItem":
            return [
                etree.fromstring(
                    f'<m:GetItemResponseMessage ResponseClass="Success" {_NS}>'
                    "<m:ResponseCode>NoError</m:ResponseCode>"
                    f"<m:Items>{_message_xml(item.get('Id'))}</m:Items>"
                    "</m:GetItemResponseMessage>"
                )
                for item in payload.iter(_T + "ItemId")
            ]
        assert payload.tag == _M + "FindItem", payload.tag
        items = "".join(_message_xml(ident) for ident in FLAGS)
        return [etree.fromstring(
            f'<m:FindItemResponseMessage ResponseClass="Success" {_NS}>'
            "<m:ResponseCode>NoError</m:ResponseCode>"
            f'<m:RootFolder TotalItemsInView="{len(FLAGS)}" IncludesLastItemInRange="true">'
            f"<t:Items>{items}</t:Items></m:RootFolder></m:FindItemResponseMessage>"
        )]

    monkeypatch.setattr(EWSService, "_get_response_xml", answer)
    return requests


@pytest.fixture()
def account(monkeypatch):
    account = offline_account()
    root = Root(account=account)
    account.__dict__["root"] = root
    for name, folder_class in (
        ("inbox", Inbox), ("sent", SentItems), ("drafts", Drafts),
        ("trash", DeletedItems), ("junk", JunkEmail),
    ):
        account.__dict__[name] = folder_class(root=root, id=f"{name}-id", changekey="CK")

    async def resolve(account_, folder_identifier):
        return account.__dict__["inbox"]

    monkeypatch.setattr(email_tools, "resolve_folder_for_account", resolve)
    return account


class _Client:
    mailbox_registry = None

    def __init__(self, account):
        self.account = account
        self.config = SimpleNamespace(ews_email=account.primary_smtp_address)

    def get_account(self, target_mailbox=None):
        return self.account


def _run(tool_cls, account, **kwargs):
    return asyncio.run(tool_cls(_Client(account)).execute(**kwargs))


def _restrictions(requests):
    return [
        etree.tostring(p.find(_M + "Restriction"), encoding="unicode")
        for p in requests if p.tag == _M + "FindItem" and p.find(_M + "Restriction") is not None
    ]


def _flag_tests(restriction):
    """Every PidTagFlagStatus test in a restriction: ("missing",) for
    Not(Exists), else (operator, value) for a comparison."""
    root = etree.fromstring(restriction)
    found = []
    for element in root.iter():
        uri = element.find(_T + "ExtendedFieldURI")
        if uri is None or uri.get("PropertyTag") != "0x1090":
            continue
        operator = etree.QName(element).localname
        if operator == "Exists":
            assert element.getparent().tag == _T + "Not"
            found.append(("missing",))
        else:
            found.append((operator, element.find(f"{_T}FieldURIOrConstant/{_T}Constant").get("Value")))
    return sorted(found)


# ================================================================ Exchange
@pytest.mark.parametrize("status,tests", [
    ("Complete", [("IsEqualTo", "1")]),
    ("Flagged", [("IsEqualTo", "2")]),
    # Never flagged (no property) or cleared to another value.
    ("NotFlagged", [("IsNotEqualTo", "1"), ("IsNotEqualTo", "2"), ("missing",)]),
])
def test_quick_flag_status_filter_restricts_on_pidtagflagstatus(account, wire, status, tests):
    response = _run(email_tools.SearchEmailsTool, account, mode="quick", flag_status=status)

    assert "meta" not in response
    restrictions = _restrictions(wire)
    assert restrictions
    for restriction in restrictions:
        assert _flag_tests(restriction) == tests
        # A filter of its own: no silent "last 30 days" window replaces it.
        assert "item:DateTimeReceived" not in restriction


def test_is_flagged_false_keeps_its_meaning_beside_flag_status(account, wire):
    _run(email_tools.SearchEmailsTool, account, mode="quick", is_flagged=False)

    for restriction in _restrictions(wire):
        assert _flag_tests(restriction) == [("IsNotEqualTo", "2")]


@pytest.mark.parametrize("mode,extra", [
    ("advanced", {"search_scope": ["inbox"]}),
    ("full_text", {"query": "report", "search_scope": ["inbox"]}),
])
def test_advanced_and_full_text_filter_on_flag_status(account, wire, mode, extra):
    _run(email_tools.SearchEmailsTool, account, mode=mode, flag_status="complete", **extra)

    restrictions = _restrictions(wire)
    assert restrictions
    for restriction in restrictions:
        assert _flag_tests(restriction) == [("IsEqualTo", "1")]


def test_an_unknown_flag_status_is_refused(account, wire):
    with pytest.raises(ValidationError, match="flag_status='Done'"):
        _run(email_tools.SearchEmailsTool, account, mode="quick", flag_status="Done")
    assert wire == []


def test_search_items_report_flag_status_read_from_the_wire(account, wire):
    response = _run(email_tools.SearchEmailsTool, account, mode="quick", has_attachments=False,
                    fields=["message_id", "flag_status"])

    assert {i["message_id"]: i["flag_status"] for i in response["items"]} == {
        "done": "Complete", "todo": "Flagged", "never": "NotFlagged",
    }
    # exchangelib reads extended properties through GetItem, not FindItem.
    fetches = [p for p in wire if p.tag == _M + "GetItem"]
    assert fetches and all('PropertyTag="0x1090"' in etree.tostring(p, encoding="unicode")
                           for p in fetches)


def test_default_search_projection_fetches_and_reports_flag_status(account, wire):
    response = _run(email_tools.SearchEmailsTool, account, mode="quick", has_attachments=False)

    assert {i["message_id"]: i["flag_status"] for i in response["items"]} == {
        "done": "Complete", "todo": "Flagged", "never": "NotFlagged",
    }
    fetches = [p for p in wire if p.tag == _M + "GetItem"]
    assert fetches and all('PropertyTag="0x1090"' in etree.tostring(p, encoding="unicode")
                           for p in fetches)


def test_read_emails_reports_flag_status(account, wire):
    response = _run(email_tools.ReadEmailsTool, account, folder="inbox")

    assert {e["message_id"]: e["flag_status"] for e in response["emails"]} == {
        "done": "Complete", "todo": "Flagged", "never": "NotFlagged",
    }


@pytest.mark.parametrize("ident,expected", [("done", "Complete"), ("todo", "Flagged"), ("never", "NotFlagged")])
def test_get_email_details_reports_flag_status(monkeypatch, account, ident, expected):
    item = Message.from_xml(etree.fromstring(f"<root {_NS}>{_message_xml(ident)}</root>")[0], account)
    monkeypatch.setattr(email_tools, "find_message_for_account", lambda a, m: item)

    response = _run(email_tools.GetEmailDetailsTool, account, message_id=ident)

    assert response["email"]["flag_status"] == expected


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


def _graph_rows():
    return [
        {"id": ident, "subject": ident, "isRead": True, "hasAttachments": False,
         "receivedDateTime": "2026-03-02T10:00:00Z", "flag": {"flagStatus": status}}
        for ident, status in (("done", "complete"), ("todo", "flagged"), ("never", "notFlagged"))
    ]


def test_graph_search_filters_and_reports_flag_status():
    requests = []

    def handler(request):
        requests.append(dict(request.url.params))
        return httpx.Response(200, json={"value": _graph_rows()})

    backend = _graph(handler)
    try:
        response = asyncio.run(RoutedSearchEmailsTool(_STUB_CLIENT).execute_routed(
            GRAPH, backend, query="report", flag_status="Complete", max_results=5,
        ))
    finally:
        backend.close()

    assert [(i["id"], i["flag_status"]) for i in response["items"]] == [("done", "Complete")]
    # Filtered locally ($search takes no $filter), so the page is over-fetched.
    assert requests[0]["$top"] == "25"


def test_graph_listing_reports_every_flag_state():
    backend = _graph(lambda request: httpx.Response(200, json={"value": _graph_rows()}))
    try:
        response = asyncio.run(RoutedReadEmailsTool(_STUB_CLIENT).execute_routed(GRAPH, backend))
    finally:
        backend.close()

    assert {e["id"]: e["flag_status"] for e in response["emails"]} == {
        "done": "Complete", "todo": "Flagged", "never": "NotFlagged",
    }
    assert {e["id"]: e.get("is_flagged", False) for e in response["emails"]} == {
        "done": False, "todo": True, "never": False,
    }


# ==================================================================== IMAP
IMAP = MailAccount(email="me@example.com", provider="imap", password="unused", settings={})


def _imap_head(uid, flags):
    headers = (
        f"Subject: message {uid}\r\nFrom: peer@example.com\r\n"
        f"Date: {email.utils.formatdate(0)}\r\n\r\n"
    ).encode()
    head = (
        f'{uid} (UID {uid} FLAGS ({flags}) RFC822.SIZE 100 INTERNALDATE "02-Mar-2026 10:00:00 +0000" '
        f"BODY[HEADER.FIELDS (SUBJECT FROM)] {{{len(headers)}}}"
    ).encode()
    return (head, headers)


@pytest.fixture()
def imap(monkeypatch):
    backend = IMAPBackend(IMAP)
    commands: list = []

    def uid(command, *args):
        commands.append((command, args))
        if command == "SEARCH":
            return "OK", [b"11 12"]
        return "OK", [_imap_head(11, "\\Seen"), b")", _imap_head(12, "\\Seen \\Flagged"), b")"]

    monkeypatch.setattr(backend, "_connect", lambda: SimpleNamespace(uid=uid))
    monkeypatch.setattr(backend, "_native_folder", lambda token: "INBOX")
    monkeypatch.setattr(backend, "_select", lambda native, readonly=True: (2, "7"))
    return backend, commands


@pytest.mark.parametrize("status,key", [("Flagged", "FLAGGED"), ("NotFlagged", "UNFLAGGED")])
def test_imap_search_sends_the_flag_criterion_and_reports_flags(imap, status, key):
    backend, commands = imap

    response = asyncio.run(RoutedSearchEmailsTool(_STUB_CLIENT).execute_routed(
        IMAP, backend, subject_contains="message", flag_status=status.lower(),
    ))

    search = [args for command, args in commands if command == "SEARCH"]
    assert search and key in search[0]
    assert {i["id"]: i["flag_status"] for i in response["items"]} == {
        "7:12": "Flagged", "7:11": "NotFlagged",
    }


def test_imap_refuses_a_complete_filter_clearly(imap):
    backend, commands = imap

    with pytest.raises(ValidationError, match="flag_status='Complete'.*NotFlagged or Flagged"):
        asyncio.run(RoutedSearchEmailsTool(_STUB_CLIENT).execute_routed(
            IMAP, backend, query="x", flag_status="Complete",
        ))
    assert commands == []
