"""Native Graph forwarding retains server-owned content and attachments."""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

from types import SimpleNamespace
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.mail_backends.accounts import MailAccount
from src.mail_backends.base import MailBackendError, OutgoingAttachment, OutgoingMessage
from src.mail_backends.graph_backend import GraphBackend


def graph_backend(handler):
    backend = GraphBackend(MailAccount(
        email="user@outlook.com", provider="outlook",
        settings={"client_id": "cid", "refresh_token": "rt"},
    ))
    backend._access_token = "token"
    backend._token_expires_at = 1 << 40
    backend._client = httpx.Client(transport=httpx.MockTransport(handler))
    return backend


def test_forward_without_prelude_uses_native_action_and_recipient_overrides():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "POST"
        assert request.url.raw_path == b"/v1.0/me/messages/source%2F%2B%3D/forward"
        payload = json.loads(request.content)
        assert payload["message"] == {
            "toRecipients": [{"emailAddress": {"address": "to@example.com"}}],
            "ccRecipients": [{"emailAddress": {"address": "cc@example.com"}}],
            "bccRecipients": [{"emailAddress": {"address": "bcc@example.com"}}],
        }
        # Omitting body/attachments lets Graph forward its original MIME content,
        # including inline and item attachments, rather than rebuilding a copy.
        assert "body" not in payload["message"]
        assert "attachments" not in payload["message"]
        assert "internetMessageHeaders" not in payload["message"]
        return httpx.Response(202)

    backend = graph_backend(handler)
    try:
        result = backend.forward_message("source/+=", OutgoingMessage(
            to=["to@example.com"], cc=["cc@example.com"], bcc=["bcc@example.com"],
            tracking_id="not-on-wire",
        ))
    finally:
        backend.close()
    assert len(requests) == 1
    assert result["sent"] is True
    assert result["forwarded_from"] == "source/+="
    assert result["tracking_id"] is None
    assert result["accepted_by_mail_server"] is True
    assert result["delivery_confirmed"] is False


@pytest.mark.parametrize("source_body,prelude,body_format,expected", [
    (
        {"contentType": "html", "content": '<html><head><title>Kept</title></head><body class="mail"><p>Original</p><img src="cid:original"></body></html>'},
        "<em>Added</em>", "html",
        '<html><head><title>Kept</title></head><body class="mail"><em>Added</em><p>Original</p><img src="cid:original"></body></html>',
    ),
    (
        {"contentType": "text", "content": "Original <text>\nnext"},
        "Added <literal>\nsecond", "text",
        "<p>Added &lt;literal&gt;<br>second</p><p>Original &lt;text&gt;<br>next</p>",
    ),
])
def test_forward_prepends_formatted_body_without_discarding_native_history(source_body, prelude, body_format, expected):

    def handler(request):
        payload = json.loads(request.content) if request.content else None
        if request.url.path.endswith("/createForward"):
            assert payload["message"]["toRecipients"] == [{"emailAddress": {"address": "to@example.com"}}]
            assert "body" not in payload["message"]
            return httpx.Response(201, json={"id": "draft/+=", "body": source_body})
        if request.method == "PATCH":
            assert request.url.raw_path == b"/v1.0/me/messages/draft%2F%2B%3D"
            assert payload == {"body": {"contentType": "HTML", "content": expected}}
            return httpx.Response(200, json={"id": "draft/+="})
        if request.method == "GET" and request.url.path.endswith("/attachments"):
            return httpx.Response(200, json={"value": []})
        assert request.url.raw_path == b"/v1.0/me/messages/draft%2F%2B%3D/send"
        return httpx.Response(202)

    backend = graph_backend(handler)
    try:
        result = backend.forward_message("source/+=", OutgoingMessage(
            to=["to@example.com"], body=prelude, body_format=body_format,
        ))
    finally:
        backend.close()
    assert result["accepted_by_mail_server"] is True


def test_public_forward_uploads_only_new_files_with_inline_ids_then_sends(monkeypatch):
    from src.mail_backends.registry import MailboxRegistry
    from src.tools import ForwardEmailTool

    uploads = []
    steps = []

    def handler(request):
        steps.append(request.url.raw_path)
        payload = json.loads(request.content) if request.content else None
        if request.url.path.endswith("/createForward"):
            assert "body" not in payload["message"]
            assert "attachments" not in payload["message"]
            assert payload["message"]["bccRecipients"] == [{"emailAddress": {"address": "secret@example.com"}}]
            return httpx.Response(201, json={"id": "draft/+=", "hasAttachments": True})
        if request.method == "GET":
            return httpx.Response(200, json={"value": [
                {"id": "native", "name": "original.eml"},
                {"id": "extra", "name": "extra.pdf"},
                {"id": "logo", "name": "logo.png"},
            ]})
        if request.url.path.endswith("/attachments"):
            assert request.url.raw_path == b"/v1.0/me/messages/draft%2F%2B%3D/attachments"
            uploads.append(payload)
            return httpx.Response(201, json={"id": "new-attachment"})
        assert request.url.raw_path == b"/v1.0/me/messages/draft%2F%2B%3D/send"
        assert [item["name"] for item in uploads] == ["extra.pdf", "logo.png"]
        return httpx.Response(202)

    monkeypatch.setattr("src.mail_backends.registry.accounts_from_env", lambda: [])
    client = SimpleNamespace(config=SimpleNamespace(mail_account_rows=[{
        "email": "user@outlook.com", "provider": "outlook",
        "settings": {"client_id": "cid", "refresh_token": "rt"},
    }]))
    client.mailbox_registry = MailboxRegistry(client.config, client)
    backend = client.mailbox_registry.get_backend("user@outlook.com")
    backend._access_token = "token"
    backend._token_expires_at = 1 << 40
    backend._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        result = asyncio.run(ForwardEmailTool(client).execute(
            target_mailbox="user@outlook.com", message_id="source",
            to=["to@example.com"], bcc=["secret@example.com"],
            files=[
                {"file_name": "extra.pdf", "file_content": base64.b64encode(b"%PDF").decode(), "content_type": "application/pdf"},
                {"file_name": "logo.png", "file_content": base64.b64encode(b"PNG").decode(), "content_type": "image/png", "is_inline": True, "content_id": "logo"},
            ],
        ))
    finally:
        backend.close()
    assert uploads == [
        {"@odata.type": "#microsoft.graph.fileAttachment", "name": "extra.pdf", "contentType": "application/pdf", "contentBytes": base64.b64encode(b"%PDF").decode(), "isInline": False},
        {"@odata.type": "#microsoft.graph.fileAttachment", "name": "logo.png", "contentType": "image/png", "contentBytes": base64.b64encode(b"PNG").decode(), "isInline": True, "contentId": "logo"},
    ]
    assert result["accepted_by_mail_server"] is True


def test_forward_attachment_failure_never_sends_partial_message():
    steps = []

    def handler(request):
        steps.append(request.url.path)
        if request.url.path.endswith("/createForward"):
            return httpx.Response(201, json={"id": "draft"})
        assert request.url.path.endswith("/attachments"), "must not send after upload failure"
        if len(steps) == 2:
            return httpx.Response(201, json={"id": "first"})
        return httpx.Response(400, json={"error": {"code": "ErrorInvalidRequest", "message": "attachment rejected"}})

    backend = graph_backend(handler)
    try:
        with pytest.raises(MailBackendError, match="attachment rejected"):
            backend.forward_message("source", OutgoingMessage(
                to=["to@example.com"], attachments=[
                    OutgoingAttachment("first", b"a"), OutgoingAttachment("second", b"b"),
                ],
            ))
    finally:
        backend.close()
    assert steps == ["/v1.0/me/messages/source/createForward", "/v1.0/me/messages/draft/attachments", "/v1.0/me/messages/draft/attachments"]


def test_forward_oversize_files_fail_before_any_graph_mutation():
    def handler(request):
        pytest.fail(f"oversize forward must not call Graph: {request.method} {request.url}")

    backend = graph_backend(handler)
    try:
        with pytest.raises(MailBackendError):
            backend.forward_message("source", OutgoingMessage(
                to=["to@example.com"], body="Prelude", attachments=[
                    OutgoingAttachment("too-big", b"x" * (3 * 1024 * 1024 + 1)),
                ],
            ))
    finally:
        backend.close()


def test_forward_missing_draft_id_never_uploads_or_sends():
    steps = []

    def handler(request):
        steps.append(request.url.path)
        assert request.url.path.endswith("/createForward")
        return httpx.Response(201, json={})

    backend = graph_backend(handler)
    try:
        with pytest.raises(MailBackendError, match="draft id"):
            backend.forward_message("source", OutgoingMessage(
                to=["to@example.com"], attachments=[OutgoingAttachment("file", b"a")],
            ))
    finally:
        backend.close()
    assert steps == ["/v1.0/me/messages/source/createForward"]
