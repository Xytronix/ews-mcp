"""Remote-agent attachment UX: field aliases, reply/forward dry_run, staging."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import stat

import httpx
import pytest

from src import attachment_sources as sources
from src import attachment_staging as staging
from src.exceptions import ValidationError
from src.mail_backends.accounts import MailAccount
from src.middleware.logging import redact_sensitive
from src.tools.mailbox_tools import ForwardEmailTool, ReplyEmailTool
from src.tools.outgoing_attachments import FILES_SCHEMA, validate_outgoing_files
from src.utils import format_error_response
from tests.test_exchange_files import NS, PAYLOAD, attachment_rows, exchange, execute  # noqa: F401

JPEG = b"\xff\xd8\xff\xe0 tiny jpeg \xff\xd9"
JPEG_B64 = base64.b64encode(JPEG).decode("ascii")


@pytest.fixture(autouse=True)
def staging_dir(tmp_path, monkeypatch):
    root = tmp_path / "staged"
    monkeypatch.setenv(staging.STAGING_DIR_ENV, str(root))
    return root


def load(entry, **extra):
    return asyncio.run(sources.load_attachment_content(entry, **extra))


def stage(exchange, **kwargs):
    return asyncio.run(exchange.tools["stage_outgoing_file"].execute(**kwargs))


# ------------------------------------------------------------------- aliases
def test_aliases_map_onto_canonical_fields():
    assert sources.normalize_source_aliases({
        "filename": "a.jpg", "base64": "QQ==", "is_inline": True,
    }) == {"file_name": "a.jpg", "file_content": "QQ==", "is_inline": True}
    assert sources.normalize_source_aliases({"path": "/srv/x"}) == {"file_path": "/srv/x"}
    assert sources.normalize_source_aliases({"filepath": "/srv/x"}) == {"file_path": "/srv/x"}
    assert sources.normalize_source_aliases({"url": "https://x/y"}) == {"file_url": "https://x/y"}
    assert sources.normalize_source_aliases({"data": "QQ=="}) == {"file_content": "QQ=="}
    assert sources.normalize_source_aliases({"content": "QQ=="}) == {"file_content": "QQ=="}


def test_canonical_field_wins_over_its_alias():
    assert sources.normalize_source_aliases(
        {"file_content": "QQ==", "content": "Qg==", "file_name": "a", "filename": "b"}
    ) == {"file_content": "QQ==", "file_name": "a"}


def test_filename_alone_is_not_a_content_source():
    with pytest.raises(ValidationError, match="one content source is required"):
        sources.selected_source({"filename": "photo.jpg"})


def test_alias_for_a_second_source_is_still_ambiguous():
    with pytest.raises(ValidationError, match="exactly one content source"):
        sources.selected_source({"file_url": "https://cdn.example/a.jpg", "data": JPEG_B64})


def test_filename_plus_base64_content_loads():
    loaded = load({"filename": "photo.jpg", "content": JPEG_B64})
    assert (loaded.content, loaded.file_name, loaded.source) == (JPEG, "photo.jpg", "file_content")


def test_validate_outgoing_files_returns_normalised_entries():
    entries = validate_outgoing_files([{"filename": "photo.jpg", "data": JPEG_B64, "is_inline": True}])
    assert entries == [{"file_name": "photo.jpg", "file_content": JPEG_B64, "is_inline": True}]


def test_missing_name_names_the_alias_and_the_entry():
    with pytest.raises(ValidationError, match=r"files\[0\]: file_name is required.*filename"):
        validate_outgoing_files([{"base64": JPEG_B64}])


def test_files_schema_documents_aliases_and_staging():
    assert "filename -> file_name" in FILES_SCHEMA["description"]
    assert "stage_outgoing_file" in FILES_SCHEMA["description"]
    assert "Alias: filename" in FILES_SCHEMA["items"]["properties"]["file_name"]["description"]


@pytest.mark.parametrize("operation", ["send", "reply", "forward"])
def test_alias_spelled_files_reach_ews(exchange, operation):
    execute(exchange, operation, files=[{"filename": "photo.jpg", "content": JPEG_B64}])
    row = attachment_rows(exchange.sent[0])["photo.jpg"]
    assert base64.b64decode(row.findtext("t:Content", namespaces=NS)) == JPEG
    assert row.findtext("t:ContentType", namespaces=NS) == "image/jpeg"


def test_add_attachment_accepts_aliases():
    from src.tools.mailbox_tools import AddAttachmentTool

    captured = {}

    class Backend:
        def add_attachment(self, message_id, name, content, folder, content_type, is_inline, content_id):
            captured.update(name=name, content=content, content_type=content_type)
            return {"id": message_id}

    response = asyncio.run(AddAttachmentTool(_Client()).execute_routed(
        ICLOUD, Backend(), message_id="7", filename="photo.jpg", base64=JPEG_B64,
    ))
    assert response["success"] is True
    assert captured == {"name": "photo.jpg", "content": JPEG, "content_type": "image/jpeg"}


# ------------------------------------------------------------ error guidance
def test_missing_file_path_points_remote_agents_elsewhere():
    with pytest.raises(ValidationError) as exc:
        load({"file_path": "/workspace/agent-box/photo.jpg"})
    assert "File not found on the Email MCP server" in str(exc.value)
    assert "stage_outgoing_file" in str(exc.value)
    for hint in ("file_content", "file_url", "storage_file_id", "stage_outgoing_file"):
        assert hint in exc.value.hint


def test_guidance_survives_the_error_length_cap():
    with pytest.raises(ValidationError) as exc:
        validate_outgoing_files([{"filename": "photo.jpg"}])
    response = format_error_response(exc.value)
    assert len(response["error"]) <= 200
    assert "stage_outgoing_file" in response["error"]
    assert response["hint"] == sources.REMOTE_AGENT_HINT


def test_no_source_error_mentions_staging():
    with pytest.raises(ValidationError, match="stage_outgoing_file"):
        sources.selected_source({})


def test_error_response_without_hint_keeps_its_shape():
    assert "hint" not in format_error_response(ValidationError("plain"))


# ---------------------------------------------------------- reply/forward dry run
def test_exchange_reply_dry_run_sends_nothing_and_skips_fetching(exchange, monkeypatch):
    monkeypatch.setattr(sources, "fetch_url_bytes", lambda *a, **k: pytest.fail("must not fetch"))
    response = execute(exchange, "reply", dry_run=True, files=[
        {"file_url": "https://cdn.example/a.pdf"},
        {"filename": "photo.jpg", "content": JPEG_B64},
    ])
    assert exchange.sent == []
    assert response["dry_run"] is True and response["sent"] is False
    assert response["would_send_to"] == ["author@example.com"]
    assert response["subject"].lower().startswith("re:")
    assert response["files_validated"] is True
    assert response["files_fetched"] is False
    assert response["would_attach"] == 2
    assert response["message_checked"] is True


def test_exchange_reply_dry_run_verify_files_loads_but_does_not_send(exchange):
    response = execute(exchange, "reply", dry_run=True, verify_files=True,
                       files=[{"filename": "photo.jpg", "content": JPEG_B64}])
    assert exchange.sent == []
    assert response["files_fetched"] is True
    assert response["would_attach"] == 1
    assert response["files"] == [
        {"name": "photo.jpg", "size": len(JPEG), "content_type": "image/jpeg", "is_inline": False}
    ]


def test_exchange_reply_dry_run_surfaces_bad_files(exchange):
    with pytest.raises(ValidationError, match="files\\[0\\].*exactly one content source"):
        execute(exchange, "reply", dry_run=True,
                files=[{"file_url": "https://cdn.example/a", "base64": JPEG_B64}])
    assert exchange.sent == []


def test_exchange_reply_dry_run_verify_reports_missing_path(exchange):
    with pytest.raises(ValidationError, match="stage_outgoing_file"):
        execute(exchange, "reply", dry_run=True, verify_files=True,
                files=[{"file_path": "/workspace/photo.jpg"}])
    assert exchange.sent == []


def test_exchange_forward_dry_run(exchange):
    response = execute(exchange, "forward", dry_run=True,
                       files=[{"filename": "photo.jpg", "data": JPEG_B64}])
    assert exchange.sent == []
    assert response["dry_run"] is True
    assert response["would_send_to"] == ["recipient@example.com"]
    assert response["would_attach"] == 1
    assert response["attachments_included"] == 1  # original inline logo


def test_reply_and_forward_schemas_offer_dry_run(exchange):
    for name in ("reply_email", "forward_email", "send_email"):
        properties = exchange.tools[name].get_schema()["inputSchema"]["properties"]
        assert properties["dry_run"]["type"] == "boolean"
        assert properties["verify_files"]["type"] == "boolean"


class _NoSendBackend:
    def send_message(self, message):
        pytest.fail("dry_run must not send")

    def forward_message(self, *args, **kwargs):
        pytest.fail("dry_run must not forward")


class _Client:
    mailbox_registry = None
    config = None


ICLOUD = MailAccount(email="user@icloud.com", provider="icloud", settings={})


def test_routed_reply_dry_run_validates_and_optionally_loads():
    tool = ReplyEmailTool(_Client())
    bare = asyncio.run(tool.execute_routed(
        ICLOUD, _NoSendBackend(), message_id="42", body="hi", dry_run=True,
        files=[{"filename": "photo.jpg", "base64": JPEG_B64}],
    ))
    assert bare["dry_run"] is True and bare["sent"] is False
    assert (bare["files_validated"], bare["files_fetched"], bare["would_attach"]) == (True, False, 1)
    verified = asyncio.run(tool.execute_routed(
        ICLOUD, _NoSendBackend(), message_id="42", body="hi", dry_run=True, verify_files=True,
        files=[{"filename": "photo.jpg", "base64": JPEG_B64}],
    ))
    assert verified["files"][0]["size"] == len(JPEG)


def test_routed_forward_dry_run():
    response = asyncio.run(ForwardEmailTool(_Client()).execute_routed(
        ICLOUD, _NoSendBackend(), message_id="42", to=["peer@example.com"], dry_run=True,
        files=[{"filename": "photo.jpg", "content": JPEG_B64}],
    ))
    assert response["would_send_to"] == ["peer@example.com"]
    assert response["would_attach"] == 1


# ------------------------------------------------------------------- staging
def test_stage_base64_then_attach_by_returned_path(exchange, staging_dir):
    staged = stage(exchange, filename="photo.jpg", content=JPEG_B64, ttl_seconds=600)
    assert staged["success"] is True
    path = staged["file_path"]
    assert path.startswith(str(staging_dir.resolve()))
    assert path.endswith("/photo.jpg")
    assert staged["size_bytes"] == len(JPEG)
    assert staged["content_type"] == "image/jpeg"
    assert staged["expires_at"].endswith("Z")
    assert staged["attach_with"] == {"files": [{"file_path": path}]}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    execute(exchange, "reply", files=staged["attach_with"]["files"])
    row = attachment_rows(exchange.sent[0])["photo.jpg"]
    assert base64.b64decode(row.findtext("t:Content", namespaces=NS)) == JPEG


def test_stage_from_url_uses_ssrf_guarded_fetch(exchange, monkeypatch):
    monkeypatch.setattr(sources, "_resolve_host", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(sources, "_build_client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}))))
    staged = stage(exchange, url="https://cdn.example/pics/cat.jpg")
    assert staged["file_name"] == "cat.jpg"
    assert open(staged["file_path"], "rb").read() == JPEG


def test_stage_refuses_private_urls(exchange, monkeypatch):
    monkeypatch.setattr(sources, "_resolve_host", lambda host: ["127.0.0.1"])
    with pytest.raises(ValidationError, match="file_url refused"):
        stage(exchange, file_url="https://metadata.example/x.jpg")


@pytest.mark.parametrize("name", ["../evil.jpg", "a/b.jpg", "..\\evil.jpg", "..", "/etc/passwd", "a\x00b"])
def test_stage_rejects_path_traversal(exchange, staging_dir, name):
    with pytest.raises(ValidationError, match="bare file name"):
        stage(exchange, file_content=JPEG_B64, file_name=name)
    assert not staging_dir.exists() or not any(staging_dir.iterdir())


def test_stage_requires_a_name_for_base64(exchange):
    with pytest.raises(ValidationError, match="file_name is required"):
        stage(exchange, file_content=JPEG_B64)


def test_stage_refuses_storage_file_id(exchange):
    with pytest.raises(ValidationError, match="straight to"):
        stage(exchange, storage_file_id="GUID")


def test_stage_refuses_oversize(exchange, monkeypatch):
    monkeypatch.setattr("src.tools.staging_tools.MAX_REMOTE_ATTACHMENT_BYTES", 8)
    with pytest.raises(ValidationError, match="at most"):
        stage(exchange, file_content=JPEG_B64, file_name="photo.jpg")


def test_ttl_is_clamped():
    assert staging.clamp_ttl(None) == staging.DEFAULT_TTL_SECONDS
    assert staging.clamp_ttl(10**9) == staging.MAX_TTL_SECONDS
    assert staging.clamp_ttl(1) == staging.MIN_TTL_SECONDS
    with pytest.raises(ValidationError):
        staging.clamp_ttl("soon")


def test_expired_staged_path_is_refused_and_purged(staging_dir):
    staged = staging.stage_bytes(JPEG, "photo.jpg", "image/jpeg", 60, now=1000.0)
    with pytest.raises(ValidationError, match="expired"):
        staging.check_staged_path(staged.file_path, now=2000.0)
    assert not staged.file_path.exists()
    assert not (staging_dir.resolve() / f"{staged.staging_id}.json").exists()


def test_expired_staged_path_fails_the_send_with_guidance(exchange, staging_dir):
    staged = staging.stage_bytes(JPEG, "photo.jpg", "image/jpeg", 60, now=1000.0)
    with pytest.raises(ValidationError, match="expired.*stage_outgoing_file"):
        execute(exchange, "send", files=[{"file_path": str(staged.file_path)}])
    assert exchange.sent == []


def test_staging_purges_expired_entries_lazily(staging_dir):
    old = staging.stage_bytes(JPEG, "old.jpg", "image/jpeg", 60, now=1000.0)
    fresh = staging.stage_bytes(JPEG, "new.jpg", "image/jpeg", 60, now=5000.0)
    assert not old.file_path.exists()
    assert fresh.file_path.exists()
    meta = json.loads((staging_dir.resolve() / f"{fresh.staging_id}.json").read_text())
    assert meta["file_name"] == "new.jpg"


def test_delete_staged_file(exchange):
    staged = stage(exchange, file_content=JPEG_B64, file_name="photo.jpg")
    tool = exchange.tools["delete_staged_file"]
    response = asyncio.run(tool.execute(staging_id=staged["staging_id"]))
    assert response["deleted"] is True
    assert not os.path.exists(staged["file_path"])
    again = asyncio.run(tool.execute(staging_id=staged["staging_id"]))
    assert again["deleted"] is False
    with pytest.raises(ValidationError, match="32-character"):
        asyncio.run(tool.execute(staging_id="../../etc"))


def test_base64_never_reaches_tool_logs(exchange):
    logged = exchange.tools["send_email"]._sanitize_kwargs({
        "files": [{"file_content": JPEG_B64, "file_name": "a.jpg"}, {"data": JPEG_B64}],
        "content": JPEG_B64,
    })
    text = json.dumps(logged)
    assert JPEG_B64 not in text
    assert "a.jpg" in text
    assert redact_sensitive({"metadata": "keep"}) == {"metadata": "keep"}
