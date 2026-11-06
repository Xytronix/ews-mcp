"""One-shot Exchange files through registered tools and the EWS send boundary."""
from __future__ import annotations

import asyncio
import base64
import logging
from types import SimpleNamespace

import httpx
import pytest
from exchangelib import Body, FileAttachment, HTMLBody, ItemAttachment, Mailbox, Message

from src import attachment_sources as sources
from src.exceptions import ToolExecutionError, ValidationError
from src.mail_backends.accounts import MailAccount
from src.main import EWSMCPServer
from src.tools import email_tools
from tests.support import offline_account

TARGET = "shared@example.com"
PAYLOAD = b"new attachment bytes\x00\xff"
NS = {"t": "http://schemas.microsoft.com/exchange/services/2006/types"}


@pytest.fixture
def exchange(monkeypatch):
    account = offline_account(TARGET)
    sent = []
    storage_calls = []
    resolved_mailboxes = []

    def send(message, *args, **kwargs):
        sent.append(message.to_xml(version=account.version))

    monkeypatch.setattr(Message, "send", send)
    monkeypatch.setattr(
        Message, "save", lambda *args, **kwargs: pytest.fail("one-shot send must not save a draft")
    )

    class Storage:
        def download(self, method, path, **kwargs):
            storage_calls.append((method, path))
            return PAYLOAD

        def request(self, method, path, **kwargs):
            storage_calls.append((method, path))
            return {"folder": {"files": [{"id": "FILE-1", "fileName": "new.bin"}]}}

    def storage_for(mailbox):
        resolved_mailboxes.append(mailbox)
        return Storage()

    client = SimpleNamespace(
        config=SimpleNamespace(ews_email="primary@example.com"),
        mailbox_registry=SimpleNamespace(
            find_account=lambda address: MailAccount(email=address or TARGET, provider="ews", settings={})
        ),
        get_account=lambda address=None: account,
        smartermail=SimpleNamespace(enabled=True, get=storage_for),
    )
    original = SimpleNamespace(
        subject="Original subject", sender=Mailbox(email_address="author@example.com"),
        to_recipients=[Mailbox(email_address=TARGET)], cc_recipients=[],
        body=HTMLBody('<p>Original body<img src="cid:original-logo"></p>'),
        text_body="Original body", datetime_received=None,
        message_id="<source@example.com>", references="<ancestor@example.com>",
        attachments=[FileAttachment(name="original.png", content=b"original logo", is_inline=True,
                                    content_id="original-logo", content_type="image/png")],
    )
    monkeypatch.setattr(email_tools, "find_message_for_account", lambda *args, **kwargs: original)
    settings = SimpleNamespace(
        enable_email=True, enable_calendar=False, enable_contacts=False,
        enable_tasks=False, enable_ai=False, enable_agent=False,
        enable_semantic_search=False, smartermail_enabled=False,
        smartermail_admin_tools=False,
    )
    server = SimpleNamespace(settings=settings, logger=logging.getLogger(__name__), tools={},
                             ews_client=client, server=object(), openapi_adapter=None)
    EWSMCPServer.register_tools(server)
    return SimpleNamespace(account=account, client=client, tools=server.tools, sent=sent,
                           original=original, storage_calls=storage_calls,
                           resolved_mailboxes=resolved_mailboxes)


def execute(exchange, operation, **kwargs):
    if operation == "forward":
        tool = exchange.tools["forward_email"]
        args = {"message_id": "SOURCE", "to": ["recipient@example.com"], "body": "New body"}
    elif operation == "reply":
        tool = exchange.tools["reply_email"]
        args = {"message_id": "SOURCE", "body": "New body"}
    else:
        tool = exchange.tools["send_email"]
        args = {"to": ["recipient@example.com"], "subject": "New subject", "body": "New body"}
    return asyncio.run(tool.execute(target_mailbox=TARGET, **(args | kwargs)))


def attachment_rows(xml):
    return {
        item.findtext("t:Name", namespaces=NS): item
        for item in xml.findall("t:Attachments/t:FileAttachment", NS)
    }


@pytest.mark.parametrize("operation", ["send", "reply", "forward"])
@pytest.mark.parametrize("source", ["file_url", "file_content", "storage_file_id", "file_path"])
def test_files_reach_ews_send_with_inline_identity(exchange, monkeypatch, tmp_path, operation, source):
    network = []

    def fetch(request):
        network.append(str(request.url))
        return httpx.Response(200, content=PAYLOAD, headers={"content-type": "application/octet-stream"})

    monkeypatch.setattr(sources, "_resolve_host", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(sources, "_build_client", lambda: httpx.Client(transport=httpx.MockTransport(fetch)))
    local = tmp_path / "new.bin"
    local.write_bytes(PAYLOAD)
    value = {
        "file_url": "https://cdn.example/new.bin", "file_content": base64.b64encode(PAYLOAD).decode(),
        "storage_file_id": "FILE-1", "file_path": str(local),
    }[source]
    response = execute(exchange, operation, files=[{
        source: value, "file_name": "new.bin", "content_type": "image/png",
        "is_inline": True, "content_id": "<new-logo>",
    }], body='<p>New body<img src="cid:new-logo"></p>')
    assert len(exchange.sent) == 1
    xml = exchange.sent[0]
    new = attachment_rows(xml)["new.bin"]
    assert base64.b64decode(new.findtext("t:Content", namespaces=NS)) == PAYLOAD
    assert new.findtext("t:ContentType", namespaces=NS) == "image/png"
    assert new.findtext("t:ContentId", namespaces=NS) == "new-logo"
    assert new.findtext("t:IsInline", namespaces=NS) in ("1", "true")
    assert xml.findtext("t:From/t:Mailbox/t:EmailAddress", namespaces=NS) == TARGET
    assert "cid:new-logo" in xml.findtext("t:Body", namespaces=NS)
    assert response["accepted_by_mail_server"] is True
    assert response["delivery_confirmed"] is False
    if operation != "send":
        assert "Original body" in xml.findtext("t:Body", namespaces=NS)
        assert xml.findtext("t:InReplyTo", namespaces=NS) == "<source@example.com>"
        assert xml.findtext("t:References", namespaces=NS) == "<ancestor@example.com> <source@example.com>"
        assert attachment_rows(xml)["original.png"].findtext("t:ContentId", namespaces=NS) == "original-logo"
    if source == "file_url":
        assert network == ["https://cdn.example/new.bin"]
    if source == "storage_file_id":
        assert exchange.resolved_mailboxes == [TARGET]
        assert ("GET", "/api/v1/filestorage/FILE-1/download") in exchange.storage_calls
    else:
        assert exchange.resolved_mailboxes == []


@pytest.mark.parametrize("operation", ["send", "reply", "forward"])
def test_resolution_failure_never_sends_partial_files(exchange, monkeypatch, operation):
    monkeypatch.setattr(sources, "_resolve_host", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(sources, "_build_client", lambda: httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(404))))
    with pytest.raises((ToolExecutionError, ValidationError), match="404"):
        execute(exchange, operation, files=[
            {"file_content": "b2s=", "file_name": "valid.txt"},
            {"file_url": "https://cdn.example/missing.pdf"},
        ])
    assert exchange.sent == []


@pytest.mark.parametrize("operation", ["send", "reply", "forward"])
def test_new_files_are_additive_to_legacy_attachments(exchange, tmp_path, operation):
    legacy = tmp_path / "legacy.txt"
    legacy.write_bytes(b"legacy path")
    execute(exchange, operation, attachments=[str(legacy)],
            inline_attachments=[{"file_name": "legacy.png", "file_content": "aW5saW5l", "is_inline": True}],
            files=[{"file_content": "bmV3", "file_name": "new.txt"}])
    rows = attachment_rows(exchange.sent[0])
    for name, content in [("legacy.txt", b"legacy path"), ("legacy.png", b"inline"), ("new.txt", b"new")]:
        assert base64.b64decode(rows[name].findtext("t:Content", namespaces=NS)) == content
    assert rows["legacy.png"].findtext("t:IsInline", namespaces=NS) in ("1", "true")


def test_forward_preserves_nested_messages_and_their_attachments(exchange):
    nested = Message(subject="Nested subject", body=Body("Nested body"),
                     attachments=[FileAttachment(name="nested.txt", content=b"nested bytes")])
    exchange.original.attachments.append(ItemAttachment(name="nested.eml", item=nested))
    execute(exchange, "forward", files=[{"file_content": "bmV3", "file_name": "new.txt"}])
    xml = exchange.sent[0]
    embedded = xml.find("t:Attachments/t:ItemAttachment/t:Message", NS)
    assert embedded is not None
    assert embedded.findtext("t:Subject", namespaces=NS) == "Nested subject"
    assert embedded.findtext("t:Body", namespaces=NS) == "Nested body"
    assert base64.b64decode(attachment_rows(embedded)["nested.txt"].findtext("t:Content", namespaces=NS)) == b"nested bytes"
    assert "new.txt" in attachment_rows(xml)


def test_send_dry_run_counts_files_without_loading_any_source(exchange, monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("dry_run must not resolve files")

    monkeypatch.setattr(sources, "fetch_url_bytes", forbidden)
    monkeypatch.setattr(sources, "fetch_storage_bytes", forbidden)
    legacy = tmp_path / "legacy.txt"
    legacy.write_bytes(b"legacy preview")
    response = execute(exchange, "send", dry_run=True, attachments=[str(legacy)], files=[
        {"file_url": "https://cdn.example/new.bin"}, {"storage_file_id": "FILE-1"},
        {"file_path": str(tmp_path / "not-on-server.txt")},
    ])
    assert exchange.sent == []
    assert exchange.resolved_mailboxes == []
    assert response["files_fetched"] is False
    assert response["would_attach"] == 4
    assert response["attachments_count"] == 1
    assert response["sent"] is False


def test_send_dry_run_rejects_ambiguous_sources(exchange):
    with pytest.raises((ToolExecutionError, ValidationError), match="exactly one"):
        execute(exchange, "send", dry_run=True, files=[{
            "file_url": "https://cdn.example/new.bin", "storage_file_id": "FILE-1",
        }])
    assert exchange.sent == []


def test_stored_draft_send_rejects_files_before_lookup(exchange, monkeypatch):
    monkeypatch.setattr(email_tools, "find_message_for_account", lambda *args: pytest.fail("must reject compose override"))
    with pytest.raises(ValidationError, match="files"):
        asyncio.run(exchange.tools["send_email"].execute(
            target_mailbox=TARGET, draft_id="DRAFT", files=[{"file_content": "eA==", "file_name": "x.txt"}]))
    assert exchange.sent == []
