"""``search_emails`` puts the caller's filters and sort on the EWS wire.

Quick mode did not count ``categories`` or ``is_flagged`` as filters, so a
search on either alone was narrowed to the last 30 days. It never applied
``categories`` at all, and full_text ignored it too. Advanced mode passed
``sort_by="from"`` straight to exchangelib, which has no such field, so every
folder failed and the search returned nothing. These tests read the EWS
FindItem request that would be sent to the server.
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
_EMPTY_FIND_ITEM = (
    '<m:FindItemResponseMessage ResponseClass="Success"'
    ' xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages"'
    ' xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types">'
    "<m:ResponseCode>NoError</m:ResponseCode>"
    '<m:RootFolder TotalItemsInView="0" IncludesLastItemInRange="true">'
    "<t:Items/></m:RootFolder>"
    "</m:FindItemResponseMessage>"
)


class _Client:
    mailbox_registry = None

    def __init__(self, account):
        self.account = account
        self.config = SimpleNamespace(ews_email=account.primary_smtp_address)

    def get_account(self, target_mailbox=None):
        return self.account


@pytest.fixture()
def find_items(monkeypatch):
    """Every FindItem request body; the server answers with an empty folder."""
    requests: list = []

    def answer(self, payload, **parse_opts):
        assert payload.tag == _M + "FindItem"
        requests.append(payload)
        return [etree.fromstring(_EMPTY_FIND_ITEM)]

    monkeypatch.setattr(EWSService, "_get_response_xml", answer)
    return requests


@pytest.fixture()
def account(monkeypatch):
    account = offline_account()
    root = Root(account=account)
    # Item fetches validate against account.root, and full_text maps five
    # well-known folders; keep every one of those lookups offline.
    account.__dict__["root"] = root
    for name, folder_class in (
        ("inbox", Inbox), ("sent", SentItems), ("drafts", Drafts),
        ("trash", DeletedItems), ("junk", JunkEmail),
    ):
        account.__dict__[name] = folder_class(root=root, id=f"{name}-id", changekey="CK")
    inbox = account.__dict__["inbox"]

    async def resolve(account_, folder_identifier):
        return inbox

    monkeypatch.setattr(email_tools, "resolve_folder_for_account", resolve)
    return account


def _restrictions(requests) -> list:
    out = []
    for payload in requests:
        restriction = payload.find(_M + "Restriction")
        out.append(
            "" if restriction is None
            else etree.tostring(restriction, encoding="unicode")
        )
    return out


def _search(account, **kwargs):
    tool = email_tools.SearchEmailsTool(_Client(account))
    return asyncio.run(tool.execute(**kwargs))


def test_quick_search_by_category_alone_restricts_to_that_category(account, find_items):
    response = _search(account, mode="quick", categories=["Project X"])

    assert response["count"] == 0
    assert "meta" not in response
    restrictions = _restrictions(find_items)
    assert restrictions
    for restriction in restrictions:
        assert 'FieldURI="item:Categories"' in restriction
        assert "Project X" in restriction
        # A real filter: no silent "last 30 days" window replaces it.
        assert "item:DateTimeReceived" not in restriction


def test_quick_search_needs_every_listed_category(account, find_items):
    _search(account, mode="quick", categories=["Project X", "Urgent"],
            start_date="2026-01-01T00:00:00+00:00")

    for restriction in _restrictions(find_items):
        assert "Project X" in restriction and "Urgent" in restriction
        assert 'FieldURI="item:DateTimeReceived"' in restriction


def test_full_text_search_restricts_to_the_category(account, find_items):
    response = _search(account, mode="full_text", query="budget", categories=["Project X"])

    assert "meta" not in response
    restrictions = _restrictions(find_items)
    assert restrictions
    for restriction in restrictions:
        assert "budget" in restriction
        assert 'FieldURI="item:Categories"' in restriction
        assert "Project X" in restriction


def test_quick_search_without_any_filter_keeps_the_30_day_default(account, find_items):
    _search(account, mode="quick")

    for restriction in _restrictions(find_items):
        assert 'FieldURI="item:DateTimeReceived"' in restriction
        assert "item:Categories" not in restriction


@pytest.mark.parametrize("flagged", [True, False])
def test_quick_search_by_flag_alone_drops_the_30_day_window(account, find_items, flagged):
    response = _search(account, mode="quick", is_flagged=flagged)

    assert "meta" not in response
    restrictions = _restrictions(find_items)
    assert restrictions
    for restriction in restrictions:
        assert 'PropertyTag="0x1090"' in restriction
        assert "item:DateTimeReceived" not in restriction


def test_quick_search_with_flag_unset_keeps_the_30_day_default(account, find_items):
    _search(account, mode="quick", is_flagged=None)

    for restriction in _restrictions(find_items):
        assert 'FieldURI="item:DateTimeReceived"' in restriction
        assert "0x1090" not in restriction


def test_advanced_sort_by_from_orders_every_folder_by_sender(account, find_items):
    response = _search(account, mode="advanced", search_scope=["inbox", "sent"],
                       is_read=True, sort_by="from")

    assert "meta" not in response
    sorted_folders = set()
    for payload in find_items:
        sort_order = payload.find(_M + "SortOrder")
        if sort_order is None:
            continue  # the count() request carries no sort
        order = etree.tostring(sort_order, encoding="unicode")
        assert 'Order="Descending"' in order
        assert 'FieldURI="message:Sender"' in order
        sorted_folders.update(
            folder.get("Id") for folder in payload.iter(_T + "FolderId")
        )
    assert sorted_folders == {"inbox-id", "sent-id"}
