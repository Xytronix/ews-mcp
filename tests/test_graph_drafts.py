"""Recoverable Graph drafts preserve provider-owned content and identities."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.mail_backends.accounts import MailAccount
from src.mail_backends.base import MailBackendError, OutgoingAttachment, OutgoingMessage
from src.mail_backends.graph_backend import GraphBackend
from src.utils import configure_outbound_tracking


def graph_backend(handler):
    backend = GraphBackend(MailAccount(
        email="user@outlook.com", provider="outlook",
        settings={"client_id": "cid", "refresh_token": "rt"},
    ))
    backend._access_token = "token"
    backend._token_expires_at = 1 << 40
    backend._client = httpx.Client(transport=httpx.MockTransport(handler))
    return backend


@pytest.mark.parametrize("operation,action", [
    ("create_reply_draft", "createReply"),
    ("create_reply_draft_all", "createReplyAll"),
    ("create_forward_draft", "createForward"),
])
def test_native_drafts_keep_thread_history_and_inherited_attachments(operation, action):
    state = {"body": "<html><body><p>Original</p><img src='cid:logo'></body></html>",
             "attachments": [{"id": "inherited", "name": "original.eml"}]}
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        payload = json.loads(request.content) if request.content else None
        if request.url.path.endswith('/' + action):
            assert "body" not in payload["message"]
            assert "attachments" not in payload["message"]
            return httpx.Response(201, json={"id": "draft", "body": {"contentType": "html", "content": state["body"]}})
        if request.method == "PATCH":
            assert set(payload) == {"body"}
            state["body"] = payload["body"]["content"]
            return httpx.Response(200, json={"id": "draft"})
        if request.method == "POST":
            assert request.url.path.endswith("/attachments")
            state["attachments"].append({"id": "uploaded", "name": payload["name"]})
            return httpx.Response(201, json={"id": "uploaded"})
        assert request.url.path.endswith("/attachments")
        return httpx.Response(200, json={"value": state["attachments"]})

    backend = graph_backend(handler)
    try:
        message = OutgoingMessage(to=["target@example.com"], body="Prelude", body_format="text",
                                  reply_all=operation.endswith("_all"), attachments=[OutgoingAttachment("new.txt", b"new")])
        method = backend.create_reply_draft if operation.endswith("_all") else getattr(backend, operation)
        result = method("source", message)
    finally:
        backend.close()
    assert "<p>Prelude</p><p>Original</p>" in state["body"]
    assert "cid:logo" in state["body"]
    assert [a["attachment_id"] for a in result["attachments"]] == ["inherited", "uploaded"]
    assert result["id"] == "draft"
    assert result["folder"] == "drafts"
    assert not any(path.endswith("/send") for _, path in calls)


def test_new_draft_stores_tracking_and_returns_uploaded_ids():
    stored = {}

    def handler(request):
        payload = json.loads(request.content) if request.content else None
        if request.url.path == "/v1.0/me/messages":
            stored.update(payload)
            return httpx.Response(201, json={"id": "new"})
        if request.method == "POST":
            assert payload["contentBytes"] == "eA=="
            return httpx.Response(201, json={"id": "attachment-id"})
        return httpx.Response(200, json={"value": [{"id": "attachment-id", "name": "file"}]})

    backend = graph_backend(handler)
    previous = configure_outbound_tracking("X-Test-Tracking-ID")
    try:
        result = backend.create_draft(OutgoingMessage(to=["to@example.com"], subject="", body="",
            tracking_id="trace", attachments=[OutgoingAttachment("file", b"x")]))
    finally:
        configure_outbound_tracking(previous)
        backend.close()
    assert stored["subject"] == ""
    assert stored["internetMessageHeaders"] == [{"name": "X-Test-Tracking-ID", "value": "trace"}]
    assert result["tracking_id"] == "trace"
    assert result["attachments"][0]["attachment_id"] == "attachment-id"


@pytest.mark.parametrize("fields", [{"subject": ""}, {"body": ""}, {"subject": "new"}, {}])
def test_partial_update_preserves_omitted_fields_recipients_and_files(fields):
    stored = {"subject": "old", "body": {"contentType": "HTML", "content": "old body"},
              "toRecipients": [{"emailAddress": {"address": "kept@example.com"}}],
              "attachments": [{"id": "kept-file", "name": "kept"}]}

    def handler(request):
        if request.url.path.endswith("/attachments"):
            return httpx.Response(200, json={"value": stored["attachments"]})
        if request.method == "PATCH":
            patch = json.loads(request.content)
            assert set(patch) == set(fields)
            stored.update(patch)
            return httpx.Response(200, json={"id": "draft"})
        return httpx.Response(200, json={"isDraft": True})

    backend = graph_backend(handler)
    try:
        result = backend.update_draft("draft", **fields)
    finally:
        backend.close()
    assert stored["subject"] == fields.get("subject", "old")
    assert stored["body"]["content"] == fields.get("body", "old body")
    assert stored["toRecipients"][0]["emailAddress"]["address"] == "kept@example.com"
    assert result["attachments"][0]["attachment_id"] == "kept-file"


def test_update_rejects_sent_message_before_mutation():
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"isDraft": False})

    backend = graph_backend(handler)
    try:
        with pytest.raises(MailBackendError):
            backend.update_draft("sent", subject="changed")
    finally:
        backend.close()


@pytest.mark.parametrize("stage", ["patch", "attachments", "metadata", "send"])
@pytest.mark.parametrize("cleanup", ["retain", "delete", "delete-fails"])
def test_created_draft_failures_expose_recovery_and_cleanup(stage, cleanup):
    deleted = []
    sends = []

    def handler(request):
        if request.url.path.endswith("/createForward"):
            return httpx.Response(201, json={"id": "recoverable", "body": {"contentType": "html", "content": "original"}})
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(403 if cleanup == "delete-fails" else 204)
        current = "patch" if request.method == "PATCH" else ("send" if request.url.path.endswith("/send") else ("metadata" if request.method == "GET" else "attachments"))
        if current == "send":
            sends.append(request)
        if current == stage:
            return httpx.Response(400, json={"error": {"message": "rejected"}})
        return httpx.Response(200, json={"id": "file", "value": []})

    backend = graph_backend(handler)
    try:
        with pytest.raises(MailBackendError) as caught:
            backend.forward_message("source", OutgoingMessage(to=["to@example.com"], body="intro",
                attachments=[OutgoingAttachment("file", b"x")], cleanup_on_failure=cleanup != "retain"))
    finally:
        backend.close()
    details = caught.value.details
    assert details["draft_id"] == "recoverable"
    assert details["folder"] == "drafts"
    assert details["stage"] == stage
    assert details["sent"] is False
    assert details["cleanup"]["deleted"] is (cleanup == "delete")
    assert details["draft_retained"] is (cleanup != "delete")
    assert len(deleted) == (0 if cleanup == "retain" else 1)
    assert len(sends) == (1 if stage == "send" else 0)
    if cleanup == "delete-fails":
        assert "403" in details["cleanup"]["error"]


@pytest.mark.parametrize("operation", ["new", "reply", "reply-all"])
def test_new_and_reply_upload_failures_keep_draft_id(operation):
    def handler(request):
        if request.url.path.endswith("/attachments"):
            return httpx.Response(400)
        expected = "/messages" if operation == "new" else ("/createReplyAll" if operation == "reply-all" else "/createReply")
        assert request.url.path.endswith(expected)
        return httpx.Response(201, json={"id": "recoverable"})

    backend = graph_backend(handler)
    try:
        with pytest.raises(MailBackendError) as caught:
            backend.send_message(OutgoingMessage(to=["to@example.com"], reply_to_message_id=None if operation == "new" else "source",
                reply_all=operation == "reply-all", attachments=[OutgoingAttachment("file", b"x")]))
    finally:
        backend.close()
    assert caught.value.details["draft_id"] == "recoverable"
    assert caught.value.details["stage"] == "attachments"


@pytest.mark.parametrize("failure", ["disconnect", "unavailable"])
def test_uncertain_submission_is_not_retried_or_cleaned_up(failure):
    sends = []

    def handler(request):
        if request.url.path.endswith("/createForward"):
            return httpx.Response(201, json={"id": "draft"})
        if request.method == "GET":
            return httpx.Response(200, json={"value": []})
        if request.url.path.endswith("/attachments"):
            return httpx.Response(201, json={"id": "file"})
        assert request.url.path.endswith("/send"), "uncertain submission must not delete"
        sends.append(request)
        if failure == "disconnect":
            raise httpx.ReadError("response lost", request=request)
        return httpx.Response(503)

    backend = graph_backend(handler)
    try:
        with pytest.raises(MailBackendError) as caught:
            backend.forward_message("source", OutgoingMessage(to=["to@example.com"], cleanup_on_failure=True,
                attachments=[OutgoingAttachment("file", b"x")]))
    finally:
        backend.close()
    assert len(sends) == 1
    assert caught.value.details["submission_uncertain"] is True
    assert caught.value.details["draft_retained"] is None
    assert caught.value.details["cleanup"]["attempted"] is False
    assert caught.value.details["sent"] is False


def test_existing_draft_patch_failure_is_recoverable_without_deletion():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"isDraft": True})
        assert request.method == "PATCH"
        return httpx.Response(403)

    backend = graph_backend(handler)
    try:
        with pytest.raises(MailBackendError) as caught:
            backend.update_draft("saved", body="new")
    finally:
        backend.close()
    assert caught.value.details["draft_id"] == "saved"
    assert caught.value.details["cleanup"]["attempted"] is False


@pytest.mark.parametrize("operation,destination", [("move", "archive"), ("delete", "trash")])
def test_moves_return_new_identity_and_destination(operation, destination):
    def handler(request):
        assert request.url.path.endswith("/move")
        return httpx.Response(201, json={"id": "new-id"})

    backend = graph_backend(handler)
    try:
        result = backend.move_message("old-id", destination) if operation == "move" else backend.delete_message("old-id")
    finally:
        backend.close()
    assert result["id"] == "new-id"
    assert result["previous_id"] == "old-id"
    assert result["folder"] == destination


@pytest.mark.parametrize("operation", ["create_draft", "create_reply_draft", "create_forward_draft"])
def test_ambiguous_creation_never_replays_the_mutation(operation):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    backend = graph_backend(handler)
    try:
        with pytest.raises(MailBackendError) as caught:
            message = OutgoingMessage(to=["to@example.com"], cleanup_on_failure=True)
            method = getattr(backend, operation)
            method(message) if operation == "create_draft" else method("source", message)
    finally:
        backend.close()
    assert len(calls) == 1
    assert caught.value.details["draft_id"] is None
    assert caught.value.details["stage"] == "create"
    assert caught.value.details["draft_retained"] is None
    assert caught.value.details["cleanup"]["attempted"] is False


def test_saved_draft_ambiguous_send_preserves_identity_without_cleanup():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"isDraft": True})
        assert request.url.path.endswith("/send")
        raise httpx.ReadTimeout("response lost", request=request)

    backend = graph_backend(handler)
    try:
        with pytest.raises(MailBackendError) as caught:
            backend.send_draft("saved")
    finally:
        backend.close()
    details = caught.value.details
    assert details["draft_id"] == "saved"
    assert details["submission_uncertain"] is True
    assert details["accepted_by_mail_server"] is None
    assert details["retry_safe"] is False
    assert details["cleanup"]["attempted"] is False


@pytest.mark.parametrize("operation", ["move", "delete"])
def test_move_without_destination_id_does_not_claim_source_id_is_current(operation):
    def handler(request):
        assert request.url.path.endswith("/move")
        return httpx.Response(201, json={})

    backend = graph_backend(handler)
    try:
        with pytest.raises(MailBackendError) as caught:
            backend.move_message("old", "archive") if operation == "move" else backend.delete_message("old")
    finally:
        backend.close()
    assert caught.value.details["id"] is None
    assert caught.value.details["previous_id"] == "old"
    assert caught.value.details["folder"] == ("archive" if operation == "move" else "trash")


def test_rejected_creation_does_not_claim_a_draft_may_exist():
    def handler(request):
        assert request.method == "POST"
        return httpx.Response(403)

    backend = graph_backend(handler)
    try:
        with pytest.raises(MailBackendError) as caught:
            backend.create_draft(OutgoingMessage(to=["to@example.com"], cleanup_on_failure=True))
    finally:
        backend.close()
    assert caught.value.details["draft_retained"] is False
    assert caught.value.details["draft_id"] is None
    assert caught.value.details["cleanup"]["attempted"] is False


def test_successful_creation_without_id_reports_unknown_draft_presence():
    def handler(request):
        assert request.method == "POST"
        return httpx.Response(201, json={})

    backend = graph_backend(handler)
    try:
        with pytest.raises(MailBackendError) as caught:
            backend.create_draft(OutgoingMessage(to=["to@example.com"]))
    finally:
        backend.close()
    assert caught.value.details["draft_retained"] is None
    assert caught.value.details["draft_id"] is None
