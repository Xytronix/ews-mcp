"""Public draft routing and partial-update contracts."""
import asyncio
import email
import email.policy
import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from exchangelib import FileAttachment, HTMLBody, Mailbox, Message

from src.exceptions import ToolExecutionError, ValidationError
from src.mail_backends.accounts import MailAccount
from src.mail_backends.base import MailBackendError
from src.mail_backends.graph_backend import GraphBackend
from src.tools import (
    CreateDraftTool, CreateReplyDraftTool, CreateForwardDraftTool, UpdateDraftTool,
    ListMailboxesTool, SendEmailTool, ReplyEmailTool, ForwardEmailTool,
)
from tests.support import offline_account
from test_imap_safety import mailbox  # noqa: F401
from test_imap_drafts import source


class Registry:
    def __init__(self, account, backend=None):
        self.account = account
        self.backend = backend
        self.default_email = account.email

    def find_account(self, address=None):
        return self.account

    def get_backend(self, address=None):
        return self.backend

    def describe_accounts(self):
        return [self.account.describe()]


def client(account, backend=None):
    return SimpleNamespace(
        mailbox_registry=Registry(account, backend),
        config=SimpleNamespace(ews_email=account.email, smartermail_enabled=True),
        smartermail=SimpleNamespace(enabled=True),
    )


def run(tool, **kwargs):
    return asyncio.run(tool.execute(**kwargs))


def ews_draft(monkeypatch, is_draft=True):
    def forbid_network(*args, **kwargs):
        raise AssertionError("Offline draft fixture attempted a network request")

    monkeypatch.setattr("requests.sessions.Session.send", forbid_network)
    account = offline_account()
    message = Message(
        account=account, is_draft=is_draft,
        subject="Keep subject", body=HTMLBody("<p>Keep body</p>"),
        to_recipients=[Mailbox(email_address="recipient@example.com")],
        attachments=[FileAttachment(name="keep.txt", content=b"keep")],
    )
    # Populate attachments before assigning a persisted ID: exchangelib's
    # constructor otherwise issues CreateAttachment immediately.
    message.id = "ews-draft"
    message.changekey = "CK"
    monkeypatch.setattr(account.__class__, "fetch", lambda self, **kwargs: iter([message]))
    c = client(MailAccount(email=account.primary_smtp_address, provider="ews"))
    c.get_account = lambda target=None: account
    updates = []

    def update(self, **kwargs):
        # Keep Message.save real: intercept only its EWS update boundary.
        updates.append(kwargs["update_fieldnames"])
        return self.id, self.changekey

    monkeypatch.setattr(Message, "_update", update)
    return UpdateDraftTool(c), message, updates


def test_ews_update_empty_subject_preserves_body_recipients_and_attachments(monkeypatch):
    tool, message, updates = ews_draft(monkeypatch)
    result = run(tool, draft_id=message.id, subject="")
    assert result["success"] is True
    assert message.subject == ""
    assert message.body == "<p>Keep body</p>"
    assert message.to_recipients[0].email_address == "recipient@example.com"
    assert message.attachments[0].content == b"keep"
    assert updates == [["subject"]]


def test_ews_update_empty_body_and_omitted_subject(monkeypatch):
    tool, message, updates = ews_draft(monkeypatch)
    run(tool, draft_id=message.id, body="")
    assert str(message.body) == ""
    assert message.subject == "Keep subject"
    assert updates == [["body"]]


def test_ews_update_formats_nonempty_body_without_touching_other_fields(monkeypatch):
    tool, message, updates = ews_draft(monkeypatch)
    run(tool, draft_id=message.id, body="Text <literal>", body_format="text")
    assert "&lt;literal&gt;" in message.body
    assert message.subject == "Keep subject"
    assert updates == [["body"]]


def test_ews_update_no_fields_never_saves(monkeypatch):
    tool, message, updates = ews_draft(monkeypatch)
    run(tool, draft_id=message.id)
    assert updates == []


def test_ews_update_refuses_nondraft(monkeypatch):
    tool, message, updates = ews_draft(monkeypatch, is_draft=False)
    with pytest.raises(ToolExecutionError, match="draft"):
        run(tool, draft_id=message.id, subject="must not write")
    assert updates == []


@pytest.mark.parametrize("field", ["subject", "body"])
def test_update_refuses_null_and_nonstrings(monkeypatch, field):
    tool, message, updates = ews_draft(monkeypatch)
    with pytest.raises(ValidationError):
        run(tool, draft_id=message.id, **{field: None})
    assert updates == []


@pytest.mark.parametrize("tool_cls, kwargs", [
    (CreateDraftTool, {"to": ["to@example.com"], "subject": "Draft", "body": "hello"}),
    (CreateReplyDraftTool, {"message_id": "source", "body": "hello"}),
    (CreateForwardDraftTool, {"message_id": "source", "to": ["to@example.com"], "body": "hello"}),
    (UpdateDraftTool, {"draft_id": "draft", "body": "", "subject": ""}),
])
def test_graph_public_draft_tools_use_provider_and_return_saved_identity(tool_cls, kwargs):
    saved = {"id": "draft", "isDraft": True, "subject": "Keep", "body": {"contentType": "HTML", "content": "Keep"}}
    attachments = []
    calls = []

    def handler(request):
        path = request.url.path
        calls.append((request.method, path))
        payload = json.loads(request.content) if request.content else {}
        if path.endswith("/attachments"):
            if request.method == "POST":
                attachments.append({**payload, "id": "fresh-attachment", "size": 4})
                return httpx.Response(201, json=attachments[-1])
            return httpx.Response(200, json={"value": attachments})
        if request.method in ("POST", "PATCH"):
            saved.update(payload)
        return httpx.Response(201 if request.method == "POST" else 200, json=saved)

    account = MailAccount(email="user@outlook.com", provider="outlook")
    backend = GraphBackend(account)
    backend._access_token = "token"
    backend._token_expires_at = 1 << 40
    backend._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        if tool_cls is not UpdateDraftTool:
            kwargs = {**kwargs, "files": [{"file_name": "test.txt", "file_content": base64.b64encode(b"test").decode()}]}
        result = run(tool_cls(client(account, backend)), target_mailbox=account.email, **kwargs)
        assert result["success"] is True
        assert result["id"] == result["message_id"] == "draft"
        assert result["mailbox"] == account.email and result["backend"] == "graph"
        assert result["folder"] == "drafts"
        assert not any(path.endswith("/send") for _, path in calls)
        if tool_cls is UpdateDraftTool:
            assert saved["subject"] == "" and saved["body"]["content"] == ""
        else:
            assert result["attachments"][0]["attachment_id"] == "fresh-attachment"
    finally:
        backend.close()


def test_public_error_preserves_full_recovery_metadata():
    account = MailAccount(email="user@outlook.com", provider="outlook")
    recovery_id = "long-draft-id-" * 100
    details = {"draft_id": recovery_id, "folder": "drafts", "stage": "attachments", "sent": False,
               "cleanup": {"attempted": False}, "submission_uncertain": False}

    class FailingBackend:
        def create_draft(self, message):
            raise MailBackendError("attachment failed", details=details)

    result = run(CreateDraftTool(client(account, FailingBackend())), to=["to@example.com"], subject="s", body="b")
    assert result["success"] is False
    assert result["details"] == details
    assert result["draft_id"] == recovery_id
    assert result["mailbox"] == account.email
    assert result["provider"] == "outlook" and result["backend"] == "graph"


@pytest.mark.parametrize("provider, backend, schedule", [
    ("ews", "ews", True), ("smartermail", "ews", True),
    ("outlook", "graph", False), ("icloud", "imap", False),
])
def test_mailbox_capabilities_are_support_not_provider_promise(provider, backend, schedule):
    account = MailAccount(email="user@example.com", provider=provider)
    result = run(ListMailboxesTool(client(account)))
    entry = result["mailboxes"][0]
    caps = entry["capabilities"]
    assert caps["drafts_create"] and caps["update_draft"] and caps["files"]
    assert caps["folder_scoped_ids"] is (backend == "imap")
    dav_capable = provider == "icloud"
    assert caps["calendar"] is (backend in ("ews", "graph") or dav_capable)
    assert caps["contacts"] is (backend in ("ews", "graph") or dav_capable)
    assert caps["schedule"] is schedule
    assert "capability_hints" in entry


def test_schedule_is_not_advertised_when_native_service_is_disabled():
    c = client(MailAccount(email="user@example.com", provider="smartermail"))
    c.smartermail.enabled = False
    entry = run(ListMailboxesTool(c))["mailboxes"][0]
    assert entry["capabilities"]["schedule"] is False


@pytest.mark.parametrize("tool_cls", [CreateDraftTool, CreateReplyDraftTool, CreateForwardDraftTool, SendEmailTool, ReplyEmailTool, ForwardEmailTool])
def test_public_outgoing_schema_advertises_canonical_files_and_safe_cleanup(tool_cls):
    schema = tool_cls(client(MailAccount(email="user@example.com"))).get_schema()
    json.dumps(schema)
    props = schema["inputSchema"]["properties"]
    assert props["cleanup_on_failure"]["type"] == "boolean"
    assert props["cleanup_on_failure"]["default"] is False
    assert "files" in props
    assert props["attachments"]["deprecated"] is True


@pytest.mark.parametrize("tool_cls, kwargs", [
    (CreateDraftTool, {"to": ["to@example.com"], "subject": "Draft", "body": "hello"}),
    (CreateReplyDraftTool, {"message_id": "41:7", "body": "hello"}),
    (CreateForwardDraftTool, {"message_id": "41:7", "to": ["to@example.com"], "body": "hello"}),
    (UpdateDraftTool, {"draft_id": "40:7", "body": "", "subject": ""}),
])
def test_imap_public_draft_tools_return_working_replacement_ids(mailbox, tool_cls, kwargs):
    source(mailbox)
    backend, server = mailbox
    account = backend.account
    if tool_cls is not UpdateDraftTool:
        kwargs = {**kwargs, "files": [{"file_name": "test.txt", "file_content": base64.b64encode(b"test").decode()}]}
    result = run(tool_cls(client(account, backend)), target_mailbox=account.email, **kwargs)
    assert result["success"] is True
    assert result["id"] == result["message_id"]
    assert result["id"] != "40:7"
    assert result["folder"] == "drafts"
    stored = email.message_from_bytes(
        backend.get_mime(result["id"], result["folder"]), policy=email.policy.default,
    )
    assert result["backend"] == "imap" and result["mailbox"] == account.email
    if tool_cls is UpdateDraftTool:
        assert result["draft_id"] == result["id"]
        assert result["previous_id"] == "40:7"
        assert not stored["Subject"]
        assert not stored.get_body(preferencelist=("plain",)).get_content().strip()
    else:
        attachment = next(a for a in result["attachments"] if a["name"] == "test.txt")
        assert backend.get_attachment(
            result["id"], result["folder"], attachment_id=attachment["attachment_id"],
        ).content == b"test"
    assert "8" in server.messages["Drafts"]


@pytest.mark.parametrize("tool_cls, kwargs", [
    (CreateDraftTool, {"to": ["to@example.com"], "subject": "s", "body": "b"}),
    (CreateReplyDraftTool, {"message_id": "source", "body": "b"}),
    (CreateForwardDraftTool, {"message_id": "source", "to": ["to@example.com"], "body": "b"}),
    (SendEmailTool, {"to": ["to@example.com"], "subject": "s", "body": "b"}),
    (ReplyEmailTool, {"message_id": "source", "body": "b"}),
    (ForwardEmailTool, {"message_id": "source", "to": ["to@example.com"], "body": "b"}),
])
@pytest.mark.parametrize("cleanup", [False, True])
def test_cleanup_option_controls_new_graph_draft_recovery(tool_cls, kwargs, cleanup):
    drafts = {}

    def handler(request):
        path = request.url.path
        if request.method == "DELETE":
            drafts.pop("draft")
            return httpx.Response(204)
        if path.endswith("/attachments") and request.method == "POST":
            return httpx.Response(400, json={"error": {"message": "Attachment refused"}})
        if request.method == "POST":
            drafts["draft"] = {"id": "draft", "isDraft": True,
                               "body": {"contentType": "HTML", "content": "Original"}}
            return httpx.Response(201, json=drafts["draft"])
        if request.method == "PATCH":
            drafts["draft"].update(json.loads(request.content))
        return httpx.Response(200, json=drafts["draft"])

    account = MailAccount(email="user@outlook.com", provider="outlook")
    backend = GraphBackend(account)
    backend._access_token = "token"
    backend._token_expires_at = 1 << 40
    backend._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        result = run(
            tool_cls(client(account, backend)), **kwargs, cleanup_on_failure=cleanup,
            files=[{"file_name": "test.txt", "file_content": base64.b64encode(b"test").decode()}],
        )
        assert result["success"] is False
        assert result["sent"] is False
        assert result["draft_id"] == "draft"
        assert ("draft" in drafts) is not cleanup
    finally:
        backend.close()


def test_safe_execute_preserves_recovery_and_records_backend_failure(monkeypatch):
    from src.middleware.circuit_breaker import CircuitBreaker, CircuitState

    breaker = CircuitBreaker(failure_threshold=1)
    monkeypatch.setattr("src.tools.base.get_circuit_breaker", lambda: breaker)
    details = {"draft_id": "long-id-" * 100, "stage": "attachments", "sent": False}

    class FailingBackend:
        def create_draft(self, message):
            raise MailBackendError("transport timeout", details=details)

    audits = []
    activities = []
    performance = []
    tool = CreateDraftTool(client(MailAccount(email="user@outlook.com", provider="outlook"), FailingBackend()))
    tool.log_manager = SimpleNamespace(
        log_activity=lambda **record: activities.append(record),
        log_performance=lambda **record: performance.append(record),
        log_audit=lambda **record: audits.append(record),
    )
    result = asyncio.run(tool.safe_execute(to=["to@example.com"], subject="s", body="b"))
    assert result["success"] is False
    assert result["details"] == details
    assert result["draft_id"] == details["draft_id"]
    assert result["error_type"] == "MailBackendError"
    assert result["error_class"] == "src.mail_backends.base.MailBackendError"
    assert breaker.state == CircuitState.OPEN
    assert [record["result"] for record in audits] == ["failed"]
    assert [record["status"] for record in performance] == ["failed"]
    assert not any(record["result"].get("status") == "success" for record in activities)
