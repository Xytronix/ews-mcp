"""Public contact tools against a stateful Graph HTTP boundary."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.mail_backends.accounts import MailAccount
from src.mail_backends.graph_backend import GraphBackend
from src.tools import (
    AnalyzeContactsTool, CreateContactTool, DeleteContactTool, FindPersonTool,
    UpdateContactTool,
)


def client_for(handler, provider="outlook"):
    account = MailAccount(email="user@outlook.com", provider=provider,
                          settings={"client_id": "cid", "refresh_token": "rt"})
    backend = GraphBackend(account)
    backend._access_token = "token"
    backend._token_expires_at = 1 << 40
    backend._client = httpx.Client(transport=httpx.MockTransport(handler))
    registry = SimpleNamespace(find_account=lambda address: account,
                               get_backend=lambda address: backend)
    return SimpleNamespace(mailbox_registry=registry), backend


def invoke(tool_class, client, **kwargs):
    return asyncio.run(tool_class(client).execute(target_mailbox="user@outlook.com", **kwargs))


def test_public_contact_lifecycle_maps_fields_and_keeps_provider_collections():
    state = {}
    patches = []

    def handler(request):
        payload = json.loads(request.content) if request.content else None
        if request.method == "POST":
            assert request.url.path == "/v1.0/me/contacts"
            assert payload == {
                "givenName": "Alice", "surname": "Example", "displayName": "Alice Example",
                "emailAddresses": [{"address": "alice@example.com", "name": "Alice Example"}],
                "businessPhones": ["111"], "companyName": "Acme", "jobTitle": "Engineer",
                "department": "Research", "categories": ["VIP"],
            }
            state.update(payload, id="contact/+=", homePhones=["333"], mobilePhone="444")
            state["emailAddresses"].append({"name": "Other", "address": "other@example.com"})
            state["businessPhones"].append("222")
            return httpx.Response(201, json=state)
        assert request.url.raw_path.split(b"?")[0] == b"/v1.0/me/contacts/contact%2F%2B%3D"
        if request.method == "PATCH":
            patches.append(payload)
            state.update(payload)
            return httpx.Response(200, json=state)
        if request.method == "DELETE":
            state.clear()
            return httpx.Response(204)
        return httpx.Response(200, json=state)

    client, backend = client_for(handler)
    try:
        created = invoke(CreateContactTool, client, given_name="Alice", surname="Example",
                         email_address="alice@example.com", phone_number="111", company="Acme",
                         job_title="Engineer", department="Research", categories=["VIP"])
        assert created["success"] is True
        assert created["item_id"] == "contact/+="
        updated = invoke(UpdateContactTool, client, item_id=created["item_id"], given_name="Alicia",
                         email_address="new@example.com", phone_number="999")
        assert updated["success"] is True
        assert state["displayName"] == "Alicia Example"
        assert [e["address"] for e in state["emailAddresses"]] == ["new@example.com", "other@example.com"]
        assert state["businessPhones"] == ["999", "222"]
        assert state["homePhones"] == ["333"]
        assert state["mobilePhone"] == "444"
        assert state["companyName"] == "Acme"
        assert "companyName" not in patches[0]
        cleared = invoke(UpdateContactTool, client, item_id=created["item_id"], email_address="",
                         phone_number="", company="", categories=[])
        assert cleared["success"] is True
        assert state["emailAddresses"] == [{"name": "Other", "address": "other@example.com"}]
        assert state["businessPhones"] == ["222"]
        assert state["companyName"] == ""
        assert state["categories"] == []
        assert state["givenName"] == "Alicia"
        assert invoke(DeleteContactTool, client, item_id=created["item_id"])["success"] is True
        assert state == {}
    finally:
        backend.close()


def test_public_contact_listing_and_search_follow_pages_and_expose_real_details():
    queries = []
    records = [
        {"id": "one", "displayName": "Alice", "emailAddresses": [{"address": "alice@example.com"}]},
        {"id": "two", "displayName": "O'Brien", "emailAddresses": [
            {"address": "primary@example.com"}, {"address": "match@example.com"}],
         "businessPhones": ["123"], "homePhones": ["456"], "mobilePhone": "789"},
    ]

    def handler(request):
        queries.append(dict(request.url.params))
        assert "$filter" not in request.url.params  # Free text never becomes OData syntax.
        if request.url.params.get("$skiptoken"):
            return httpx.Response(200, json={"value": records[1:]})
        return httpx.Response(200, json={"value": records[:1], "@odata.nextLink":
            "https://graph.microsoft.com/v1.0/me/contacts?$skiptoken=page2"})

    client, backend = client_for(handler)
    try:
        listed = invoke(FindPersonTool, client, source="contacts", include_stats=False)
        assert [c["item_id"] for c in listed["contacts"]] == ["one", "two"]
        assert listed["count"] == 2
        assert listed["truncated"] is False
        found = invoke(FindPersonTool, client, query="O'Brien", include_stats=True)
        assert found["total_results"] == 1
        person = found["unified_results"][0]
        assert person["item_id"] == "two"
        assert person["sources"] == ["contacts"]
        assert {p["number"] for p in person["phone_numbers"]} == {"123", "456", "789"}
        assert "email_count" not in person
        assert found["partial"] is True
        assert "gal" in found["unsupported_sources"]
        assert found["stats_included"] is False
        assert found["limitations"]
        secondary = invoke(FindPersonTool, client, source="contacts", query="match@example.com")
        assert secondary["unified_results"][0]["item_id"] == "two"
        assert len(queries) == 6
    finally:
        backend.close()


def test_default_empty_query_lists_supported_contacts_and_reports_limit():
    def handler(request):
        return httpx.Response(200, json={"value": [
            {"id": "one", "displayName": "One"}, {"id": "two", "displayName": "Two"}],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/contacts?$skiptoken=next"})

    client, backend = client_for(handler)
    try:
        result = invoke(FindPersonTool, client, max_results=1)
        assert result["success"] is True
        assert result["total_results"] == 1
        assert result["truncated"] is True
        assert result["partial"] is True
    finally:
        backend.close()


@pytest.mark.parametrize("source", ["gal", "email_history", "domain"])
def test_unsupported_sources_do_not_pretend_to_search_directory(source):
    def handler(request):
        pytest.fail("Unsupported sources must not issue Graph requests")

    client, backend = client_for(handler)
    try:
        result = invoke(FindPersonTool, client, source=source, query="Alice")
        assert result["success"] is False
        assert result["unsupported_sources"] == [source]
        assert "unified_results" not in result
    finally:
        backend.close()


@pytest.mark.parametrize("tool,args", [
    (CreateContactTool, {"given_name": "A", "surname": "B", "email_address": "a@example.com"}),
    (UpdateContactTool, {"item_id": "contact", "company": "New"}),
    (DeleteContactTool, {"item_id": "contact"}),
    (FindPersonTool, {"source": "contacts"}),
])
def test_scope_denial_is_actionable_at_public_tool_boundary(tool, args):
    def handler(request):
        return httpx.Response(403, json={"error": {"code": "ErrorAccessDenied", "message": "Denied"}})

    client, backend = client_for(handler)
    try:
        result = invoke(tool, client, **args)
        assert result["success"] is False
        assert "Contacts.ReadWrite" in result["error"]
        assert "re-consent" in result["error"]
        assert "Mail.Send" not in result["error"]
    finally:
        backend.close()


def test_server_failure_and_external_paging_url_never_report_success():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.method == "DELETE":
            return httpx.Response(500, json={"error": {"message": "Unavailable"}})
        return httpx.Response(200, json={"value": [], "@odata.nextLink": "https://evil.example/steal"})

    client, backend = client_for(handler)
    try:
        failed = invoke(DeleteContactTool, client, item_id="contact")
        assert failed["success"] is False
        assert "500" in failed["error"]
        paging = invoke(FindPersonTool, client, source="contacts")
        assert paging["success"] is False
        assert all("evil.example" not in url for url in calls)
    finally:
        backend.close()


def test_graph_analytics_explicitly_unsupported_not_fake_empty_statistics():
    client, backend = client_for(lambda request: pytest.fail("No Graph analytics implementation"))
    try:
        result = invoke(AnalyzeContactsTool, client, analysis_type="overview")
        assert result["success"] is False
        assert "mail" in result["error"].lower()
        assert "total_emails" not in result
    finally:
        backend.close()


def test_generic_imap_contacts_require_carddav_configuration():
    from src.mail_backends.imap_backend import IMAPBackend
    account = MailAccount(email="user@example.com", provider="imap", password="pw",
                          settings={"imap_host": "imap.example.com", "smtp_host": "smtp.example.com"})
    backend = IMAPBackend(account)
    registry = SimpleNamespace(find_account=lambda address: account, get_backend=lambda address: backend)
    client = SimpleNamespace(mailbox_registry=registry)
    try:
        result = asyncio.run(FindPersonTool(client).execute(target_mailbox="user@example.com", source="contacts"))
        assert result["success"] is False
        assert "CardDAV" in result["error"]
    finally:
        backend.close()


def test_exchange_fallback_keeps_existing_implementation(monkeypatch):
    from src.tools.contact_tools import CreateContactTool as ExchangeCreate

    sentinel = {"success": True, "item_id": "exchange-contact"}

    async def exchange_execute(self, **kwargs):
        assert kwargs["given_name"] == "A"
        return sentinel

    monkeypatch.setattr(ExchangeCreate, "execute", exchange_execute)
    account = MailAccount(email="user@outlook.com", provider="ews")
    client = SimpleNamespace(mailbox_registry=SimpleNamespace(find_account=lambda address: account))
    assert invoke(CreateContactTool, client, given_name="A", surname="B", email_address="a@example.com") == sentinel


def test_noop_update_checks_existence_without_overwriting_fields():
    calls = []

    def handler(request):
        calls.append(request.method)
        assert request.method == "GET"
        return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})

    client, backend = client_for(handler)
    try:
        result = invoke(UpdateContactTool, client, item_id="missing")
        assert result["success"] is False
        assert "404" in result["error"]
        assert calls == ["GET"]
    finally:
        backend.close()


def test_search_scan_bound_reports_incomplete_instead_of_false_no_match():
    pages = []

    def handler(request):
        pages.append(request)
        return httpx.Response(200, json={"value": [{"id": str(len(pages)), "displayName": "Other"}],
            "@odata.nextLink": f"https://graph.microsoft.com/v1.0/me/contacts?$skiptoken={len(pages)}"})

    client, backend = client_for(handler)
    try:
        result = invoke(FindPersonTool, client, source="contacts", query="Absent", include_stats=False)
        assert result["unified_results"] == []
        assert result["truncated"] is True
        assert result["partial"] is True
        assert len(pages) == 20
    finally:
        backend.close()


def test_refresh_scope_error_is_actionable_with_calendar_contact_scopes():
    def handler(request):
        assert request.url.host == "login.microsoftonline.com"
        assert b"Contacts.ReadWrite" in request.content
        return httpx.Response(400, json={"error": "invalid_scope", "error_description": "Consent missing"})

    client, backend = client_for(handler)
    backend._access_token = None
    try:
        result = invoke(FindPersonTool, client, source="contacts")
        assert result["success"] is False
        assert "Contacts.ReadWrite" in result["error"]
        assert "re-consent" in result["error"]
    finally:
        backend.close()
