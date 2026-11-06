"""``search_scope=["all"]`` searches every mail folder of the mailbox at once.

Exchange enumerates the mail folders under msg_folder_root, skips Deleted
Items, Junk, Drafts and Outbox with their subtrees unless named, searches each
folder and merges one sorted page. Graph uses its mailbox-wide messages
collection. IMAP has no such collection and refuses the scope.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exchangelib.folders import (  # noqa: E402
    Calendar, Contacts, DeletedItems, Drafts, Inbox, JunkEmail, Messages,
    MsgFolderRoot, Outbox, Root, SentItems, Tasks,
)
from exchangelib.services.common import EWSService  # noqa: E402

from src.exceptions import ToolExecutionError, ValidationError  # noqa: E402
from src.mail_backends.accounts import MailAccount  # noqa: E402
from src.mail_backends.graph_backend import GraphBackend  # noqa: E402
from src.mail_backends.imap_backend import IMAPBackend  # noqa: E402
from src.tools import email_tools  # noqa: E402
from src.tools.mailbox_tools import SearchEmailsTool as RoutedSearchEmailsTool  # noqa: E402
from tests.support import offline_account  # noqa: E402

_M = "{http://schemas.microsoft.com/exchange/services/2006/messages}"
_T = "{http://schemas.microsoft.com/exchange/services/2006/types}"
_NS = (
    'xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages" '
    'xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"'
)

# folder id -> messages (id, day received), newest first as FindItem sorts them
MAIL = {
    "inbox": [("inbox-12", 12), ("inbox-9", 9), ("inbox-3", 3)],
    "projects": [("projects-11", 11), ("projects-4", 4)],
    "sent": [("sent-10", 10)],
    "archive": [("archive-8", 8), ("archive-1", 1)],
    "trash": [("trash-13", 13)],
    "old": [("old-14", 14)],
    "junk": [("junk-15", 15)],
    "drafts": [("drafts-16", 16)],
    "outbox": [("outbox-17", 17)],
    "birthdays": [("birthday-18", 18)],
}


def _message_xml(ident, day):
    return (
        f'<t:Message><t:ItemId Id="{ident}" ChangeKey="ck"/>'
        f"<t:Subject>report {ident}</t:Subject>"
        f"<t:DateTimeReceived>2026-03-{day:02d}T10:00:00Z</t:DateTimeReceived>"
        "<t:IsRead>true</t:IsRead></t:Message>"
    )


def _find_item_answer(folder_id, failing):
    if folder_id in failing:
        return etree.fromstring(
            f'<m:FindItemResponseMessage ResponseClass="Error" {_NS}>'
            "<m:MessageText>Access is denied.</m:MessageText>"
            "<m:ResponseCode>ErrorAccessDenied</m:ResponseCode>"
            "<m:DescriptiveLinkKey>0</m:DescriptiveLinkKey>"
            "</m:FindItemResponseMessage>"
        )
    items = "".join(_message_xml(i, d) for i, d in MAIL[folder_id])
    return etree.fromstring(
        f'<m:FindItemResponseMessage ResponseClass="Success" {_NS}>'
        "<m:ResponseCode>NoError</m:ResponseCode>"
        f'<m:RootFolder TotalItemsInView="{len(MAIL[folder_id])}" IncludesLastItemInRange="true">'
        f"<t:Items>{items}</t:Items></m:RootFolder></m:FindItemResponseMessage>"
    )


@pytest.fixture()
def server(monkeypatch):
    """Answers FindItem per folder and GetItem per item; records folder ids."""
    state = SimpleNamespace(searched=set(), failing=set())
    days = {ident: day for messages in MAIL.values() for ident, day in messages}

    def answer(self, payload, **parse_opts):
        if payload.tag == _M + "GetItem":
            return [
                etree.fromstring(
                    f'<m:GetItemResponseMessage ResponseClass="Success" {_NS}>'
                    "<m:ResponseCode>NoError</m:ResponseCode><m:Items>"
                    f"{_message_xml(item.get('Id'), days[item.get('Id')])}"
                    "</m:Items></m:GetItemResponseMessage>"
                )
                for item in payload.iter(_T + "ItemId")
            ]
        assert payload.tag == _M + "FindItem", payload.tag
        (folder_id,) = {f.get("Id") for f in payload.iter(_T + "FolderId")}
        state.searched.add(folder_id)
        return [_find_item_answer(folder_id, state.failing)]

    monkeypatch.setattr(EWSService, "_get_response_xml", answer)
    return state


@pytest.fixture()
def account():
    """A mailbox hierarchy held in exchangelib's folder cache, never fetched."""
    account = offline_account()
    root = Root(account=account, id="root", changekey="CK")
    root._subfolders = {root.id: root}

    def folder(cls, ident, name, parent, folder_class="IPF.Note"):
        made = cls(root=root, id=ident, changekey="CK", name=name,
                   folder_class=folder_class, parent=parent)
        root._subfolders[ident] = made
        return made

    top = folder(MsgFolderRoot, "top", "Top of Information Store", root)
    inbox = folder(Inbox, "inbox", "Inbox", top)
    folder(Messages, "projects", "Projects", inbox, folder_class=None)  # no class
    sent = folder(SentItems, "sent", "Sent Items", top)
    calendar = folder(Calendar, "calendar", "Calendar", top, "IPF.Appointment")
    folder(Calendar, "birthdays", "Birthdays", calendar, "IPF.Appointment")
    trash = folder(DeletedItems, "trash", "Deleted Items", top)
    folder(Messages, "old", "Old", trash)
    junk = folder(JunkEmail, "junk", "Junk Email", top)
    drafts = folder(Drafts, "drafts", "Drafts", top)
    outbox = folder(Outbox, "outbox", "Outbox", top)
    folder(Messages, "archive", "Archive", top)
    contacts = folder(Contacts, "contacts", "Contacts", top, "IPF.Contact")
    tasks = folder(Tasks, "tasks", "Tasks", top, "IPF.Task")
    for name, value in (
        ("root", root), ("msg_folder_root", top), ("inbox", inbox), ("sent", sent),
        ("trash", trash), ("junk", junk), ("drafts", drafts), ("outbox", outbox),
        ("calendar", calendar), ("contacts", contacts), ("tasks", tasks),
    ):
        account.__dict__[name] = value
    return account


class _Client:
    mailbox_registry = None

    def __init__(self, account):
        self.account = account
        self.config = SimpleNamespace(ews_email=account.primary_smtp_address)

    def get_account(self, target_mailbox=None):
        return self.account


def _search(account, **kwargs):
    return asyncio.run(email_tools.SearchEmailsTool(_Client(account)).execute(**kwargs))


def _hits(response):
    return [(item["message_id"], item["folder"]) for item in response["items"]]


_FIELDS = ["message_id", "received_time", "folder"]


# ================================================================ Exchange
def test_all_scope_searches_every_mail_folder_into_one_sorted_page(account, server):
    response = _search(account, mode="advanced", search_scope=["all"], is_read=True,
                       fields=_FIELDS, max_results=5)

    # Mail folders only, and none of the four excluded subtrees.
    assert server.searched == {"inbox", "projects", "sent", "archive"}
    assert _hits(response) == [
        ("inbox-12", "Inbox"), ("projects-11", "Inbox/Projects"), ("sent-10", "Sent Items"),
        ("inbox-9", "Inbox"), ("archive-8", "Archive"),
    ]
    assert sorted(response["folders_searched"]) == [
        "Archive", "Inbox", "Inbox/Projects", "Sent Items",
    ]
    assert response["folders_failed"] == []
    assert response["total_available"] == 8
    assert response["next_offset"] == 5


def test_all_scope_pages_cover_every_message_once(account, server):
    common = dict(mode="full_text", query="report", search_scope=["all"],
                  fields=_FIELDS, max_results=3)

    pages = [_search(account, **common)]
    while "next_offset" in pages[-1]:
        pages.append(_search(account, offset=pages[-1]["next_offset"], **common))

    seen = [ident for page in pages for ident, _folder in _hits(page)]
    assert seen == [
        "inbox-12", "projects-11", "sent-10", "inbox-9", "archive-8",
        "projects-4", "inbox-3", "archive-1",
    ]
    assert len(pages) == 3


def test_naming_an_excluded_folder_next_to_all_adds_its_subtree(account, server):
    response = _search(account, mode="full_text", query="report",
                       search_scope=["all", "deleted", "outbox"], fields=_FIELDS)

    assert server.searched == {"inbox", "projects", "sent", "archive", "trash", "old", "outbox"}
    assert _hits(response)[:3] == [
        ("outbox-17", "Outbox"), ("old-14", "Deleted Items/Old"), ("trash-13", "Deleted Items"),
    ]


def test_one_failing_folder_is_listed_and_the_rest_still_answer(account, server):
    server.failing.add("archive")

    response = _search(account, mode="advanced", search_scope=["all"], is_read=True,
                       fields=_FIELDS)

    assert [f["folder"] for f in response["folders_failed"]] == ["Archive"]
    assert response["folders_failed"][0]["error_code"] == "AUTH_EXPIRED"
    assert "Archive" not in response["folders_searched"]
    assert {folder for _ident, folder in _hits(response)} == {
        "Inbox", "Inbox/Projects", "Sent Items",
    }
    assert response["total_available"] is None


def test_default_projection_names_each_hits_folder(account, server):
    response = _search(account, mode="full_text", query="report", search_scope=["all"])

    assert response["items"]
    for item in response["items"]:
        assert item["folder"] in {"Inbox", "Inbox/Projects", "Sent Items", "Archive"}
        assert item["flag_status"] == "NotFlagged"


def test_too_many_folders_are_refused_before_searching(monkeypatch, account, server):
    monkeypatch.setattr(email_tools, "SEARCH_ALL_MAX_FOLDERS", 3)

    with pytest.raises(ToolExecutionError, match="4 mail folders.*EWS_SEARCH_ALL_MAX_FOLDERS"):
        _search(account, mode="full_text", query="report", search_scope=["all"])
    assert server.searched == set()


def test_quick_mode_searches_the_all_scope_too(account, server):
    response = _search(account, mode="quick", subject_contains="report", search_scope=["all"],
                       fields=_FIELDS, max_results=3)

    assert server.searched == {"inbox", "projects", "sent", "archive"}
    assert _hits(response) == [
        ("inbox-12", "Inbox"), ("projects-11", "Inbox/Projects"), ("sent-10", "Sent Items"),
    ]
    assert response["total_available"] == 8
    assert response["folders_failed"] == []


# ------------------------------------------------------------ named scopes
def test_a_folder_path_in_search_scope_is_resolved_and_searched(account, server):
    response = _search(account, mode="full_text", query="report",
                       search_scope=["sent", "Inbox/Projects", "Archive"], fields=_FIELDS)

    assert server.searched == {"sent", "projects", "archive"}
    assert _hits(response) == [
        ("projects-11", "Projects"), ("sent-10", "Sent Items"), ("archive-8", "Archive"),
        ("projects-4", "Projects"), ("archive-1", "Archive"),
    ]


def test_an_unknown_scope_name_is_refused_with_the_accepted_names(account, server):
    with pytest.raises(ValidationError) as caught:
        _search(account, mode="advanced", search_scope=["inbox", "Nowhere"], is_read=True)

    message = str(caught.value)
    assert "'Nowhere' not found" in message
    for accepted in ("inbox", "sent", "drafts", "deleted", "junk", "all", "folder path"):
        assert accepted in message
    assert server.searched == set()


def test_a_non_mail_folder_in_search_scope_is_refused(account, server):
    with pytest.raises(ValidationError, match="'calendar' is not a mail folder"):
        _search(account, mode="full_text", query="report", search_scope=["calendar"])
    assert server.searched == set()


def test_a_folder_named_twice_is_searched_once(account, server):
    response = _search(account, mode="advanced", is_read=True, fields=_FIELDS,
                       search_scope=["inbox", "Inbox", "/Top of Information Store/Inbox"])

    assert server.searched == {"inbox"}
    assert [ident for ident, _folder in _hits(response)] == ["inbox-12", "inbox-9", "inbox-3"]


def test_quick_mode_honours_a_named_search_scope(account, server):
    response = _search(account, mode="quick", subject_contains="report",
                       search_scope=["inbox", "sent"], fields=_FIELDS, max_results=3)

    assert server.searched == {"inbox", "sent"}
    assert _hits(response) == [
        ("inbox-12", "Inbox"), ("sent-10", "Sent Items"), ("inbox-9", "Inbox"),
    ]
    assert response["next_offset"] == 3
    assert response["total_available"] == 4


def test_quick_mode_refuses_two_different_folder_choices(account, server):
    with pytest.raises(ValidationError, match="either 'folder' or 'search_scope'"):
        _search(account, mode="quick", folder="sent", search_scope=["Archive"],
                subject_contains="report")
    assert server.searched == set()


@pytest.mark.parametrize("folder,scope,searched", [
    ("inbox", ["sent"], {"sent"}),       # folder left at its default
    ("sent", ["inbox"], {"sent"}),       # scope left at its default
    ("inbox", ["inbox"], {"inbox"}),     # both at their defaults
])
def test_quick_mode_lets_a_default_folder_or_scope_yield(account, server, folder, scope, searched):
    response = _search(account, mode="quick", folder=folder, search_scope=scope,
                       subject_contains="report", fields=_FIELDS)

    assert server.searched == searched
    assert response["items"]


def test_all_cannot_be_combined_with_an_unknown_folder(account, server):
    with pytest.raises(ValidationError, match="'archive' cannot be combined with 'all'"):
        _search(account, mode="full_text", query="report", search_scope=["all", "archive"])


# =================================================================== Graph
GRAPH = MailAccount(
    email="user@outlook.com", provider="outlook",
    settings={"client_id": "cid", "refresh_token": "rt"},
)
_STUB_CLIENT = SimpleNamespace(config=None, mailbox_registry=None)

_GRAPH_FOLDERS = {
    None: [
        {"id": "F-IN", "displayName": "Inbox", "childFolderCount": 1},
        {"id": "F-SENT", "displayName": "Sent Items", "childFolderCount": 0},
        {"id": "F-DEL", "displayName": "Deleted Items", "childFolderCount": 1},
        {"id": "F-JUNK", "displayName": "Junk Email", "childFolderCount": 0},
        {"id": "F-DRAFTS", "displayName": "Drafts", "childFolderCount": 0},
        {"id": "F-ARC", "displayName": "Archive", "childFolderCount": 0},
    ],
    "F-IN": [{"id": "F-PROJ", "displayName": "Projects", "childFolderCount": 0}],
    "F-DEL": [{"id": "F-OLD", "displayName": "Old", "childFolderCount": 0}],
}
_WELL_KNOWN_IDS = {"deleteditems": "F-DEL", "junkemail": "F-JUNK", "drafts": "F-DRAFTS"}


def _graph_message(ident, folder_id, day, flag="notFlagged"):
    return {
        "id": ident, "subject": f"report {ident}", "parentFolderId": folder_id,
        "receivedDateTime": f"2026-03-{day:02d}T10:00:00Z", "isRead": True,
        "hasAttachments": False, "flag": {"flagStatus": flag},
        "from": {"emailAddress": {"address": "peer@example.com"}},
    }


class _GraphServer:
    def __init__(self):
        self.calls: list = []
        self.messages = [
            _graph_message("m-del", "F-DEL", 14),
            _graph_message("m-in", "F-IN", 13),
            _graph_message("m-old", "F-OLD", 12),
            _graph_message("m-proj", "F-PROJ", 11),
            _graph_message("m-hidden", "F-HIDDEN", 10),
            _graph_message("m-junk", "F-JUNK", 9),
            _graph_message("m-arc", "F-ARC", 8),
        ]

    def __call__(self, request):
        path = request.url.path.removeprefix("/v1.0/me")
        self.calls.append((path, dict(request.url.params)))
        if path == "/mailFolders":
            return httpx.Response(200, json={"value": _GRAPH_FOLDERS[None]})
        if path.endswith("/childFolders"):
            parent = path.split("/")[2]
            return httpx.Response(200, json={"value": _GRAPH_FOLDERS[parent]})
        if path.startswith("/mailFolders/"):
            ident = _WELL_KNOWN_IDS.get(path.split("/")[2])
            return httpx.Response(200, json={"id": ident}) if ident else httpx.Response(404)
        assert path == "/messages", path
        return httpx.Response(200, json={"value": self.messages})


def _graph(server):
    backend = GraphBackend(GRAPH)
    backend._access_token = "token"
    backend._token_expires_at = 1 << 40
    backend._client = httpx.Client(transport=httpx.MockTransport(server))
    return backend


def _routed(account, backend, **kwargs):
    return asyncio.run(RoutedSearchEmailsTool(_STUB_CLIENT).execute_routed(account, backend, **kwargs))


def test_graph_all_scope_uses_the_mailbox_wide_collection_without_excluded_folders():
    server = _GraphServer()
    backend = _graph(server)
    try:
        response = _routed(GRAPH, backend, mode="full_text", query="report", search_scope=["all"])
    finally:
        backend.close()

    assert [(i["id"], i["folder"]) for i in response["items"]] == [
        ("m-in", "Inbox"), ("m-proj", "Inbox/Projects"), ("m-arc", "Archive"),
    ]
    assert sorted(response["folders_searched"]) == ["Archive", "Inbox", "Inbox/Projects", "Sent Items"]
    message_calls = [params for path, params in server.calls if path == "/messages"]
    assert message_calls and "parentFolderId" in message_calls[0]["$select"]
    assert message_calls[0]["$search"] == '"report"'
    # The excluded subtree is never walked; a missing Outbox is not an error.
    assert not any(path == "/mailFolders/F-DEL/childFolders" for path, _params in server.calls)
    assert any(path == "/mailFolders/outbox" for path, _params in server.calls)


def test_graph_all_scope_includes_a_named_excluded_folder_and_its_subtree():
    server = _GraphServer()
    backend = _graph(server)
    try:
        messages, folders = backend.search_all_folders(include=("trash",), limit=10)
    finally:
        backend.close()

    assert [(m.id, m.folder) for m in messages] == [
        ("m-del", "Deleted Items"), ("m-in", "Inbox"), ("m-old", "Deleted Items/Old"),
        ("m-proj", "Inbox/Projects"), ("m-arc", "Archive"),
    ]
    assert "Deleted Items/Old" in folders
    assert not any(path == "/mailFolders/deleteditems" for path, _params in server.calls)


def test_routed_all_scope_refuses_a_real_folder_beside_it():
    with pytest.raises(ValidationError, match="folder and search_scope name different folders"):
        _routed(GRAPH, _graph(_GraphServer()), query="report", search_scope=["all"], folder="Archive")


def test_routed_all_scope_lets_a_default_folder_yield():
    server = _GraphServer()
    response = _routed(GRAPH, _graph(server), query="report", search_scope=["all"], folder="inbox")

    assert "folders_searched" in response
    assert any(path == "/messages" for path, _params in server.calls)


def test_routed_all_scope_refuses_an_unknown_folder_beside_it():
    with pytest.raises(ValidationError, match="'Archive' cannot be combined with 'all'"):
        _routed(GRAPH, _graph(_GraphServer()), query="report", search_scope=["all", "Archive"])


# ==================================================================== IMAP
def test_imap_refuses_the_all_scope_clearly(monkeypatch):
    account = MailAccount(email="me@example.com", provider="imap", password="unused", settings={})
    backend = IMAPBackend(account)
    monkeypatch.setattr(backend, "_connect", lambda: pytest.fail("IMAP must not be contacted"))

    with pytest.raises(ValidationError, match=r"search_scope=\['all'\]: not supported .* IMAP"):
        _routed(account, backend, query="report", search_scope=["all"])
