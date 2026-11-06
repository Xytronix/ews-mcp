"""Saved Exchange drafts must be sent, not rebuilt as new messages."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from exchangelib import FileAttachment, Mailbox, Message
from exchangelib.folders import SentItems
from exchangelib.services import SendItem
from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.exceptions import ToolExecutionError, ValidationError
from src.tools import email_tools, mailbox_tools
from tests.support import offline_account

ADDRESS = "owner@example.com"


@pytest.fixture
def draft_mailbox(monkeypatch):
    account = offline_account(ADDRESS)
    account.__dict__["sent"] = SentItems()
    message = Message(
        account=account, is_draft=True,
        subject="RE: Existing thread", body="Stored reply",
        to_recipients=[Mailbox(email_address="recipient@example.com")],
        cc_recipients=[Mailbox(email_address="cc@example.com")],
        references="<original@example.com>",
        attachments=[FileAttachment(name="invoice.pdf", content=b"%PDF-1.4 stored")],
    )
    message.id, message.changekey = "DRAFT-1", "CK-1"
    sent = []

    def find(found_account, message_id):
        assert found_account is account
        if message_id != message.id:
            raise ToolExecutionError("Message not found")
        return message

    def send_item(service, items, saved_item_folder):
        item, = items
        sent.append((item.id, item.subject, str(item.body), item.references,
                     item.attachments[0].content, saved_item_folder))

    def get_account(target):
        assert target == ADDRESS
        return account

    monkeypatch.setattr(email_tools, "find_message_for_account", find)
    monkeypatch.setattr(SendItem, "get", send_item)
    client = SimpleNamespace(
        config=SimpleNamespace(ews_email="user@example.com"),
        get_account=get_account,
        mailbox_registry=SimpleNamespace(
            find_account=lambda address: SimpleNamespace(kind="ews", email=address)
        ),
    )
    return client, message, sent


@pytest.mark.parametrize("tool_class", [email_tools.SendEmailTool, mailbox_tools.SendEmailTool])
def test_sends_saved_draft_with_pdf_and_thread_unchanged(draft_mailbox, tool_class):
    client, message, sent = draft_mailbox
    response = asyncio.run(tool_class(client).execute(draft_id="DRAFT-1", target_mailbox=ADDRESS))
    assert sent == [("DRAFT-1", "RE: Existing thread", "Stored reply",
                     "<original@example.com>", b"%PDF-1.4 stored", message.account.sent)]
    assert message.id is None  # Real exchangelib SendItem lifecycle, not a fresh CreateItem.
    assert response["draft_id"] == "DRAFT-1"
    assert response["mailbox"] == ADDRESS
    assert response["accepted_by_mail_server"] is True
    assert response["delivery_confirmed"] is False


def test_draft_dry_run_preserves_draft_and_reports_stored_content(draft_mailbox):
    client, message, sent = draft_mailbox
    response = asyncio.run(mailbox_tools.SendEmailTool(client).execute(
        draft_id="DRAFT-1", target_mailbox=ADDRESS, dry_run=True))
    assert sent == []
    assert message.id == "DRAFT-1"
    assert response["sent"] is False
    assert response["would_send_to"] == ["recipient@example.com"]
    assert response["would_cc"] == ["cc@example.com"]
    assert response["attachments_count"] == 1
    assert response["body_preview"] == "Stored reply"


@pytest.mark.parametrize("field,value", [("body", "replace"), ("attachments", []), ("to", ["other@example.com"])])
def test_draft_rejects_compose_overrides_before_sending(draft_mailbox, field, value):
    client, message, sent = draft_mailbox
    with pytest.raises(ValidationError, match="draft_id"):
        asyncio.run(mailbox_tools.SendEmailTool(client).execute(
            draft_id="DRAFT-1", target_mailbox=ADDRESS, **{field: value}))
    assert sent == []
    assert message.id == "DRAFT-1"


def test_non_draft_cannot_be_resent(draft_mailbox):
    client, message, sent = draft_mailbox
    message.is_draft = False
    with pytest.raises(ValidationError, match="draft"):
        asyncio.run(mailbox_tools.SendEmailTool(client).execute(draft_id="DRAFT-1", target_mailbox=ADDRESS))
    assert sent == []


def test_missing_draft_does_not_create_or_send_message(draft_mailbox):
    client, _, sent = draft_mailbox
    with pytest.raises(ToolExecutionError, match="Message not found"):
        asyncio.run(mailbox_tools.SendEmailTool(client).execute(draft_id="missing", target_mailbox=ADDRESS))
    assert sent == []


def test_send_failure_is_not_reported_as_acceptance(draft_mailbox, monkeypatch):
    client, message, _ = draft_mailbox
    def fail(*args, **kwargs):
        raise RuntimeError("submission refused")
    monkeypatch.setattr(SendItem, "get", fail)
    with pytest.raises(ToolExecutionError, match="submission refused"):
        asyncio.run(mailbox_tools.SendEmailTool(client).execute(draft_id="DRAFT-1", target_mailbox=ADDRESS))
    assert message.id == "DRAFT-1"


def test_public_schema_accepts_draft_only_but_requires_new_message_fields():
    schema = mailbox_tools.SendEmailTool(object()).get_schema()["inputSchema"]
    assert "draft_id" in schema["properties"]
    validator = Draft202012Validator(schema)
    assert validator.is_valid({"draft_id": "DRAFT-1", "target_mailbox": ADDRESS})
    assert validator.is_valid({"to": ["recipient@example.com"], "subject": "new", "body": "new"})
    assert not validator.is_valid({"target_mailbox": ADDRESS})
    assert not validator.is_valid({"to": ["recipient@example.com"], "body": "new"})
    assert not validator.is_valid({"draft_id": ""})


@pytest.mark.parametrize("reply", [False, True])
def test_create_attach_send_workflow_preserves_saved_pdf(monkeypatch, reply):
    import base64
    from exchangelib.folders import Drafts, Root
    from exchangelib.attachments import AttachmentId
    from src.tools import email_tools_draft

    account = offline_account(ADDRESS)
    root = Root(account=account)
    account.__dict__["drafts"] = Drafts(root=root)
    account.__dict__["inbox"] = Drafts(root=root)
    account.__dict__["sent"] = SentItems(root=root)
    stored = {}
    submitted = []

    def save(message, **kwargs):
        message.id, message.changekey = "SAVED-DRAFT", "CK"
        message.is_draft = True
        stored[message.id] = message

    def lookup(account_, message_id):
        return stored[message_id]

    def attach(attachment):
        attachment.attachment_id = AttachmentId(id="ATTACHMENT")

    def submit(service, items, saved_item_folder):
        message, = items
        submitted.append((
            message.id, message.subject, str(message.body),
            message.to_recipients[0].email_address,
            message.attachments[0].content,
        ))

    monkeypatch.setattr(Message, "save", save)
    monkeypatch.setattr(FileAttachment, "attach", attach)
    monkeypatch.setattr(Drafts, "get", lambda self, id: stored[id])
    monkeypatch.setattr(email_tools, "find_message_for_account", lookup)
    monkeypatch.setattr(email_tools_draft, "find_message_for_account", lookup)
    monkeypatch.setattr(SendItem, "get", submit)
    client = SimpleNamespace(
        config=SimpleNamespace(ews_email=ADDRESS),
        get_account=lambda target: account,
        mailbox_registry=SimpleNamespace(
            find_account=lambda address: SimpleNamespace(kind="ews", email=address)
        ),
    )
    async def workflow():
        if reply:
            stored["ORIGINAL"] = Message(
                subject="Question", body="Original text",
                sender=Mailbox(email_address="recipient@example.com"),
            )
            created = await email_tools_draft.CreateReplyDraftTool(client).execute(
                message_id="ORIGINAL", body="Answer", target_mailbox=ADDRESS)
        else:
            created = await email_tools_draft.CreateDraftTool(client).execute(
                to=["recipient@example.com"], subject="Question", body="Answer",
                target_mailbox=ADDRESS)
        await mailbox_tools.AddAttachmentTool(client).execute(
            message_id=created["message_id"], target_mailbox=ADDRESS,
            file_name="invoice.pdf",
            file_content=base64.b64encode(b"%PDF-1.4 workflow").decode())
        before = str(stored[created["message_id"]].body)
        response = await mailbox_tools.SendEmailTool(client).execute(
            draft_id=created["message_id"], target_mailbox=ADDRESS)
        assert response["accepted_by_mail_server"] is True
        assert submitted == [(
            "SAVED-DRAFT", "RE: Question" if reply else "Question", before,
            "recipient@example.com", b"%PDF-1.4 workflow",
        )]

    asyncio.run(workflow())
