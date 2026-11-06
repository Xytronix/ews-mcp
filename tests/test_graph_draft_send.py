"""Stored Graph drafts are submitted by ID, never reconstructed as new mail."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.mail_backends.accounts import MailAccount  # noqa: E402
from src.mail_backends.base import MailBackendError  # noqa: E402
from src.mail_backends.graph_backend import GraphBackend  # noqa: E402
from src.tools import mailbox_tools  # noqa: E402


def graph_backend(handler, *, app_only=False, base_url=None):
    settings = {"client_id": "cid"}
    if not app_only:
        settings["refresh_token"] = "rt"
    if base_url:
        settings["graph_base_url"] = base_url
    backend = GraphBackend(MailAccount(
        email="user@outlook.de", provider="outlook", settings=settings,
    ))
    backend._access_token = "token"
    backend._token_expires_at = 1 << 40
    backend._client = httpx.Client(transport=httpx.MockTransport(handler))
    return backend


def test_stored_draft_send_uses_empty_action_without_rebuilding_attachments():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer token"
        if request.method == "GET":
            assert request.url.path == "/v1.0/me/messages/saved-draft"
            assert "isDraft" in request.url.params["$select"].split(",")
            return httpx.Response(200, json={
                "id": "saved-draft", "isDraft": True,
                "subject": "Stored subject", "bodyPreview": "Stored body",
                "hasAttachments": True,
                "attachments": [
                    {"@odata.type": "#microsoft.graph.itemAttachment", "id": "embedded"},
                    {"@odata.type": "#microsoft.graph.fileAttachment", "id": "inline", "isInline": True},
                ],
            })
        assert request.method == "POST"
        assert request.url.path == "/v1.0/me/messages/saved-draft/send"
        assert request.content == b""
        return httpx.Response(202)

    backend = graph_backend(handler)
    accounts = {backend.email: backend._account}
    backends = {backend.email: backend}
    client = SimpleNamespace(
        config=None,
        mailbox_registry=SimpleNamespace(
            find_account=accounts.get,
            get_backend=backends.__getitem__,
        ),
    )
    result = asyncio.run(mailbox_tools.SendEmailTool(client).execute(
        draft_id="saved-draft", target_mailbox=backend.email,
    ))

    assert [request.method for request in calls] == ["GET", "POST"]
    assert result["success"] is True
    assert result["mailbox"] == backend.email
    assert result["draft_id"] == "saved-draft"
    assert result["backend"] == "graph"
    assert result["sent"] is True
    assert result["accepted_by_mail_server"] is True
    assert result["delivery_confirmed"] is False
    assert "id" not in result
    assert not result.get("tracking_id")


def test_app_only_draft_send_honors_custom_host_and_escapes_id():
    paths = []

    def handler(request):
        assert request.url.host == "graph.example.test"
        paths.append(request.url.raw_path.split(b"?", 1)[0])
        if request.method == "GET":
            return httpx.Response(200, json={"isDraft": True})
        assert request.method == "POST"
        assert request.content == b""
        return httpx.Response(202)

    draft_id = "A/B+C= ?#"
    result = graph_backend(
        handler, app_only=True, base_url="https://graph.example.test/",
    ).send_draft(draft_id)

    expected = b"/v1.0/users/user%40outlook.de/messages/A%2FB%2BC%3D%20%3F%23"
    assert paths == [expected, expected + b"/send"]
    assert result["draft_id"] == draft_id


@pytest.mark.parametrize("metadata", [{"isDraft": False}, {}])
def test_non_draft_or_missing_draft_flag_never_posts(metadata):
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json=metadata)

    with pytest.raises(MailBackendError):
        graph_backend(handler).send_draft("not-a-draft")


def test_missing_draft_never_posts():
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(404, json={
            "error": {"code": "ErrorItemNotFound", "message": "Draft not found"},
        })

    with pytest.raises(MailBackendError, match="404"):
        graph_backend(handler).send_draft("missing")


def test_blank_draft_id_is_rejected_before_any_request():
    def handler(request):
        pytest.fail("Invalid draft IDs must not reach Graph")

    with pytest.raises(MailBackendError):
        graph_backend(handler).send_draft("   ")


def test_dry_run_returns_stored_preview_without_posting():
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={
            "isDraft": True, "subject": "Saved subject", "bodyPreview": "Saved preview",
            "toRecipients": [{"emailAddress": {"address": "to@example.test"}}],
            "ccRecipients": [{"emailAddress": {"address": "cc@example.test"}}],
            "bccRecipients": [{"emailAddress": {"address": "bcc@example.test"}}],
            "hasAttachments": False,
        })

    result = graph_backend(handler).send_draft("saved-draft", dry_run=True)

    assert result["dry_run"] is True
    assert result["sent"] is False
    assert result["would_send_to"] == ["to@example.test"]
    assert result["would_cc"] == ["cc@example.test"]
    assert result["would_bcc"] == ["bcc@example.test"]
    assert result["subject"] == "Saved subject"
    assert result["body_preview"] == "Saved preview"
    assert not result.get("accepted_by_mail_server")


@pytest.mark.parametrize("empty_error", [False, True])
def test_send_failure_is_not_reported_as_acceptance(empty_error):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"isDraft": True})
        assert request.method == "POST"
        if empty_error:
            return httpx.Response(403)
        return httpx.Response(403, json={
            "error": {"code": "ErrorAccessDenied", "message": "Send denied"},
        })

    with pytest.raises(MailBackendError, match="403"):
        graph_backend(handler).send_draft("saved-draft")
