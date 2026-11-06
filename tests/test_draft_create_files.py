"""Draft creation persists attachments after the server returns the new item ID."""
from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import httpx
import pytest
from exchangelib import FileAttachment, HTMLBody, ItemAttachment, Mailbox, Message
from exchangelib.folders import Drafts, Root
from exchangelib.attachments import AttachmentId
from exchangelib.items.base import BulkCreateResult
from exchangelib.services import CreateAttachment, CreateItem, DeleteItem

from src import attachment_sources
from src.exceptions import ToolExecutionError
from src.tools import email_tools_draft
from tests.support import offline_account

TARGET = "shared@example.com"
CREATORS = [
    email_tools_draft.CreateDraftTool,
    email_tools_draft.CreateReplyDraftTool,
    email_tools_draft.CreateForwardDraftTool,
]


def encoded(content):
    return base64.b64encode(content).decode()


def compose_args(tool_class):
    args = {"body": "New content", "target_mailbox": TARGET}
    if tool_class is email_tools_draft.CreateDraftTool:
        args.update(to=["recipient@example.com"], subject="New subject")
    else:
        args["message_id"] = "ORIGINAL"
        if tool_class is email_tools_draft.CreateForwardDraftTool:
            args["to"] = ["recipient@example.com"]
    return args


@pytest.fixture
def draft_boundary(monkeypatch):
    account = offline_account(TARGET)
    account.__dict__["drafts"] = Drafts(root=Root(account=account))
    original = Message(
        subject="Original subject",
        body=HTMLBody('<p>Original content<img src="cid:old-logo"/></p>'),
        sender=Mailbox(email_address="sender@example.com"),
        attachments=[
            FileAttachment(name="old.pdf", content=b"old pdf", content_type="application/pdf"),
            FileAttachment(name="old.png", content=b"old image", content_type="image/png",
                           is_inline=True, content_id="old-logo"),
            ItemAttachment(name="nested.eml", item=Message(
                subject="Nested message", body="Nested body",
                attachments=[FileAttachment(name="nested.txt", content=b"nested bytes")],
            )),
        ],
    )
    original.id = "ORIGINAL"
    boundary = SimpleNamespace(
        saved=[], stored={"ORIGINAL": original}, deleted=[], uploads=0,
        fail_upload=None, delete_error=None,
    )

    def create_item(service, items, **kwargs):
        message, = items
        assert message.id is None
        xml = message.to_xml(version=account.version)
        boundary.saved.append((message, xml))
        # SmarterMail SaveOnly ignores embedded attachments and returns no metadata.
        boundary.stored["CREATED-DRAFT"] = Message(
            id="CREATED-DRAFT", changekey="CK", subject=message.subject,
            body=message.body, attachments=[],
        )
        return BulkCreateResult(id="CREATED-DRAFT", changekey="CK", attachments=[])

    def create_attachment(service, parent_item, items):
        assert parent_item.id in boundary.stored
        attachment, = items
        boundary.uploads += 1
        if boundary.uploads == boundary.fail_upload:
            raise RuntimeError("attachment upload rejected")
        # Exercise real serialization; keep server state separate from the client.
        wire = attachment.to_xml(version=account.version)
        # EWS responses use true/false; exchangelib emits 1/0 in requests but
        # its response parser does not accept that numeric lexical form.
        for inline in wire.iter("{http://schemas.microsoft.com/exchange/services/2006/types}IsInline"):
            inline.text = {"1": "true", "0": "false"}.get(inline.text, inline.text)
        uploaded = type(attachment).from_xml(wire, account=account)
        uploaded.attachment_id = AttachmentId(
            id=f"ATTACHMENT-{boundary.uploads}", root_id=parent_item.id,
            root_changekey=f"CK-{boundary.uploads}",
        )
        boundary.stored[parent_item.id].attachments.append(uploaded)
        return uploaded

    def delete_item(service, items, **kwargs):
        message, = items
        if boundary.delete_error:
            raise RuntimeError(boundary.delete_error)
        boundary.deleted.append(message.id)
        del boundary.stored[message.id]

    monkeypatch.setattr(CreateItem, "get", create_item)
    monkeypatch.setattr(CreateAttachment, "get", create_attachment)
    monkeypatch.setattr(DeleteItem, "get", delete_item)
    monkeypatch.setattr(email_tools_draft, "find_message_for_account", lambda account, id: original)
    client = SimpleNamespace(
        config=SimpleNamespace(ews_email="primary@example.com"),
        get_account=lambda target: account,
        mailbox_registry=SimpleNamespace(find_account=lambda address: SimpleNamespace(kind="ews")),
    )
    return client, boundary


@pytest.mark.parametrize("tool_class", CREATORS)
def test_saved_draft_retains_files_when_create_item_returns_no_attachments(
    draft_boundary, monkeypatch, tmp_path, tool_class,
):
    client, boundary = draft_boundary
    seen_urls = []

    def serve(request):
        seen_urls.append(str(request.url))
        return httpx.Response(200, content=b"downloaded pdf", headers={"content-type": "application/pdf"})

    monkeypatch.setattr(attachment_sources, "_resolve_host", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(attachment_sources, "_build_client", lambda: httpx.Client(transport=httpx.MockTransport(serve)))
    storage_targets = []

    class Storage:
        def download(self, method, path, **kwargs):
            assert (method, path) == ("GET", "/api/v1/filestorage/FILE-1/download")
            return b"stored pdf"

        def request(self, method, path, **kwargs):
            return {"folder": {"id": 1, "files": [{"id": "FILE-1", "fileName": "stored.pdf"}]}}

    def storage_for(target):
        storage_targets.append(target)
        return Storage()

    client.smartermail = SimpleNamespace(enabled=True, get=storage_for)
    local = tmp_path / "local.txt"
    local.write_bytes(b"local bytes")
    legacy = tmp_path / "legacy.txt"
    legacy.write_bytes(b"legacy bytes")
    args = compose_args(tool_class)
    args.update(
        files=[
            {"file_url": "https://cdn.example/new.pdf"},
            {"file_name": "new.png", "file_content": encoded(b"new image"),
             "content_type": "image/png", "is_inline": True, "content_id": "new-logo"},
            {"storage_file_id": "FILE-1"},
            {"file_path": str(local)},
        ],
        attachments=[str(legacy)],
        inline_attachments=[{"file_name": "legacy.png", "file_content": encoded(b"legacy image"),
                             "is_inline": True, "content_id": "legacy-logo"}],
    )
    response = asyncio.run(tool_class(client).execute(**args))
    assert response["message_id"] == "CREATED-DRAFT"
    message = boundary.stored[response["message_id"]]
    assert len(boundary.saved) == 1
    assert storage_targets == [TARGET]
    assert seen_urls == ["https://cdn.example/new.pdf"]
    files = {a.name: a for a in message.attachments}
    expected = {"new.pdf": b"downloaded pdf", "new.png": b"new image", "stored.pdf": b"stored pdf",
                "local.txt": b"local bytes", "legacy.txt": b"legacy bytes", "legacy.png": b"legacy image"}
    if tool_class is not email_tools_draft.CreateDraftTool:
        expected.update({"old.pdf": b"old pdf", "old.png": b"old image"})
        assert files["old.png"].content_id == "old-logo"
        assert files["old.png"].is_inline is True
        assert files["nested.eml"].item.subject == "Nested message"
        assert files["nested.eml"].item.attachments[0].content == b"nested bytes"
        assert "Original content" in str(message.body)
    assert {name: a.content for name, a in files.items() if isinstance(a, FileAttachment)} == expected
    assert files["new.png"].is_inline is True
    assert files["new.png"].content_id == "new-logo"
    assert files["new.png"].content_type == "image/png"
    assert files["legacy.png"].content_id == "legacy-logo"
    assert all(a.attachment_id is not None for a in message.attachments)


@pytest.mark.parametrize("tool_class", CREATORS)
def test_failed_file_resolution_never_saves_partial_draft(draft_boundary, monkeypatch, tool_class):
    client, boundary = draft_boundary
    monkeypatch.setattr(attachment_sources, "_resolve_host", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(attachment_sources, "_build_client", lambda: httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(404))))
    args = compose_args(tool_class)
    args["files"] = [
        {"file_name": "first.txt", "file_content": encoded(b"first")},
        {"file_url": "https://cdn.example/missing.pdf"},
    ]
    with pytest.raises(ToolExecutionError, match="404"):
        asyncio.run(tool_class(client).execute(**args))
    assert boundary.saved == []
    assert set(boundary.stored) == {"ORIGINAL"}
    assert boundary.deleted == []


@pytest.mark.parametrize("tool_class", CREATORS)
def test_upload_failure_removes_only_new_partial_draft(draft_boundary, tool_class):
    client, boundary = draft_boundary
    boundary.fail_upload = 2
    args = compose_args(tool_class)
    args["files"] = [
        {"file_name": "first.txt", "file_content": encoded(b"first")},
        {"file_name": "second.txt", "file_content": encoded(b"second")},
    ]
    with pytest.raises(ToolExecutionError, match="attachment upload rejected"):
        asyncio.run(tool_class(client).execute(**args))
    assert boundary.uploads == 2
    assert boundary.deleted == ["CREATED-DRAFT"]
    assert set(boundary.stored) == {"ORIGINAL"}
    assert boundary.stored["ORIGINAL"].attachments[0].content == b"old pdf"


def test_upload_failure_reports_draft_cleanup_failure(draft_boundary):
    client, boundary = draft_boundary
    boundary.fail_upload = 2
    boundary.delete_error = "delete denied"
    args = compose_args(email_tools_draft.CreateDraftTool)
    args["files"] = [
        {"file_name": "first.txt", "file_content": encoded(b"first")},
        {"file_name": "second.txt", "file_content": encoded(b"second")},
    ]
    with pytest.raises(ToolExecutionError) as error:
        asyncio.run(email_tools_draft.CreateDraftTool(client).execute(**args))
    assert "attachment upload rejected" in str(error.value)
    assert "delete denied" in str(error.value)
    assert "CREATED-DRAFT" in str(error.value)
    assert [a.name for a in boundary.stored["CREATED-DRAFT"].attachments] == ["first.txt"]


def test_create_draft_converts_markdown_before_save(draft_boundary):
    client, boundary = draft_boundary
    args = compose_args(email_tools_draft.CreateDraftTool)
    args.update(body="**Important**\n\n- First\n- Second", body_format="markdown")
    asyncio.run(email_tools_draft.CreateDraftTool(client).execute(**args))
    message, _ = boundary.saved[0]
    assert isinstance(message.body, HTMLBody)
    assert "<strong>Important</strong>" in message.body
    assert "<li>First</li>" in message.body
    assert "**Important**" not in message.body
