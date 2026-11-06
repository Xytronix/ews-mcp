"""``to_address`` / ``recipient`` filter on the To list in every search mode.

exchangelib refuses ``to_recipients`` in a restriction ("EWS does not support
filtering on field 'to_recipients'"), so quick and advanced mode put a filter
on the wire that failed when the query ran: the search returned nothing but a
``meta`` error. The recipient is now checked on the fetched To list.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exchangelib.folders import (  # noqa: E402
    DeletedItems, Drafts, Inbox, JunkEmail, Root, SentItems,
)
from exchangelib.services.common import EWSService  # noqa: E402

from src.tools import email_tools  # noqa: E402
from tests.support import offline_account  # noqa: E402

_M = "{http://schemas.microsoft.com/exchange/services/2006/messages}"
_T = "{http://schemas.microsoft.com/exchange/services/2006/types}"
_NS = (
    'xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages" '
    'xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"'
)
# message id -> (day received, To recipients as (name, address))
STORED = {
    "to-team": (12, [("Team", "team@example.com")]),
    "to-boss": (11, [("Boss", "boss@example.com")]),
    "to-both": (10, [("Boss", "boss@example.com"), ("Team Lead", "TEAM@example.com")]),
}


def _message_xml(ident):
    day, recipients = STORED[ident]
    to = "".join(
        f"<t:Mailbox><t:Name>{name}</t:Name><t:EmailAddress>{address}</t:EmailAddress></t:Mailbox>"
        for name, address in recipients
    )
    return (
        f'<t:Message><t:ItemId Id="{ident}" ChangeKey="ck"/><t:Subject>report</t:Subject>'
        f"<t:DateTimeReceived>2026-03-{day:02d}T10:00:00Z</t:DateTimeReceived>"
        f"<t:ToRecipients>{to}</t:ToRecipients></t:Message>"
    )


@pytest.fixture()
def wire(monkeypatch):
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
        items = "".join(_message_xml(ident) for ident in STORED)
        return [etree.fromstring(
            f'<m:FindItemResponseMessage ResponseClass="Success" {_NS}>'
            "<m:ResponseCode>NoError</m:ResponseCode>"
            f'<m:RootFolder TotalItemsInView="{len(STORED)}" IncludesLastItemInRange="true">'
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


def _search(account, **kwargs):
    return asyncio.run(email_tools.SearchEmailsTool(_Client(account)).execute(
        fields=["message_id"], **kwargs,
    ))


@pytest.mark.parametrize("mode,extra", [
    ("quick", {}),
    ("advanced", {"search_scope": ["inbox"]}),
    ("full_text", {"query": "report", "search_scope": ["inbox"]}),
])
def test_a_full_recipient_address_matches_any_to_recipient(account, wire, mode, extra):
    key = "recipient" if mode == "full_text" else "to_address"
    response = _search(account, mode=mode, **{key: "team@example.com"}, **extra)

    assert "meta" not in response
    assert [i["message_id"] for i in response["items"]] == ["to-team", "to-both"]
    assert response["total_available"] == 2
    for payload in wire:
        restriction = payload.find(_M + "Restriction")
        if restriction is not None:
            assert "ToRecipients" not in etree.tostring(restriction, encoding="unicode")


def test_a_partial_recipient_matches_an_address_or_display_name(account, wire):
    response = _search(account, mode="quick", recipient="lead")

    assert [i["message_id"] for i in response["items"]] == ["to-both"]
