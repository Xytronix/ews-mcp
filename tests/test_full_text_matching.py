"""full_text ``search_emails`` matches within one field, attachment names included.

exact_phrase joined subject and body with a space before matching, so a phrase
split across the two fields matched. ``search_in=["attachments"]`` built no
restriction and skipped every folder without an error, and attachment names
were only checked when the attachments happened to be loaded.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exchangelib import Mailbox  # noqa: E402
from exchangelib.folders import (  # noqa: E402
    DeletedItems, Drafts, Inbox, JunkEmail, Root, SentItems,
)
from exchangelib.services.common import EWSService  # noqa: E402

from src.exceptions import ValidationError  # noqa: E402
from src.tools import email_tools  # noqa: E402
from tests.support import ADDRESS, offline_account  # noqa: E402

_M = "{http://schemas.microsoft.com/exchange/services/2006/messages}"
_T = "{http://schemas.microsoft.com/exchange/services/2006/types}"
_NS = (
    'xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages" '
    'xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"'
)


# ----------------------------------------------------------- folder fakes
def _mail(ident, day, subject="", body="", attachments=(), sender="peer@example.com"):
    return SimpleNamespace(
        id=ident,
        subject=subject,
        sender=Mailbox(name="Peer", email_address=sender),
        to_recipients=[],
        text_body=body,
        body=body,
        datetime_received=datetime(2026, 3, day, tzinfo=timezone.utc),
        is_read=True,
        has_attachments=bool(attachments),
        importance="Normal",
        categories=[],
        attachments=[SimpleNamespace(name=name) for name in attachments],
    )


class _Query:
    """Sorts and slices like FindItem but, like SmarterMail, ignores restrictions."""

    def __init__(self, items):
        self.items = list(items)

    def filter(self, *args, **kwargs):
        return self

    def only(self, *fields):
        return self

    def order_by(self, field):
        name = field.lstrip("-")
        return _Query(sorted(self.items, key=lambda item: getattr(item, name),
                             reverse=field.startswith("-")))

    def count(self):
        return len(self.items)

    def __getitem__(self, window):
        return self.items[window]


class _Folder:
    def __init__(self, name, items):
        self.name = name
        self.items = items

    def filter(self, *args, **kwargs):
        return _Query(self.items)

    def all(self):
        return self.filter()


class _Client:
    mailbox_registry = None

    def __init__(self, inbox, sent=()):
        self.config = SimpleNamespace(ews_email=ADDRESS)
        self.account = SimpleNamespace(
            primary_smtp_address=ADDRESS,
            inbox=_Folder("Inbox", list(inbox)), sent=_Folder("Sent Items", list(sent)),
            drafts=_Folder("Drafts", []), trash=_Folder("Deleted Items", []),
            junk=_Folder("Junk Email", []),
        )

    def get_account(self, target_mailbox=None):
        return self.account


def _search(client, **kwargs):
    tool = email_tools.SearchEmailsTool(client)
    return asyncio.run(tool.execute(
        mode="full_text", fields=["message_id", "subject"], **kwargs,
    ))


def _ids(response):
    return [item["message_id"] for item in response["items"]]


# ------------------------------------------------------------ exact phrase
def test_exact_phrase_does_not_match_across_the_subject_body_boundary():
    straddling = _mail("straddle", 12, subject="Quarterly", body="report attached")
    inside_body = _mail("body", 11, subject="Numbers", body="the quarterly report is late")
    inside_subject = _mail("subject", 10, subject="Quarterly report v2", body="see above")
    client = _Client([straddling, inside_body, inside_subject])

    response = _search(client, query="quarterly report", exact_phrase=True)

    assert _ids(response) == ["body", "subject"]
    assert response["total_available"] == 2


def test_exact_phrase_honours_search_in():
    in_body = _mail("body", 12, subject="Numbers", body="quarterly report")
    in_subject = _mail("subject", 11, subject="Quarterly report", body="")
    client = _Client([in_body, in_subject])

    response = _search(client, query="quarterly report", exact_phrase=True,
                       search_in=["subject"])

    assert _ids(response) == ["subject"]


# ------------------------------------------------------------- attachments
def test_attachment_names_are_searched_and_counted_truthfully():
    inbox = [
        _mail("budget-xlsx", 12, subject="Numbers", attachments=["Q3-BUDGET.xlsx"]),
        _mail("photo", 11, subject="Budget meeting", attachments=["photo.png"]),
        _mail("plain", 10, subject="Budget", body="budget"),
        _mail("budget-pdf", 9, subject="FYI", attachments=["notes.txt", "budget-final.pdf"]),
    ]
    sent = [_mail("sent-budget", 8, subject="Re: numbers", attachments=["Budget v2.docx"])]
    client = _Client(inbox, sent)
    common = dict(query="budget", search_in=["attachments"], max_results=2)

    first = _search(client, **common)
    second = _search(client, offset=first["next_offset"], **common)

    # Only attachment names count: subject and body hits are not in search_in.
    assert _ids(first) == ["budget-xlsx", "budget-pdf"]
    assert _ids(second) == ["sent-budget"]
    assert first["total_available"] == 3
    assert "next_offset" not in second


def test_attachments_combine_with_subject_and_body_as_or():
    inbox = [
        _mail("by-name", 12, subject="Numbers", attachments=["budget.xlsx"]),
        _mail("by-subject", 11, subject="Budget meeting", attachments=["photo.png"]),
        _mail("by-body", 10, subject="Hello", body="the budget is fine"),
        _mail("neither", 9, subject="Lunch", attachments=["menu.pdf"]),
    ]
    client = _Client(inbox)

    response = _search(client, query="budget",
                       search_in=["subject", "body", "attachments"])

    assert _ids(response) == ["by-name", "by-subject", "by-body"]
    assert response["total_available"] == 3


@pytest.mark.parametrize("search_in", [[], ["subject", "headers"]])
def test_search_in_that_names_no_known_area_is_refused(search_in):
    with pytest.raises(ValidationError, match="search_in"):
        _search(_Client([]), query="budget", search_in=search_in)


# ------------------------------------------------------------------- wire
def _find_item_response(ids):
    items = "".join(f'<t:Message><t:ItemId Id="{i}" ChangeKey="ck"/></t:Message>' for i in ids)
    return etree.fromstring(
        f'<m:FindItemResponseMessage ResponseClass="Success" {_NS}>'
        "<m:ResponseCode>NoError</m:ResponseCode>"
        f'<m:RootFolder TotalItemsInView="{len(ids)}" IncludesLastItemInRange="true">'
        f"<t:Items>{items}</t:Items></m:RootFolder></m:FindItemResponseMessage>"
    )


def _get_item_response(ident, day, attachment):
    return etree.fromstring(
        f'<m:GetItemResponseMessage ResponseClass="Success" {_NS}>'
        "<m:ResponseCode>NoError</m:ResponseCode><m:Items><t:Message>"
        f'<t:ItemId Id="{ident}" ChangeKey="ck"/><t:Subject>Numbers</t:Subject>'
        "<t:Attachments><t:FileAttachment>"
        f'<t:AttachmentId Id="att-{ident}"/><t:Name>{attachment}</t:Name><t:Size>10</t:Size>'
        "</t:FileAttachment></t:Attachments>"
        f"<t:DateTimeReceived>2026-03-{day:02d}T10:00:00Z</t:DateTimeReceived>"
        "<t:HasAttachments>true</t:HasAttachments>"
        "</t:Message></m:Items></m:GetItemResponseMessage>"
    )


@pytest.fixture()
def wire(monkeypatch):
    """FindItem/GetItem requests; the inbox holds two messages with attachments."""
    requests: list = []
    stored = {"m1": (12, "Q3-Budget.xlsx"), "m2": (11, "holiday.png")}

    def answer(self, payload, **parse_opts):
        requests.append(payload)
        if payload.tag == _M + "FindItem":
            folder_ids = {f.get("Id") for f in payload.iter(_T + "FolderId")}
            return [_find_item_response(list(stored) if folder_ids == {"inbox-id"} else [])]
        assert payload.tag == _M + "GetItem", payload.tag
        return [
            _get_item_response(item.get("Id"), *stored[item.get("Id")])
            for item in payload.iter(_T + "ItemId")
        ]

    monkeypatch.setattr(EWSService, "_get_response_xml", answer)
    return requests


@pytest.fixture()
def account():
    account = offline_account()
    root = Root(account=account)
    account.__dict__["root"] = root
    for name, folder_class in (
        ("inbox", Inbox), ("sent", SentItems), ("drafts", Drafts),
        ("trash", DeletedItems), ("junk", JunkEmail),
    ):
        account.__dict__[name] = folder_class(root=root, id=f"{name}-id", changekey="CK")
    return account


class _WireClient(_Client):
    def __init__(self, account):
        self.config = SimpleNamespace(ews_email=ADDRESS)
        self.account = account


def test_attachment_search_restricts_to_messages_with_attachments_and_reads_names(account, wire):
    response = asyncio.run(email_tools.SearchEmailsTool(_WireClient(account)).execute(
        mode="full_text", query="budget", search_in=["attachments"],
        search_scope=["inbox"], fields=["message_id", "subject"],
    ))

    assert "meta" not in response
    assert _ids(response) == ["m1"]
    assert response["total_available"] == 1
    find_items = [p for p in wire if p.tag == _M + "FindItem"]
    get_items = [p for p in wire if p.tag == _M + "GetItem"]
    restriction = etree.tostring(find_items[0].find(_M + "Restriction"), encoding="unicode")
    assert 'FieldURI="item:HasAttachments"' in restriction
    assert "budget" not in restriction  # names cannot be restricted on the server
    # Names come from the item's attachment metadata; no attachment content
    # (GetAttachment) is ever requested.
    assert get_items
    assert all('FieldURI="item:Attachments"' in etree.tostring(p, encoding="unicode")
               for p in get_items)
    assert {p.tag for p in wire} == {_M + "FindItem", _M + "GetItem"}


# ------------------------------------------------- quick filters in full_text
def test_full_text_applies_the_quick_filters_on_the_wire(account, wire):
    response = asyncio.run(email_tools.SearchEmailsTool(_WireClient(account)).execute(
        mode="full_text", query="budget", search_scope=["inbox"], fields=["message_id"],
        sender="boss@example.com", is_read=False, is_flagged=True, has_attachments=True,
        importance="High", body_contains="quarter", subject_contains="numbers",
        flag_status="Flagged", categories=["Finance"],
        start_date="2026-03-01T00:00:00+00:00", end_date="2026-03-31T00:00:00+00:00",
    ))

    assert "meta" not in response
    find_items = [p for p in wire if p.tag == _M + "FindItem"]
    assert find_items
    for payload in find_items:
        restriction = payload.find(_M + "Restriction")
        text = etree.tostring(restriction, encoding="unicode")
        for uri in ("message:Sender", "message:IsRead", "item:HasAttachments",
                    "item:Importance", "item:Body", "item:Subject", "item:Categories"):
            assert f'FieldURI="{uri}"' in text, uri
        dates = {
            etree.QName(test.getparent()).localname
            for test in restriction.iter(_T + "FieldURI")
            if test.get("FieldURI") == "item:DateTimeReceived"
        }
        assert dates == {"IsGreaterThanOrEqualTo", "IsLessThanOrEqualTo"}
        assert 'PropertyTag="0x1090"' in text
        for value in ("budget", "boss@example.com", "quarter", "numbers", "Finance"):
            assert value in text, value


def test_full_text_checks_a_partial_sender_and_the_subject_locally():
    inbox = [
        _mail("boss", 12, subject="Budget numbers", sender="boss@corp.example.com"),
        _mail("peer", 11, subject="Budget numbers", sender="peer@example.com"),
        _mail("other-subject", 10, subject="Budget", sender="boss@corp.example.com"),
    ]

    response = _search(_Client(inbox), query="budget", sender="corp.example",
                       subject_contains="numbers")

    assert _ids(response) == ["boss"]
    assert response["total_available"] == 1


@pytest.mark.parametrize("extra,message", [
    ({"keywords": "budget"}, "keywords"),
    ({"sort_by": "subject"}, "newest first"),
    ({"sort_order": "ascending"}, "newest first"),
    ({"start_date": "last tuesday"}, "start_date='last tuesday'"),
])
def test_full_text_refuses_what_it_cannot_apply(extra, message):
    with pytest.raises(ValidationError, match=message):
        _search(_Client([_mail("m", 12, subject="budget")]), query="budget", **extra)
