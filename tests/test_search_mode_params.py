"""``search_emails`` refuses what a mode would ignore and honours its aliases.

Advanced mode dropped an unreadable date and ignored the ``sender`` /
``recipient`` aliases, every mode silently ignored the parameters of the other
modes, an unknown ``mode`` ran as quick, and quick mode ignored sort_by /
sort_order. Each of those is now applied or refused with the mode to use.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exchangelib import Mailbox  # noqa: E402
from exchangelib.folders import (  # noqa: E402
    DeletedItems, Drafts, Inbox, JunkEmail, Root, SentItems,
)
from exchangelib.services.common import EWSService  # noqa: E402

from src.exceptions import ValidationError  # noqa: E402
from src.mail_backends.accounts import MailAccount  # noqa: E402
from src.mail_backends.graph_backend import GraphBackend  # noqa: E402
from src.tools import email_tools  # noqa: E402
from src.tools.mailbox_tools import SearchEmailsTool as RoutedSearchEmailsTool  # noqa: E402
from tests.support import ADDRESS, offline_account  # noqa: E402

_M = "{http://schemas.microsoft.com/exchange/services/2006/messages}"
_T = "{http://schemas.microsoft.com/exchange/services/2006/types}"


# ----------------------------------------------------------- folder fakes
def _mail(ident, day, subject, sender="peer@example.com", to=("team@example.com",)):
    return SimpleNamespace(
        id=ident,
        subject=subject,
        sender=Mailbox(name=sender.split("@")[0].title(), email_address=sender),
        to_recipients=[Mailbox(email_address=address) for address in to],
        text_body="", body="",
        datetime_received=datetime(2026, 3, day, tzinfo=timezone.utc),
        is_read=True, has_attachments=False, importance="Normal", categories=[],
    )


class _Query:
    """Sorts and slices like FindItem but, like SmarterMail, ignores restrictions."""

    def __init__(self, items):
        self.items = list(items)

    def filter(self, *args, **kwargs):
        return self

    exclude = filter

    def only(self, *fields):
        return self

    def order_by(self, field):
        name = field.lstrip("-")

        def value(item):
            # FindItem sorts text case-insensitively; a mailbox by its name.
            found = getattr(item, name)
            if isinstance(found, Mailbox):
                return found.name.casefold()
            return found.casefold() if isinstance(found, str) else found

        return _Query(sorted(self.items, key=value, reverse=field.startswith("-")))

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


INBOX = [
    _mail("in-12", 12, "Budget", sender="boss@corp.example.com"),
    _mail("in-11", 11, "agenda", to=("other@example.com",)),
    _mail("in-10", 10, "Zebra crossing", sender="boss@corp.example.com"),
]
SENT = [
    _mail("sent-9", 9, "minutes", sender=ADDRESS, to=("team@example.com", "boss@corp.example.com")),
    _mail("sent-8", 8, "Copy", sender=ADDRESS),
]


class _Client:
    mailbox_registry = None

    def __init__(self):
        self.config = SimpleNamespace(ews_email=ADDRESS)
        self.account = SimpleNamespace(
            primary_smtp_address=ADDRESS,
            inbox=_Folder("Inbox", INBOX), sent=_Folder("Sent Items", SENT),
            drafts=_Folder("Drafts", []), trash=_Folder("Deleted Items", []),
            junk=_Folder("Junk Email", []),
        )

    def get_account(self, target_mailbox=None):
        return self.account


@pytest.fixture()
def client(monkeypatch):
    client = _Client()

    async def resolve(account, folder_identifier):
        return {"inbox": account.inbox, "sent": account.sent}[folder_identifier.lower()]

    monkeypatch.setattr(email_tools, "resolve_folder_for_account", resolve)
    return client


def _search(client, **kwargs):
    return asyncio.run(email_tools.SearchEmailsTool(client).execute(
        fields=["message_id", "subject"], **kwargs,
    ))


def _ids(response):
    return [item["message_id"] for item in response["items"]]


# ============================================================ (a) advanced
@pytest.mark.parametrize("key", ["start_date", "end_date"])
def test_advanced_refuses_an_unreadable_date(client, key):
    with pytest.raises(ValidationError, match=f"{key}='end of march'"):
        _search(client, mode="advanced", search_scope=["inbox"], is_read=True,
                **{key: "end of march"})


def test_advanced_applies_the_sender_alias(client):
    response = _search(client, mode="advanced", search_scope=["inbox", "sent"],
                       sender="corp.example")

    assert _ids(response) == ["in-12", "in-10"]


def test_advanced_applies_the_recipient_alias(client):
    response = _search(client, mode="advanced", search_scope=["inbox", "sent"],
                       recipient="boss@corp.example.com")

    assert _ids(response) == ["sent-9"]


# ======================================================= (b) other modes
@pytest.mark.parametrize("mode,name,value,named", [
    ("quick", "keywords", "budget", "'advanced'"),
    ("quick", "search_query", "budget", "'full_text'"),
    ("quick", "search_in", ["attachments"], "'full_text'"),
    ("quick", "exact_phrase", True, "'full_text'"),
    ("advanced", "query", "budget", "'quick' or 'full_text'"),
    ("advanced", "search_query", "budget", "'full_text'"),
    ("advanced", "search_in", ["subject"], "'full_text'"),
    ("advanced", "exact_phrase", True, "'full_text'"),
    ("full_text", "keywords", "budget", "'advanced'"),
    ("full_text", "sort_by", "subject", "'quick' or 'advanced'"),
])
def test_a_parameter_of_another_mode_is_refused_with_that_mode(client, mode, name, value, named):
    # Each call is otherwise valid for its mode.
    valid = {"quick": {}, "advanced": {"is_read": True}, "full_text": {"query": "budget"}}[mode]
    with pytest.raises(ValidationError, match=f"{name} is not used in mode '{mode}'; use mode {named}"):
        _search(client, mode=mode, **valid, **{name: value})


def test_schema_defaults_of_other_modes_are_not_a_request(client):
    response = _search(client, mode="quick", subject_contains="budget",
                       search_in=["body", "subject"], exact_phrase=False,
                       sort_by="datetime_received", sort_order="descending")

    assert _ids(response) == ["in-12"]


def test_an_unknown_mode_is_refused_instead_of_running_as_quick(client):
    with pytest.raises(ValidationError, match="mode='fulltext': use one of quick, advanced, full_text"):
        _search(client, mode="fulltext", query="budget")


@pytest.mark.parametrize("params,message", [
    ({"sort_by": "size"}, "sort_by='size'"),
    ({"sort_order": "up"}, "sort_order='up'"),
])
def test_an_unknown_sort_is_refused(client, params, message):
    with pytest.raises(ValidationError, match=message):
        _search(client, mode="advanced", search_scope=["inbox"], is_read=True, **params)


def test_quick_mode_sorts_across_folders(client):
    common = dict(mode="quick", search_scope=["inbox", "sent"], is_read=True,
                  sort_by="subject", sort_order="ascending", max_results=3)

    first = _search(client, **common)
    second = _search(client, offset=first["next_offset"], **common)

    # agenda, Budget, Copy | minutes, Zebra crossing
    assert _ids(first) == ["in-11", "in-12", "sent-8"]
    assert _ids(second) == ["sent-9", "in-10"]
    assert "next_offset" not in second


def test_quick_mode_sorts_one_folder_on_the_server(monkeypatch):
    requests: list = []

    def answer(self, payload, **parse_opts):
        requests.append(payload)
        return [etree.fromstring(
            '<m:FindItemResponseMessage ResponseClass="Success"'
            ' xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages"'
            ' xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types">'
            "<m:ResponseCode>NoError</m:ResponseCode>"
            '<m:RootFolder TotalItemsInView="0" IncludesLastItemInRange="true">'
            "<t:Items/></m:RootFolder></m:FindItemResponseMessage>"
        )]

    monkeypatch.setattr(EWSService, "_get_response_xml", answer)
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
    tool_client = SimpleNamespace(
        mailbox_registry=None, config=SimpleNamespace(ews_email=ADDRESS),
        get_account=lambda target_mailbox=None: account,
    )

    asyncio.run(email_tools.SearchEmailsTool(tool_client).execute(
        mode="quick", is_read=True, sort_by="from", sort_order="ascending",
    ))

    orders = [p.find(_M + "SortOrder") for p in requests if p.find(_M + "SortOrder") is not None]
    assert orders
    for order in orders:
        text = etree.tostring(order, encoding="unicode")
        assert 'Order="Ascending"' in text
        assert 'FieldURI="message:Sender"' in text


# ============================================================ (c) folder
@pytest.mark.parametrize("mode,extra", [
    ("advanced", {"is_read": True}),
    ("full_text", {"query": "budget"}),
])
def test_advanced_and_full_text_still_ignore_an_auto_filled_folder(client, mode, extra):
    response = _search(client, mode=mode, folder="Archive", search_scope=["inbox"], **extra)

    assert "in-12" in _ids(response)


# ================================================================= routed
GRAPH = MailAccount(
    email="user@outlook.com", provider="outlook",
    settings={"client_id": "cid", "refresh_token": "rt"},
)


def _graph(calls):
    def handler(request):
        calls.append(dict(request.url.params))
        return httpx.Response(200, json={"value": []})

    backend = GraphBackend(GRAPH)
    backend._access_token = "token"
    backend._token_expires_at = 1 << 40
    backend._client = httpx.Client(transport=httpx.MockTransport(handler))
    return backend


def _routed(**kwargs):
    calls: list = []
    response = asyncio.run(RoutedSearchEmailsTool(
        SimpleNamespace(config=None, mailbox_registry=None),
    ).execute_routed(GRAPH, _graph(calls), **kwargs))
    return response, calls


@pytest.mark.parametrize("params,message", [
    ({"keywords": "budget"}, "keywords is not used in mode 'quick'"),
    ({"mode": "full_text", "keywords": "budget"}, "keywords is not used in mode 'full_text'"),
    ({"search_query": "budget"}, "search_query is not used in mode 'quick'"),
    ({"is_read": True}, "is_read=true: not supported"),
    ({"mode": "fulltext", "query": "x"}, "mode='fulltext'"),
])
def test_routed_search_refuses_what_it_would_ignore(params, message):
    with pytest.raises(ValidationError, match=message):
        _routed(**params)


def test_routed_search_accepts_schema_defaults_of_exchange_only_params():
    response, calls = _routed(query="budget", search_in=["subject", "body"],
                              exact_phrase=False, sort_by="datetime_received",
                              sort_order="descending", folder="inbox", search_scope=["inbox"])

    assert response["success"] is True
    assert calls and calls[0]["$search"] == '"budget"'
