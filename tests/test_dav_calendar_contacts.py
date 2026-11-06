"""iCloud CalDAV/CardDAV support behind the unified tool names."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.mail_backends.accounts import MailAccount, accounts_from_env
from src.mail_backends.imap_backend import IMAPBackend
from src.tools.graph_calendar_tools import CreateAppointmentTool, GetCalendarTool, UpdateAppointmentTool, DeleteAppointmentTool
from src.tools.graph_contact_tools import CreateContactTool, FindPersonTool, UpdateContactTool, DeleteContactTool
from src.tools.mailbox_tools import ListMailboxesTool

DAV = "DAV:"
CAL = "urn:ietf:params:xml:ns:caldav"
CARD = "urn:ietf:params:xml:ns:carddav"


def multistatus(*responses: str) -> str:
    return f'<?xml version="1.0" encoding="utf-8"?><d:multistatus xmlns:d="{DAV}" xmlns:c="{CAL}" xmlns:card="{CARD}">' + "".join(responses) + "</d:multistatus>"


def response_xml(href: str, *, etag: str | None = None, calendar_data: str | None = None, address_data: str | None = None, display_name: str | None = None, resource: str | None = None) -> str:
    props = [f"<d:href>{href}</d:href><d:propstat><d:prop>"]
    if etag:
        props.append(f"<d:getetag>{etag}</d:getetag>")
    if calendar_data:
        props.append(f"<c:calendar-data>{calendar_data}</c:calendar-data>")
    if address_data:
        props.append(f"<card:address-data>{address_data}</card:address-data>")
    if display_name:
        props.append(f"<d:displayname>{display_name}</d:displayname>")
    if resource:
        props.append(f"<d:resourcetype><d:collection/><{resource}/></d:resourcetype>")
    props.append("</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>")
    return "<d:response>" + "".join(props) + "</d:response>"


class DavServer:
    def __init__(self):
        self.events: dict[str, tuple[str, str]] = {}
        self.contacts: dict[str, tuple[str, str]] = {}
        self.requests: list[tuple[str, str, str | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        self.requests.append((method, path, request.headers.get("if-match")))
        if method == "PROPFIND" and path == "/":
            return httpx.Response(207, text=multistatus('<d:response><d:href>/</d:href><d:propstat><d:prop><d:current-user-principal><d:href>/u/principal/</d:href></d:current-user-principal></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'))
        if method == "PROPFIND" and path == "/u/principal/":
            return httpx.Response(207, text=multistatus('<d:response><d:href>/u/principal/</d:href><d:propstat><d:prop><c:calendar-home-set><d:href>/u/calendars/</d:href></c:calendar-home-set><card:addressbook-home-set><d:href>/u/addressbooks/</d:href></card:addressbook-home-set></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'))
        if method == "PROPFIND" and path == "/u/calendars/":
            return httpx.Response(207, text=multistatus(response_xml("/u/calendars/home/", display_name="Home", resource="c:calendar")))
        if method == "PROPFIND" and path == "/u/addressbooks/":
            return httpx.Response(207, text=multistatus(response_xml("/u/addressbooks/card/", display_name="Contacts", resource="card:addressbook")))
        if method == "PUT" and path.startswith("/u/calendars/home/"):
            self.events[path] = (request.content.decode(), '"ev2"' if path in self.events else '"ev1"')
            return httpx.Response(201 if len(self.events) == 1 else 204, headers={"ETag": self.events[path][1]})
        if method == "PUT" and path.startswith("/u/addressbooks/card/"):
            self.contacts[path] = (request.content.decode(), '"ct2"' if path in self.contacts else '"ct1"')
            return httpx.Response(201 if len(self.contacts) == 1 else 204, headers={"ETag": self.contacts[path][1]})
        if method == "REPORT" and path == "/u/calendars/home/":
            rows = [response_xml(href, etag=etag, calendar_data=body) for href, (body, etag) in self.events.items()]
            return httpx.Response(207, text=multistatus(*rows))
        if method == "REPORT" and path == "/u/addressbooks/card/":
            rows = [response_xml(href, etag=etag, address_data=body) for href, (body, etag) in self.contacts.items()]
            return httpx.Response(207, text=multistatus(*rows))
        if method == "GET" and path in self.events:
            body, etag = self.events[path]
            return httpx.Response(200, text=body, headers={"ETag": etag})
        if method == "GET" and path in self.contacts:
            body, etag = self.contacts[path]
            return httpx.Response(200, text=body, headers={"ETag": etag})
        if method == "DELETE" and path in self.events:
            del self.events[path]
            return httpx.Response(204)
        if method == "DELETE" and path in self.contacts:
            del self.contacts[path]
            return httpx.Response(204)
        return httpx.Response(500, text=f"unexpected {method} {path}")


def make_client(server: DavServer):
    account = MailAccount(
        email="user@icloud.com",
        provider="icloud",
        password="app-pass",
        settings={"caldav_url": "https://dav.test/", "carddav_url": "https://dav.test/"},
    )
    backend = IMAPBackend(account)
    backend._dav_http = httpx.Client(transport=httpx.MockTransport(server.handler), base_url="https://dav.test")
    registry = SimpleNamespace(
        describe_accounts=lambda: [account.describe()],
        default_email=account.email,
        find_account=lambda address: account,
        get_backend=lambda address: backend,
    )
    return SimpleNamespace(mailbox_registry=registry, config=None), backend


def invoke(tool_class, client, **kwargs):
    return asyncio.run(tool_class(client).execute(target_mailbox="user@icloud.com", **kwargs))


def test_icloud_env_accepts_dav_overrides():
    [account] = accounts_from_env({
        "ICLOUD_EMAIL": "user@icloud.com",
        "ICLOUD_APP_PASSWORD": "pw",
        "ICLOUD_CALDAV_URL": "https://cal.example/",
        "ICLOUD_CARDDAV_URL": "https://card.example/",
    })
    assert account.settings["caldav_url"] == "https://cal.example/"
    assert account.settings["carddav_url"] == "https://card.example/"


def test_list_mailboxes_reports_icloud_calendar_contacts_capable():
    server = DavServer()
    client, backend = make_client(server)
    try:
        result = asyncio.run(ListMailboxesTool(client).execute())
    finally:
        backend.close()
    capabilities = result["mailboxes"][0]["capabilities"]
    assert capabilities["calendar"] is True
    assert capabilities["contacts"] is True


def test_caldav_event_crud_uses_href_uid_and_etag_ids():
    server = DavServer()
    client, backend = make_client(server)
    try:
        created = invoke(CreateAppointmentTool, client, subject="Demo", start_time="2026-09-17T09:00:00+00:00", end_time="2026-09-17T10:00:00+00:00", location="Room", attendees=["peer@example.com"])
        assert created["success"] is True
        assert created["item_id"].startswith("/u/calendars/home/")
        assert created["uid"] and created["etag"] == '"ev1"'

        listed = invoke(GetCalendarTool, client, start_date="2026-09-17T00:00:00+00:00", end_date="2026-09-18T00:00:00+00:00")
        assert listed["events"] == [{**listed["events"][0], "subject": "Demo", "item_id": created["item_id"], "etag": '"ev1"'}]

        updated = invoke(UpdateAppointmentTool, client, item_id=created["item_id"], subject="Renamed")
        assert updated["success"] is True and updated["etag"] == '"ev2"'
        assert ("PUT", created["item_id"], '"ev1"') in server.requests

        deleted = invoke(DeleteAppointmentTool, client, item_id=created["item_id"])
        assert deleted["success"] is True and deleted["item_id"] == created["item_id"]
    finally:
        backend.close()


def test_carddav_contact_crud_uses_href_uid_and_etag_ids():
    server = DavServer()
    client, backend = make_client(server)
    try:
        created = invoke(CreateContactTool, client, given_name="Ada", surname="Lovelace", email_address="ada@example.com", phone_number="123", company="Analytical")
        assert created["success"] is True
        assert created["item_id"].startswith("/u/addressbooks/card/")
        assert created["uid"] and created["etag"] == '"ct1"'

        found = invoke(FindPersonTool, client, query="Ada", source="contacts")
        assert found["success"] is True
        assert found["unified_results"][0]["email"] == "ada@example.com"

        updated = invoke(UpdateContactTool, client, item_id=created["item_id"], phone_number="456")
        assert updated["success"] is True and updated["etag"] == '"ct2"'
        assert ("PUT", created["item_id"], '"ct1"') in server.requests

        deleted = invoke(DeleteContactTool, client, item_id=created["item_id"])
        assert deleted["success"] is True and deleted["item_id"] == created["item_id"]
    finally:
        backend.close()
