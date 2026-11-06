"""JMAP mailbox backend against a scripted JMAP server (httpx MockTransport).

The fake server implements the slice of RFC 8620/8621 the backend uses:
session discovery, method calls with result references and creation ids,
Mailbox/Email/Identity/EmailSubmission methods, blob upload and download,
and the session limits. Tests drive the backend (and the routed tools) and
check what reached the server and what the caller is told.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.exceptions import ValidationError  # noqa: E402
from src.mail_backends.accounts import MailAccount, build_account  # noqa: E402
from src.mail_backends.base import MailBackendError, OutgoingAttachment, OutgoingMessage  # noqa: E402
from src.mail_backends.imap_backend import IMAPBackend  # noqa: E402
from src.mail_backends.jmap_backend import JMAPBackend  # noqa: E402
from src.mail_backends.registry import MailboxRegistry  # noqa: E402
from src.tools.graph_calendar_tools import GetCalendarTool  # noqa: E402
from src.tools.graph_contact_tools import CreateContactTool  # noqa: E402
from src.tools.mailbox_tools import (  # noqa: E402
    ListFoldersTool, ListMailboxesTool, ReplyEmailTool, SearchEmailsTool,
)
from src.utils import configure_outbound_tracking  # noqa: E402
from test_dav_calendar_contacts import DavServer  # noqa: E402

OWN = "me@example.com"
SESSION_URL = "https://jmap.example.com/.well-known/jmap"
CORE = "urn:ietf:params:jmap:core"
MAIL = "urn:ietf:params:jmap:mail"
SUBMISSION = "urn:ietf:params:jmap:submission"
STUB_CLIENT = SimpleNamespace(config=None, mailbox_registry=None)
TRACKING_HEADER = "X-Test-Tracking-ID"

MAILBOXES = [
    {"id": "mb-in", "name": "Inbox", "parentId": None, "role": "inbox"},
    {"id": "mb-proj", "name": "Projects", "parentId": "mb-in", "role": None},
    {"id": "mb-sent", "name": "Sent", "parentId": None, "role": "sent"},
    {"id": "mb-drafts", "name": "Drafts", "parentId": None, "role": "drafts"},
    {"id": "mb-trash", "name": "Trash", "parentId": None, "role": "trash"},
    {"id": "mb-old", "name": "Old", "parentId": "mb-trash", "role": None},
    {"id": "mb-junk", "name": "Spam", "parentId": None, "role": "junk"},
    {"id": "mb-arc", "name": "Archive", "parentId": None, "role": None},
]
# Header fields whose Text form RFC 8621 forbids (Stalwart rejects the request).
_STRUCTURED = {
    "from", "sender", "reply-to", "to", "cc", "bcc", "date", "message-id",
    "in-reply-to", "references",
}


class FakeJMAP:
    """An in-memory JMAP server: one account ``A1`` holding ``OWN``'s mail."""

    def __init__(self, *, max_get=500, max_calls=16, query_limit=None, submission=True,
                 identities=None):
        self.max_get = max_get
        self.max_calls = max_calls
        self.query_limit = query_limit
        self.submission = submission
        self.max_upload = 50_000_000
        self.mailboxes = [dict(m) for m in MAILBOXES]
        self.emails: dict = {}
        self.blobs: dict = {}
        self.identities = identities if identities is not None else [
            {"id": "id-me", "email": OWN, "name": "Me"},
        ]
        self.requests: list = []
        self.calls: list = []
        self.submissions: list = []
        self.set_errors: dict = {}
        self.method_errors: dict = {}
        self.reject_auth = False
        self.drop_submissions = False
        self._counter = 0

    # --- data -----------------------------------------------------------
    def new_id(self, prefix):
        self._counter += 1
        return f"{prefix}{self._counter}"

    def part(self, content_type, content, **meta):
        blob_id = self.new_id("blob-")
        self.blobs[blob_id] = content
        return {"partId": self.new_id("p"), "blobId": blob_id, "size": len(content),
                "type": content_type, **meta}

    def add_email(self, ident, mailbox="mb-in", day=1, *, keywords=None, text=None, html=None,
                  files=(), **fields):
        email = {
            "id": ident, "blobId": f"raw-{ident}", "threadId": f"t-{ident}",
            "mailboxIds": {mailbox: True}, "keywords": dict(keywords or {}), "size": 120,
            "receivedAt": f"2026-03-{day:02d}T10:00:00Z", "subject": f"subject {ident}",
            "from": [{"name": "Peer", "email": "peer@example.com"}], "to": [{"email": OWN}],
            "preview": text or "", "textBody": [], "htmlBody": [], "attachments": [],
            "bodyValues": {}, "headers": [], "messageId": [f"{ident}@example.com"],
        }
        email.update(fields)
        for key, content_type, value in (("textBody", "text/plain", text), ("htmlBody", "text/html", html)):
            if value is not None:
                part = self.part(content_type, value.encode())
                email[key].append(part)
                email["bodyValues"][part["partId"]] = {"value": value}
        for name, content_type, content, extra in files:
            email["attachments"].append(self.part(content_type, content, name=name, **extra))
        email["hasAttachment"] = bool(email["attachments"])
        self.emails[ident] = email
        self.blobs[email["blobId"]] = f"Subject: {email['subject']}\r\n\r\nraw {ident}".encode()
        return email

    def api_bodies(self):
        return [json.loads(r.content) for r in self.requests if r.url.path == "/jmap/api/"]

    def method_calls(self, name):
        return [args for called, args, _ in self.calls if called == name]

    # --- HTTP -----------------------------------------------------------
    def session(self):
        caps = {CORE: {"maxSizeUpload": self.max_upload, "maxCallsInRequest": self.max_calls,
                       "maxObjectsInGet": self.max_get, "maxObjectsInSet": 500}, MAIL: {}}
        account_caps = {MAIL: {"maxSizeAttachmentsPerEmail": 50_000_000}}
        primary = {MAIL: "A1"}
        if self.submission:
            caps[SUBMISSION] = {}
            account_caps[SUBMISSION] = {}
            primary[SUBMISSION] = "A1"
        return {
            "capabilities": caps,
            "accounts": {"A1": {"name": OWN, "isPersonal": True, "isReadOnly": False,
                                "accountCapabilities": account_caps}},
            "primaryAccounts": primary, "username": OWN,
            "apiUrl": "/jmap/api/",
            "downloadUrl": "https://jmap.example.com/jmap/download/{accountId}/{blobId}/{name}?accept={type}",
            "uploadUrl": "/jmap/upload/{accountId}/",
            "eventSourceUrl": None, "state": "st1",
        }

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == "/.well-known/jmap":
            return httpx.Response(307, headers={"Location": "/jmap/session"})
        if self.reject_auth:
            return httpx.Response(401, text="unauthorized")
        if path == "/jmap/session":
            return httpx.Response(200, json=self.session())
        if path == "/jmap/api/":
            body = json.loads(request.content)
            if self.drop_submissions and any(c[0] == "EmailSubmission/set" for c in body["methodCalls"]):
                raise httpx.ReadTimeout("timed out", request=request)
            if len(body["methodCalls"]) > self.max_calls:
                return httpx.Response(400, json={"type": "urn:ietf:params:jmap:error:limit",
                                                 "limit": "maxCallsInRequest", "status": 400})
            return httpx.Response(200, json=self.process(body))
        if path == "/jmap/upload/A1/":
            blob_id = self.new_id("up-")
            self.blobs[blob_id] = request.content
            return httpx.Response(201, json={"accountId": "A1", "blobId": blob_id,
                                             "type": request.headers["content-type"],
                                             "size": len(request.content)})
        if path.startswith("/jmap/download/A1/"):
            blob_id = unquote(path.split("/")[4])
            return httpx.Response(200, content=self.blobs[blob_id])
        return httpx.Response(404)

    # --- methods --------------------------------------------------------
    def process(self, body):
        responses: list = []
        created_ids: dict = {}
        for name, args, call_id in body["methodCalls"]:
            self.calls.append((name, copy.deepcopy(args), call_id))
            args = dict(args)
            try:
                for key in [k for k in args if k.startswith("#")]:
                    ref = args.pop(key)
                    source = next(r for r in responses if r[2] == ref["resultOf"] and r[0] == ref["name"])
                    value = source[1]
                    for token in ref["path"].strip("/").split("/"):
                        value = value[token]
                    args[key[1:]] = value
            except (StopIteration, KeyError):
                responses.append(["error", {"type": "invalidResultReference"}, call_id])
                continue
            if name in self.method_errors:
                responses.append(["error", self.method_errors[name], call_id])
                continue
            handler = {
                "Mailbox/get": self.mailbox_get, "Mailbox/query": self.mailbox_query,
                "Email/query": self.email_query, "Email/get": self.email_get,
                "Identity/get": lambda a: {"accountId": "A1", "list": self.identities},
            }.get(name)
            if handler is not None:
                responses.append([name, handler(args), call_id])
            elif name == "Email/set":
                responses.append([name, self.email_set(args, created_ids), call_id])
            elif name == "EmailSubmission/set":
                self.submission_set(args, call_id, created_ids, responses)
            else:
                responses.append(["error", {"type": "unknownMethod"}, call_id])
            if responses[-1][0] != "error" and "error" in responses[-1][1]:
                responses[-1] = ["error", responses[-1][1]["error"], call_id]
        return {"methodResponses": responses, "sessionState": "st1"}

    def mailbox_get(self, args):
        if args.get("ids") is None and len(self.mailboxes) > self.max_get:
            return {"error": {"type": "requestTooLarge"}}
        rows = []
        wanted = args.get("ids")
        for mailbox in self.mailboxes:
            if wanted is not None and mailbox["id"] not in wanted:
                continue
            members = [e for e in self.emails.values() if e["mailboxIds"].get(mailbox["id"])]
            rows.append({**mailbox, "totalEmails": len(members),
                         "unreadEmails": sum(not e["keywords"].get("$seen") for e in members)})
        return {"accountId": "A1", "list": rows, "notFound": []}

    def mailbox_query(self, args):
        ids = [m["id"] for m in self.mailboxes]
        position, limit = args.get("position", 0), args.get("limit")
        return {"ids": ids[position:position + limit if limit else None], "position": position}

    def matches(self, email, condition):
        if "operator" in condition:
            results = [self.matches(email, c) for c in condition["conditions"]]
            return {"AND": all, "OR": any}[condition["operator"]](results)
        texts = " ".join(v["value"] for v in email["bodyValues"].values())
        for key, value in condition.items():
            if key == "inMailbox":
                ok = bool(email["mailboxIds"].get(value))
            elif key == "inMailboxOtherThan":
                ok = any(m not in value for m in email["mailboxIds"])
            elif key in ("hasKeyword", "notKeyword"):
                ok = bool(email["keywords"].get(value)) == (key == "hasKeyword")
            elif key == "hasAttachment":
                ok = email["hasAttachment"] == value
            elif key == "after":
                ok = email["receivedAt"] >= value
            elif key == "before":
                ok = email["receivedAt"] < value
            elif key in ("from", "to"):
                ok = any(value.lower() in a["email"].lower() for a in email.get(key) or ())
            elif key == "subject":
                ok = value.lower() in email["subject"].lower()
            elif key in ("text", "body"):
                ok = value.lower() in (texts if key == "body" else email["subject"] + " " + texts).lower()
            else:
                raise AssertionError(f"unexpected filter {key}")
            if not ok:
                return False
        return True

    def email_query(self, args):
        rows = [e for e in self.emails.values() if self.matches(e, args.get("filter") or {})]
        rows.sort(key=lambda e: e["receivedAt"], reverse=True)
        position, limit = args.get("position", 0), args.get("limit")
        out = {"accountId": "A1", "position": position, "queryState": "q1", "canCalculateChanges": False}
        if self.query_limit and (limit is None or limit > self.query_limit):
            limit = out["limit"] = self.query_limit
        out["ids"] = [e["id"] for e in rows][position:position + limit if limit else None]
        return out

    def email_get(self, args):
        ids = args.get("ids")
        if ids is None or len(ids) > self.max_get:
            return {"error": {"type": "requestTooLarge"}}
        fetch = any(args.get(k) for k in ("fetchTextBodyValues", "fetchHTMLBodyValues", "fetchAllBodyValues"))
        for prop in args.get("properties") or ():
            # RFC 8621 4.1.2: the Text form is forbidden for address/date/id headers.
            parts = prop.split(":")
            if prop.startswith("header:") and "asText" in parts and parts[1].lower() in _STRUCTURED:
                return {"error": {"type": "invalidArguments", "description": f"Invalid property {prop}"}}
        rows, missing = [], []
        for ident in ids:
            email = self.emails.get(ident)
            if email is None:
                missing.append(ident)
                continue
            row = {"id": ident}
            for prop in args.get("properties") or list(email):
                value = email.get(prop)
                row[prop] = copy.deepcopy(value if prop != "bodyValues" or fetch else {})
            rows.append(row)
        return {"accountId": "A1", "state": "s1", "list": rows, "notFound": missing}

    def create_email(self, obj):
        if "headers" in obj:
            return None, {"type": "invalidProperties", "properties": ["headers"]}
        ident = self.new_id("E")
        email = {
            "id": ident, "blobId": f"raw-{ident}", "threadId": f"t-{ident}", "size": 300,
            "receivedAt": "2026-03-31T00:00:00Z", "mailboxIds": dict(obj.get("mailboxIds") or {}),
            "keywords": dict(obj.get("keywords") or {}), "textBody": [], "htmlBody": [],
            "attachments": [], "bodyValues": {}, "headers": [], "created": copy.deepcopy(obj),
        }
        names = {"from": "From", "to": "To", "cc": "Cc", "bcc": "Bcc", "replyTo": "Reply-To"}
        for key in ("from", "to", "cc", "bcc", "replyTo", "subject", "inReplyTo", "references", "messageId"):
            if key in obj:
                email[key] = obj[key]
        # Like a real server, the stored message has header fields for them too.
        for key, name in names.items():
            if obj.get(key):
                email["headers"].append({"name": name, "value": " " + ", ".join(a["email"] for a in obj[key])})
        if "subject" in obj:
            email["headers"].append({"name": "Subject", "value": " " + obj["subject"]})
        for key, value in obj.items():
            if key.startswith("header:"):
                name, form = key.split(":")[1], key.split(":")[-1]
                for item in value if form == "all" else [value]:
                    email["headers"].append({"name": name, "value": item})
                if name.lower() == "subject" and "subject" not in obj:
                    email["subject"] = (value[0] if form == "all" else value).strip()
        values = obj.get("bodyValues") or {}
        for key in ("textBody", "htmlBody"):
            for part in obj.get(key) or ():
                text = values[part["partId"]]["value"]
                stored = self.part(part["type"], text.encode())
                email[key].append(stored)
                email["bodyValues"][stored["partId"]] = {"value": text}
        for part in obj.get("attachments") or ():
            if part.get("blobId") not in self.blobs:
                return None, {"type": "blobNotFound", "notFound": [part.get("blobId")]}
            meta = {k: part[k] for k in ("name", "disposition", "cid") if k in part}
            email["attachments"].append(self.part(part["type"], self.blobs[part["blobId"]], **meta))
        email["hasAttachment"] = bool(email["attachments"])
        self.emails[ident] = email
        self.blobs[email["blobId"]] = b"raw created"
        return email, None

    def email_set(self, args, created_ids):
        out = {"accountId": "A1", "created": {}, "updated": {}, "destroyed": [],
               "notCreated": {}, "notUpdated": {}, "notDestroyed": {}}
        for key, obj in (args.get("create") or {}).items():
            email, error = self.create_email(obj)
            error = self.set_errors.get(("Email/set", "create")) or error
            if error:
                self.emails.pop(email["id"], None) if email else None
                out["notCreated"][key] = error
                continue
            created_ids[key] = email["id"]
            out["created"][key] = {"id": email["id"], "blobId": email["blobId"],
                                   "threadId": email["threadId"], "size": email["size"]}
        for ident, patch in (args.get("update") or {}).items():
            error = self.set_errors.get(("Email/set", "update"))
            if error or ident not in self.emails:
                out["notUpdated"][ident] = error or {"type": "notFound"}
                continue
            email = self.emails[ident]
            for path, value in patch.items():
                prop, _, key = path.partition("/")
                if not key:
                    email[prop] = value
                elif value is None:
                    email[prop].pop(key, None)
                else:
                    email[prop][key] = value
            out["updated"][ident] = None
        for ident in args.get("destroy") or ():
            error = self.set_errors.get(("Email/set", "destroy"))
            if error or ident not in self.emails:
                out["notDestroyed"][ident] = error or {"type": "notFound"}
                continue
            del self.emails[ident]
            out["destroyed"].append(ident)
        return out

    def submission_set(self, args, call_id, created_ids, responses):
        out = {"accountId": "A1", "created": {}, "notCreated": {}}
        done = []
        for key, obj in (args.get("create") or {}).items():
            email_id = obj["emailId"]
            if email_id.startswith("#"):
                email_id = created_ids.get(email_id[1:])
            identity = next((i for i in self.identities if i["id"] == obj.get("identityId")), None)
            error = self.set_errors.get(("EmailSubmission/set", "create"))
            if not error and email_id not in self.emails:
                error = {"type": "invalidProperties", "properties": ["emailId"]}
            if not error and identity is None:
                error = {"type": "invalidProperties", "properties": ["identityId"]}
            if error:
                out["notCreated"][key] = error
                continue
            submission_id = self.new_id("S")
            self.submissions.append({"id": submission_id, "identity": identity,
                                     "emailId": email_id, "envelope": obj.get("envelope")})
            out["created"][key] = {"id": submission_id, "undoStatus": "final"}
            done.append((key, email_id))
        responses.append(["EmailSubmission/set", out, call_id])
        patches = args.get("onSuccessUpdateEmail") or {}
        if patches:
            update = {email_id: patches[f"#{key}"] for key, email_id in done if f"#{key}" in patches}
            responses.append(["Email/set", self.email_set({"update": update}, created_ids), call_id])


@pytest.fixture()
def server(monkeypatch):
    fake = FakeJMAP()
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(fake), **kwargs),
    )
    return fake


def jmap_account(**settings):
    password = settings.pop("password", "secret")
    return MailAccount(email=OWN, provider="jmap", password=password,
                       settings={"session_url": SESSION_URL, **settings})


def make_backend(**settings):
    return JMAPBackend(jmap_account(**settings))


@pytest.fixture()
def backend(server):
    instance = make_backend()
    yield instance
    instance.close()


@pytest.fixture()
def tracking_on():
    previous = configure_outbound_tracking(TRACKING_HEADER)
    yield
    configure_outbound_tracking(previous)


def routed(tool_class, account, backend, **kwargs):
    return asyncio.run(tool_class(STUB_CLIENT).execute_routed(account, backend, **kwargs))


def created_object(server):
    """The Email object of the last Email/set create the server received."""
    return next(
        obj for args in reversed(server.method_calls("Email/set"))
        for obj in (args.get("create") or {}).values()
    )


# ============================================================ session/auth
def test_session_found_via_well_known_redirect_uses_basic_auth_with_jmap_username(server):
    backend = make_backend(jmap_username="login-name")
    try:
        backend.list_folders()
    finally:
        backend.close()

    paths = [r.url.path for r in server.requests]
    assert paths[:3] == ["/.well-known/jmap", "/jmap/session", "/jmap/api/"]
    api = server.requests[2]
    assert str(api.url) == "https://jmap.example.com/jmap/api/"
    expected = base64.b64encode(b"login-name:secret").decode()
    assert api.headers["authorization"] == f"Basic {expected}"


def test_api_token_is_sent_as_a_bearer_token(server):
    backend = make_backend(api_token="tok-123", password=None)
    try:
        backend.list_folders()
    finally:
        backend.close()
    assert {r.headers["authorization"] for r in server.requests[1:]} == {"Bearer tok-123"}


def test_rejected_credentials_name_the_settings_to_fix(server, backend):
    server.reject_auth = True
    with pytest.raises(MailBackendError, match="api_token"):
        backend.list_folders()
    assert backend.test_connection()["connected"] is False


# ================================================================ folders
def test_folders_map_roles_paths_counts_and_name_hints(server, backend):
    server.add_email("m1", "mb-proj")
    server.add_email("m2", "mb-proj", keywords={"$seen": True})

    folders = {f.path: f for f in backend.list_folders()}

    assert folders["Inbox"].well_known == "inbox"
    assert folders["Inbox/Projects"].name == "Projects"
    assert (folders["Inbox/Projects"].total_count, folders["Inbox/Projects"].unread_count) == (2, 1)
    assert folders["Spam"].well_known == "junk"
    assert folders["Trash/Old"].well_known is None
    # No role on "Archive": the IMAP name hints still recognise it.
    assert folders["Archive"].well_known == "archive"
    # Folder names and paths resolve to the mailbox id on the wire.
    backend.list_messages("Inbox/Projects")
    backend.list_messages("projects")
    queries = server.method_calls("Email/query")
    assert [q["filter"] for q in queries] == [{"inMailbox": "mb-proj"}] * 2


def test_unknown_folder_lists_what_exists_after_a_fresh_look(server, backend):
    with pytest.raises(MailBackendError, match=r"folder 'Nope' not found.*Inbox/Projects"):
        backend.list_messages("Nope")
    assert len(server.method_calls("Mailbox/get")) == 2
    assert not server.method_calls("Email/query")


# ======================================================== listing/limits
def test_listing_is_newest_first_paged_and_filters_unread_in_one_request(server, backend):
    server.add_email("old", day=1)
    server.add_email("read", day=3, keywords={"$seen": True, "$flagged": True})
    server.add_email("new", day=4, keywords={"$flagged": True})
    server.add_email("mid", day=2)
    backend.list_folders()
    requests_before = len(server.api_bodies())

    page = backend.list_messages("inbox", limit=2, offset=1, unread_only=True)

    bodies = server.api_bodies()[requests_before:]
    assert len(bodies) == 1
    (query, query_args, _), (get, get_args, _) = bodies[0]["methodCalls"]
    assert (query, get) == ("Email/query", "Email/get")
    assert query_args["filter"] == {"operator": "AND", "conditions": [
        {"inMailbox": "mb-in"}, {"notKeyword": "$seen"},
    ]}
    assert query_args["sort"] == [{"property": "receivedAt", "isAscending": False}]
    assert (query_args["position"], query_args["limit"]) == (1, 2)
    assert get_args["#ids"] == {"resultOf": "q", "name": "Email/query", "path": "/ids"}
    assert [m.id for m in page] == ["mid", "old"]
    assert page[0].folder == "inbox" and page[0].is_read is False
    assert page[0].internet_message_id == "<mid@example.com>"

    newest = backend.list_messages("inbox", limit=1)[0]
    assert (newest.id, newest.is_flagged, newest.flag_status) == ("new", True, "Flagged")


@pytest.mark.parametrize("max_get, query_limit, limits", [
    (2, None, [2, 2, 1]),
    (500, 2, [5, 3, 1]),
])
def test_pages_stay_within_server_limits_and_still_return_everything(
    monkeypatch, max_get, query_limit, limits,
):
    fake = FakeJMAP(max_get=max_get, query_limit=query_limit)
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(fake), **kw))
    for day in range(1, 6):
        fake.add_email(f"m{day}", day=day)
    backend = make_backend()
    try:
        backend.list_folders()
        messages = backend.list_messages("inbox", limit=5)
    finally:
        backend.close()

    assert [m.id for m in messages] == ["m5", "m4", "m3", "m2", "m1"]
    queries = fake.method_calls("Email/query")
    assert [q["limit"] for q in queries] == limits
    assert [q["position"] for q in queries] == [0, 2, 4]


def test_a_request_over_max_calls_is_refused_before_it_is_sent(monkeypatch):
    fake = FakeJMAP(max_calls=1)
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(fake), **kw))
    backend = make_backend()
    try:
        backend.list_folders()
        with pytest.raises(MailBackendError, match="maxCallsInRequest"):
            backend.list_messages("inbox")
    finally:
        backend.close()
    assert not fake.method_calls("Email/query")


# ================================================================= search
def test_routed_search_puts_every_filter_on_the_server_query(server, backend):
    server.add_email(
        "hit", "mb-arc", day=5, keywords={"$seen": True, "$flagged": True}, text="budget report",
        to=[{"email": "peer@example.com"}], files=[("a.pdf", "application/pdf", b"%PDF", {})],
    )
    server.add_email("miss", "mb-arc", day=6, text="budget report")

    response = routed(
        SearchEmailsTool, jmap_account(), backend, query="report",
        from_address="peer@example.com", to_address="peer@example.com", body_contains="budget",
        is_read=True, flag_status="Flagged", has_attachments=True,
        start_date="2026-03-01", end_date="2026-03-10T12:00:00+02:00", folder="Archive",
    )

    assert [item["id"] for item in response["items"]] == ["hit"]
    [query] = server.method_calls("Email/query")
    assert query["filter"] == {"operator": "AND", "conditions": [
        {"inMailbox": "mb-arc"}, {"text": "report"}, {"from": "peer@example.com"},
        {"to": "peer@example.com"}, {"body": "budget"},
        {"after": "2026-03-01T00:00:00Z"}, {"before": "2026-03-10T10:00:00Z"},
        {"hasKeyword": "$seen"}, {"hasKeyword": "$flagged"}, {"hasAttachment": True},
    ]}


def test_recipient_alias_and_unread_search_reach_the_server(server, backend):
    routed(SearchEmailsTool, jmap_account(), backend, recipient="team@example.com", is_read=False)
    [query] = server.method_calls("Email/query")
    assert query["filter"]["conditions"][1:] == [{"to": "team@example.com"}, {"notKeyword": "$seen"}]


def test_complete_flag_status_is_refused_for_jmap(server, backend):
    with pytest.raises(ValidationError, match="can only filter on NotFlagged or Flagged"):
        routed(SearchEmailsTool, jmap_account(), backend, flag_status="Complete")
    assert not server.method_calls("Email/query")


@pytest.mark.parametrize("params, message", [
    ({"body_contains": "budget"}, "body_contains: not supported .*Exchange-only"),
    ({"to_address": "peer@example.com"}, "to_address: not supported .*Exchange-only"),
    ({"is_read": True}, "is_read=true: not supported"),
])
def test_imap_still_refuses_the_filters_only_jmap_serves(monkeypatch, params, message):
    account = MailAccount(email=OWN, provider="imap", password="unused", settings={})
    imap = IMAPBackend(account)
    monkeypatch.setattr(imap, "_connect", lambda: pytest.fail("IMAP must not be contacted"))
    with pytest.raises(ValidationError, match=message):
        routed(SearchEmailsTool, account, imap, **params)


def test_all_scope_is_one_query_without_excluded_subtrees(server, backend):
    for ident, mailbox, day in (
        ("in", "mb-in", 9), ("proj", "mb-proj", 8), ("trash", "mb-trash", 7), ("old", "mb-old", 6),
        ("junk", "mb-junk", 5), ("arc", "mb-arc", 4), ("draft", "mb-drafts", 3), ("sent", "mb-sent", 2),
    ):
        server.add_email(ident, mailbox, day=day, text="report")

    response = routed(SearchEmailsTool, jmap_account(), backend, mode="full_text",
                      query="report", search_scope=["all"])

    assert [(i["id"], i["folder"]) for i in response["items"]] == [
        ("in", "Inbox"), ("proj", "Inbox/Projects"), ("arc", "Archive"), ("sent", "Sent"),
    ]
    assert sorted(response["folders_searched"]) == ["Archive", "Inbox", "Inbox/Projects", "Sent"]
    [query] = server.method_calls("Email/query")
    assert query["filter"]["conditions"][0] == {
        "inMailboxOtherThan": ["mb-drafts", "mb-junk", "mb-old", "mb-trash"],
    }

    messages, folders = backend.search_all_folders(include=("trash",), query="report")
    assert [(m.id, m.folder) for m in messages][:4] == [
        ("in", "Inbox"), ("proj", "Inbox/Projects"), ("trash", "Trash"), ("old", "Trash/Old"),
    ]
    assert "Trash/Old" in folders and "Spam" not in folders


# ================================================================ reading
def test_details_render_both_bodies_list_every_file_and_name_the_real_folder(server, backend):
    email = server.add_email(
        "m1", "mb-arc", text="plain words", html="<p>html <b>words</b></p>",
        files=[("report.pdf", "application/pdf", b"%PDF-1", {"disposition": "attachment"})],
    )
    # Inline media in a mixed body: JMAP lists it among the body parts only.
    email["htmlBody"].append(server.part("image/png", b"PNG", name="logo.png",
                                         disposition="inline", cid="logo@x"))

    message = backend.get_message("m1", "inbox", "html")

    assert message.folder == "Archive"
    assert "<b>words</b>" in message.body and message.body_format == "html"
    assert message.preview == "plain words"
    names = {a.name: a for a in message.attachments}
    assert set(names) == {"report.pdf", "logo.png"}
    assert names["logo.png"].is_inline and names["logo.png"].content_id == "logo@x"
    [get] = server.method_calls("Email/get")[-1:]
    assert get["fetchTextBodyValues"] and get["fetchHTMLBodyValues"]
    assert backend.get_message("m1", "Archive", "text").folder == "Archive"


def test_attachments_and_mime_download_through_the_download_url(server, backend):
    server.add_email("m1", files=[("report.pdf", "application/pdf", b"%PDF-1 bytes", {})])

    content = backend.get_attachment("m1", attachment_name="REPORT.pdf")
    mime = backend.get_mime("m1")

    assert content.content == b"%PDF-1 bytes" and content.content_type == "application/pdf"
    downloads = [r.url for r in server.requests if r.url.path.startswith("/jmap/download/")]
    blob_id = server.emails["m1"]["attachments"][0]["blobId"]
    assert downloads[0].path == f"/jmap/download/A1/{blob_id}/report.pdf"
    assert downloads[0].params["accept"] == "application/pdf"
    assert mime == server.blobs["raw-m1"]
    with pytest.raises(MailBackendError, match="not found; attachments on this message: report.pdf"):
        backend.get_attachment("m1", attachment_name="other.pdf")


def test_an_unknown_id_says_ids_are_mailbox_wide(server, backend):
    with pytest.raises(MailBackendError, match="not found .*mailbox-wide"):
        backend.get_message("gone")


# ============================================================== mutations
def test_mark_sets_and_clears_keywords(server, backend):
    server.add_email("m1", keywords={"$flagged": True})

    result = backend.mark_message("m1", is_read=True, is_flagged=False)

    [update] = server.method_calls("Email/set")
    assert update["update"] == {"m1": {"keywords/$seen": True, "keywords/$flagged": None}}
    assert server.emails["m1"]["keywords"] == {"$seen": True}
    assert (result["is_read"], result["is_flagged"], result["changed"]) == (True, False, True)


def test_move_keeps_the_id_and_leaves_only_the_destination(server, backend):
    server.add_email("m1", "mb-in")
    server.emails["m1"]["mailboxIds"]["mb-proj"] = True

    result = backend.move_message("m1", "archive", "inbox")

    assert server.emails["m1"]["mailboxIds"] == {"mb-arc": True}
    assert (result["id"], result["folder"]) == ("m1", "archive")


def test_delete_moves_to_trash_and_destroys_what_is_already_there(server, backend):
    server.add_email("m1")
    server.add_email("m2")

    soft = backend.delete_message("m1", "inbox")
    assert server.emails["m1"]["mailboxIds"] == {"mb-trash": True}
    assert (soft["hard_delete"], soft["id"], soft["folder"]) == (False, "m1", "trash")

    # The caller's folder is stale: the message is in Trash, so it is destroyed.
    again = backend.delete_message("m1", "inbox")
    assert again["hard_delete"] is True and "m1" not in server.emails

    hard = backend.delete_message("m2", "inbox", hard_delete=True)
    assert hard["hard_delete"] is True and "m2" not in server.emails


def test_set_errors_and_method_errors_carry_the_server_description(server, backend):
    server.add_email("m1")
    server.set_errors[("Email/set", "update")] = {
        "type": "forbidden", "description": "mailbox is read-only",
    }
    with pytest.raises(MailBackendError, match="could not mark message 'm1': forbidden: mailbox is read-only"):
        backend.mark_message("m1", is_read=True)

    server.method_errors["Email/query"] = {"type": "unsupportedFilter", "description": "no body search"}
    with pytest.raises(MailBackendError, match="Email/query failed: unsupportedFilter: no body search"):
        backend.search_messages("inbox", body="x")


# ================================================================ sending
def test_send_creates_the_draft_and_submits_it_in_one_request(server, backend, tracking_on):
    result = backend.send_message(OutgoingMessage(
        to=["peer@example.com"], cc=["copy@example.com"], bcc=["hidden@example.com", "PEER@example.com"],
        subject="Plan", body="**hello**", tracking_id="track-1",
    ))

    [body] = [b for b in server.api_bodies() if any(c[0] == "EmailSubmission/set" for c in b["methodCalls"])]
    (create, create_args, _), (submit, submit_args, _) = body["methodCalls"]
    assert (create, submit) == ("Email/set", "EmailSubmission/set")
    email = create_args["create"]["draft"]
    assert email["mailboxIds"] == {"mb-drafts": True}
    assert email["keywords"] == {"$draft": True, "$seen": True}
    assert email["from"] == [{"email": OWN}]
    assert email["bcc"] == [{"email": "hidden@example.com"}, {"email": "PEER@example.com"}]
    assert email[f"header:{TRACKING_HEADER}:asText"] == "track-1"
    assert email["bodyValues"]["text"]["value"] == "**hello**"
    assert "<strong>hello</strong>" in email["bodyValues"]["html"]["value"]
    creation = submit_args["create"]["send"]
    assert creation["emailId"] == "#draft" and creation["identityId"] == "id-me"
    assert [r["email"] for r in creation["envelope"]["rcptTo"]] == [
        "peer@example.com", "copy@example.com", "hidden@example.com",
    ]
    assert submit_args["onSuccessUpdateEmail"] == {
        "#send": {"keywords/$draft": None, "mailboxIds": {"mb-sent": True}},
    }
    [sent] = [e for e in server.emails.values() if e.get("subject") == "Plan"]
    assert sent["mailboxIds"] == {"mb-sent": True} and "$draft" not in sent["keywords"]
    assert result["accepted_by_mail_server"] is True and result["delivery_confirmed"] is False
    assert result["saved_to_sent"] == "Sent" and result["tracking_id"] == "track-1"
    assert result["sent_copy_id"] == sent["id"]


@pytest.mark.parametrize("identities, chosen", [
    ([{"id": "alias", "email": "alias@example.com"}, {"id": "wild", "email": "*@example.com"},
      {"id": "exact", "email": "ME@example.com"}], "exact"),
    ([{"id": "other", "email": "me@other.example"}, {"id": "wild", "email": "*@example.com"}], "wild"),
])
def test_the_identity_matching_the_mailbox_address_sends(server, backend, identities, chosen):
    server.identities = identities
    backend.send_message(OutgoingMessage(to=["peer@example.com"], subject="s", body="b"))
    assert server.submissions[0]["identity"]["id"] == chosen


def test_no_matching_identity_refuses_before_creating_anything(server, backend):
    server.identities = [{"id": "other", "email": "someone@other.example"}]
    with pytest.raises(MailBackendError, match="no JMAP sending identity .*someone@other.example"):
        backend.send_message(OutgoingMessage(
            to=["peer@example.com"], subject="s", body="b",
            attachments=[OutgoingAttachment(name="a.txt", content=b"A")],
        ))
    assert not server.method_calls("Email/set") and not server.emails
    assert not [r for r in server.requests if r.url.path.startswith("/jmap/upload/")]


@pytest.mark.parametrize("cleanup", [False, True])
def test_a_refused_submission_reports_the_reason_and_the_draft(server, backend, cleanup):
    server.set_errors[("EmailSubmission/set", "create")] = {
        "type": "forbiddenFrom", "description": "not allowed to send as this address",
    }
    with pytest.raises(MailBackendError, match="forbiddenFrom: not allowed to send as") as raised:
        backend.send_message(OutgoingMessage(
            to=["peer@example.com"], subject="s", body="b", cleanup_on_failure=cleanup,
        ))

    details = raised.value.details
    assert details["accepted_by_mail_server"] is False and details["sent"] is False
    assert details["draft_retained"] is (not cleanup)
    assert details["draft_id"]
    assert (details["draft_id"] in server.emails) is (not cleanup)


def test_a_lost_submission_response_is_reported_as_uncertain(server, backend):
    server.drop_submissions = True
    with pytest.raises(MailBackendError, match="Check Sent and Drafts") as raised:
        backend.send_message(OutgoingMessage(
            to=["peer@example.com"], subject="s", body="b", cleanup_on_failure=True,
        ))
    details = raised.value.details
    assert details["submission_uncertain"] is True
    assert details["accepted_by_mail_server"] is None
    assert details["cleanup"]["attempted"] is False


def test_a_file_over_the_upload_limit_is_refused_before_anything_is_created(server, backend):
    server.max_upload = 4
    with pytest.raises(MailBackendError, match="5 bytes exceeds .*limit of 4 bytes"):
        backend.send_message(OutgoingMessage(
            to=["peer@example.com"], subject="s", body="b",
            attachments=[OutgoingAttachment(name="big.bin", content=b"12345")],
        ))
    assert not [r for r in server.requests if r.url.path.startswith("/jmap/upload/")]
    assert not server.method_calls("Email/set") and not server.submissions


def test_without_submission_sending_is_refused_and_nothing_is_created(monkeypatch):
    fake = FakeJMAP(submission=False)
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(fake), **kw))
    backend = make_backend()
    try:
        with pytest.raises(MailBackendError, match="jmap backend cannot send mail"):
            backend.send_message(OutgoingMessage(to=["peer@example.com"], subject="s", body="b"))
        assert backend.create_draft(OutgoingMessage(to=["peer@example.com"], subject="s", body="b"))["id"]
    finally:
        backend.close()
    assert len(fake.method_calls("Email/set")) == 1


def _original(server, **fields):
    return server.add_email(
        "orig", "mb-in", text="Original body", subject="Plan",
        **{"from": [{"name": "Jörg Author", "email": "author@example.com"}],
           "to": [{"email": OWN}, {"email": "peer@example.com"}],
           "cc": [{"email": "copied@example.com"}],
           "references": ["root@example.com"],
           # Raw forms, as the server returns them: folded and RFC 2047 encoded.
           "header:From": " =?utf-8?q?J=C3=B6rg_Author?=\r\n <author@example.com>",
           "header:Date": " Mon, 2 Mar 2026 10:00:00 +0000",
           "header:Subject": " Plan", **fields},
        files=[("plan.pdf", "application/pdf", b"%PDF-plan", {})],
    )


def test_reply_all_follows_the_imap_rules_and_threads(server, backend):
    _original(server)

    response = routed(
        ReplyEmailTool, jmap_account(), backend, message_id="orig", body="Agreed", reply_all=True,
        cc=["extra@example.com", "AUTHOR@example.com"], bcc=["hidden@example.com"],
    )

    email = created_object(server)
    assert [a["email"] for a in email["to"]] == ["author@example.com", "peer@example.com"]
    assert [a["email"] for a in email["cc"]] == ["extra@example.com", "copied@example.com"]
    assert [a["email"] for a in email["bcc"]] == ["hidden@example.com"]
    assert email["subject"] == "Re: Plan"
    assert email["inReplyTo"] == ["orig@example.com"]
    assert email["references"] == ["root@example.com", "orig@example.com"]
    text = email["bodyValues"]["text"]["value"]
    assert "On Mon, 2 Mar 2026 10:00:00 +0000, Jörg Author <author@example.com> wrote:" in text
    assert "> Original body" in text
    assert "attachments" not in email
    assert response["to"] == ["author@example.com", "peer@example.com"]
    assert response["bcc"] == ["hidden@example.com"]
    assert response["accepted_by_mail_server"] is True and response["delivery_confirmed"] is False


def test_a_plain_reply_answers_reply_to(server, backend):
    _original(server, replyTo=[{"email": "list@example.com"}])
    backend.create_reply_draft("orig", OutgoingMessage(to=[], body="Thanks"))
    email = created_object(server)
    assert [a["email"] for a in email["to"]] == ["list@example.com"]
    assert "cc" not in email


def test_forward_carries_original_files_by_blob_and_new_files_by_upload(server, backend):
    original = _original(server)

    result = backend.forward_message("orig", OutgoingMessage(
        to=["third@example.com"], body="FYI",
        attachments=[OutgoingAttachment(name="notes.txt", content=b"notes", content_type="text/plain")],
    ))

    email = created_object(server)
    assert email["subject"] == "Fwd: Plan"
    text = email["bodyValues"]["text"]["value"]
    assert text.startswith(
        "FYI\n\n---------- Forwarded message ----------\nFrom: Jörg Author <author@example.com>\n"
        "Date: Mon, 2 Mar 2026 10:00:00 +0000\nSubject: Plan\n\n"
    )
    assert text.endswith("Original body")
    [upload] = [r for r in server.requests if r.url.path == "/jmap/upload/A1/"]
    assert upload.content == b"notes" and upload.headers["content-type"] == "text/plain"
    uploaded = [blob for blob, content in server.blobs.items() if blob.startswith("up-")]
    assert [(p["name"], p["blobId"]) for p in email["attachments"]] == [
        ("plan.pdf", original["attachments"][0]["blobId"]),
        ("notes.txt", uploaded[0]),
    ]
    assert result["forwarded_from"] == "orig" and result["to"] == ["third@example.com"]


# ================================================================= drafts
def test_create_draft_returns_its_identity_and_files(server, backend):
    result = backend.create_draft(OutgoingMessage(
        to=["peer@example.com"], subject="Draft", body="hi",
        attachments=[OutgoingAttachment(name="a.txt", content=b"A", content_type="text/plain")],
    ))

    stored = server.emails[result["id"]]
    assert stored["mailboxIds"] == {"mb-drafts": True} and stored["keywords"]["$draft"]
    assert result["folder"] == "drafts" and result["sent"] is False
    assert [a["name"] for a in result["attachments"]] == ["a.txt"]
    assert result["attachments"][0]["attachment_id"] == stored["attachments"][0]["partId"]


def _stored_draft(server):
    return server.add_email(
        "d1", "mb-drafts", keywords={"$draft": True, "$seen": True}, text="old text",
        html="<p>old text</p>", subject="Old subject",
        files=[("keep.pdf", "application/pdf", b"%PDF-keep", {"disposition": "attachment"})],
        headers=[
            {"name": "From", "value": " Me <me@example.com>"},
            {"name": "To", "value": " peer@example.com"},
            {"name": "Subject", "value": " Old subject"},
            {"name": "Message-ID", "value": " <d1@example.com>"},
            {"name": "In-Reply-To", "value": " <root@example.com>"},
            {"name": TRACKING_HEADER, "value": " track-9"},
            {"name": "MIME-Version", "value": " 1.0"},
            {"name": "Content-Type", "value": " multipart/mixed; boundary=x"},
        ],
    )


def test_update_draft_replaces_the_immutable_email_keeping_everything_else(server, backend):
    old = _stored_draft(server)

    result = backend.update_draft("d1", subject="New subject")

    new = server.emails[result["id"]]
    assert result["previous_id"] == "d1" and result["id"] != "d1"
    assert "d1" not in server.emails and result["original_removed"] is True
    created = new["created"]
    assert created["subject"] == "New subject"
    assert "header:Subject:all" not in created
    assert created["header:Message-ID:all"] == [" <d1@example.com>"]
    assert created["header:In-Reply-To:all"] == [" <root@example.com>"]
    assert created[f"header:{TRACKING_HEADER}:all"] == [" track-9"]
    assert not any(k.lower().startswith(("header:content-", "header:mime-")) for k in created)
    assert created["keywords"] == {"$draft": True, "$seen": True}
    assert created["bodyValues"] == {"text": {"value": "old text"}, "html": {"value": "<p>old text</p>"}}
    assert created["attachments"][0]["blobId"] == old["attachments"][0]["blobId"]
    assert [a["name"] for a in result["attachments"]] == ["keep.pdf"]

    again = backend.update_draft(result["id"], body="", body_format="text")
    created = server.emails[again["id"]]["created"]
    assert created["bodyValues"] == {"text": {"value": ""}, "html": {"value": ""}}
    assert created["header:Subject:all"] == [" New subject"]


def test_update_refuses_a_non_draft_without_creating_anything(server, backend):
    server.add_email("m1")
    with pytest.raises(MailBackendError, match=r"update_draft requires a saved draft .*\$draft"):
        backend.update_draft("m1", subject="x")
    assert not server.method_calls("Email/set")


def test_the_replacement_id_survives_a_failed_cleanup_of_the_original(server, backend):
    _stored_draft(server)
    server.set_errors[("Email/set", "destroy")] = {"type": "forbidden", "description": "locked"}

    result = backend.update_draft("d1", subject="New")

    assert result["id"] in server.emails and "d1" in server.emails
    assert result["original_removed"] is False
    assert "do not repeat the mutation" in result["warnings"][0]


def test_add_and_delete_attachment_rebuild_the_draft_by_blob(server, backend):
    old = _stored_draft(server)

    added = backend.add_attachment("d1", "logo.png", b"PNG", content_type="image/png",
                                   is_inline=True, content_id="<logo@x>")
    new = server.emails[added["id"]]
    assert [p["name"] for p in new["attachments"]] == ["keep.pdf", "logo.png"]
    assert new["created"]["attachments"][0]["blobId"] == old["attachments"][0]["blobId"]
    assert new["created"]["attachments"][1]["cid"] == "logo@x"
    assert added["attachment_id"] == new["attachments"][1]["partId"]
    assert added["content_id"] == "logo@x" and added["previous_id"] == "d1"

    removed = backend.delete_attachment(added["id"], attachment_name="keep.pdf")
    newest = server.emails[removed["id"]]
    assert [p["name"] for p in newest["attachments"]] == ["logo.png"]
    assert removed["removed_attachment_id"] == new["attachments"][0]["partId"]
    assert added["id"] not in server.emails


def test_attachment_changes_on_delivered_mail_are_refused_before_uploading(server, backend):
    server.add_email("m1")
    with pytest.raises(MailBackendError, match="add attachments requires a saved draft"):
        backend.add_attachment("m1", "a.txt", b"A")
    assert not [r for r in server.requests if r.url.path.startswith("/jmap/upload/")]


def test_send_draft_submits_the_stored_draft_as_its_from_identity(server, backend):
    server.identities = [{"id": "id-me", "email": OWN}, {"id": "id-alias", "email": "alias@example.com"}]
    server.add_email(
        "d1", "mb-drafts", keywords={"$draft": True}, text="ready", subject="Ready",
        to=[{"email": "peer@example.com"}], bcc=[{"email": "hidden@example.com"}],
        **{"from": [{"email": "alias@example.com"}]},
    )

    preview = backend.send_draft("d1", dry_run=True)
    assert preview["would_send_to"] == ["peer@example.com"] and preview["would_bcc"] == ["hidden@example.com"]
    assert preview["body_preview"] == "ready" and not server.submissions

    result = backend.send_draft("d1")
    [submission] = server.submissions
    assert submission["identity"]["id"] == "id-alias" and submission["emailId"] == "d1"
    assert submission["envelope"]["mailFrom"] == {"email": "alias@example.com"}
    assert server.emails["d1"]["mailboxIds"] == {"mb-sent": True}
    assert result["sent"] is True and result["draft_removed"] is True
    assert result["saved_to_sent"] == "Sent" and result["delivery_confirmed"] is False


# ============================================================= tool layer
def test_a_mail_accounts_entry_serves_the_routed_tools(server, monkeypatch):
    for name in ("ICLOUD_EMAIL", "GMAIL_EMAIL", "OUTLOOK_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MAIL_ACCOUNTS", json.dumps([
        {"email": OWN, "provider": "jmap", "session_url": SESSION_URL, "password": "secret"},
        {"email": "fm@example.com", "provider": "fastmail", "api_token": "fm-token"},
    ]))
    registry = MailboxRegistry(SimpleNamespace(ews_email="", mail_account_rows=[]), None)
    tool = ListFoldersTool(SimpleNamespace(config=None, mailbox_registry=registry))
    try:
        listed = asyncio.run(tool.execute(target_mailbox=OWN))
        fastmail = asyncio.run(tool.execute(target_mailbox="fm@example.com"))
    finally:
        registry.close()

    assert listed["success"] is True and listed["backend"] == "jmap"
    assert {f["path"] for f in listed["folders"]} >= {"Inbox", "Inbox/Projects", "Sent"}
    assert fastmail["success"] is True
    fastmail_requests = [r for r in server.requests if r.url.host == "api.fastmail.com"]
    assert fastmail_requests[0].url.path == "/jmap/session"
    assert fastmail_requests[0].headers["authorization"] == "Bearer fm-token"


@pytest.mark.parametrize("settings, dav", [({}, False), ({"caldav_url": "https://dav.example.com/"}, True)])
def test_list_mailboxes_reports_jmap_capabilities(settings, dav):
    account = build_account({"email": OWN, "provider": "stalwart", "session_url": SESSION_URL,
                             "password": "pw", **settings})
    registry = SimpleNamespace(
        describe_accounts=lambda: [account.describe()], default_email=OWN,
        find_account=lambda address: account,
    )
    result = asyncio.run(ListMailboxesTool(SimpleNamespace(mailbox_registry=registry, config=None)).execute())
    entry = result["mailboxes"][0]
    assert entry["backend"] == "jmap" and entry["auth"] == "basic"
    caps = entry["capabilities"]
    assert caps["folder_scoped_ids"] is False
    assert caps["calendar"] is dav and caps["contacts"] is False
    assert "mailbox-wide" in entry["capability_hints"]["ids"]


def test_calendar_without_caldav_is_refused_like_imap(server, backend):
    with pytest.raises(MailBackendError, match=f"CalDAV is not configured for {OWN}"):
        routed(GetCalendarTool, jmap_account(), backend)


def test_dav_signs_in_with_the_jmap_username_and_password(monkeypatch):
    dav = DavServer()
    seen: list = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return dav.handler(request)

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    account = jmap_account(jmap_username="login-name", carddav_url="https://dav.test/")
    backend = JMAPBackend(account)
    try:
        created = routed(CreateContactTool, account, backend, given_name="Ada", surname="Lovelace",
                         email_address="ada@example.com")
    finally:
        backend.close()
    assert created["item_id"].startswith("/u/addressbooks/card/")
    assert set(seen) == {"Basic " + base64.b64encode(b"login-name:secret").decode()}
