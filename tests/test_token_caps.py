"""Token-budget caps: attachment clipping, oversize refuse, markdown default, admin catalog."""
from __future__ import annotations

import asyncio
import base64
import logging
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from exchangelib import FileAttachment

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.exceptions import ValidationError  # noqa: E402
from src.main import EWSMCPServer  # noqa: E402
from src.tools import attachment_tools as attachment_mod  # noqa: E402
from src.tools.attachment_tools import (  # noqa: E402
    DEFAULT_DOWNLOAD_MAX_BYTES,
    DEFAULT_READ_ATTACHMENT_CHARS,
    clip_extracted_text,
)
from src.tools.email_tools import GetEmailsBulkTool  # noqa: E402
from src.tools.mailbox_tools import (  # noqa: E402
    DownloadAttachmentTool,
    GetEmailDetailsTool,
    ReadAttachmentTool,
)
from src.tools.smartermail_admin_tools import (  # noqa: E402
    SMARTERMAIL_TOOLS as SM_ADMIN_TOOLS,
)
from src.utils import LIST_DEFAULT_FIELDS  # noqa: E402


HTML_BODY = "<p>Hello <strong>world</strong></p>"


class _FakeClient:
    """Enough of EWSClient for execute() without a mailbox."""

    def __init__(self):
        self.config = SimpleNamespace(ews_email="user@example.com")
        self.mailbox_registry = SimpleNamespace(find_account=lambda address: None)
        self.sqlite_cache = None

    def get_account(self, target_mailbox=None):
        return SimpleNamespace()


def _run(coro):
    return asyncio.run(coro)


def _message_with_file(name: str, content: bytes):
    return SimpleNamespace(
        attachments=[FileAttachment(name=name, content=content)],
        text_body="",
        body="",
        subject="x",
        mime_content=None,
    )


# ------------------------------------------------------------------ clip helper
def test_clip_extracted_text_pages_and_flags_truncated():
    text = "Z" * 9000
    first = clip_extracted_text(text)
    assert first["truncated"] is True
    assert first["content_length"] == 9000
    assert first["content"] == "Z" * DEFAULT_READ_ATTACHMENT_CHARS
    assert len(first["content"]) == 8000

    rest = clip_extracted_text(text, offset=8000)
    assert rest["content"] == "Z" * 1000
    assert rest["truncated"] is False
    assert rest["content_length"] == 9000


# ----------------------------------------------------------- read_attachment
def test_read_attachment_truncates_at_8000(monkeypatch):
    message = _message_with_file("notes.txt", b"hello")
    monkeypatch.setattr(
        attachment_mod, "find_message_for_account", lambda *a, **k: message
    )
    monkeypatch.setattr(
        attachment_mod,
        "_extract_with_password_fallback",
        lambda *a, **k: ("Z" * 9000, None),
    )

    result = _run(
        ReadAttachmentTool(_FakeClient()).execute(
            message_id="MID", attachment_name="notes.txt"
        )
    )
    assert result["success"] is True
    assert result["truncated"] is True
    assert result["content_length"] == 9000
    assert result["content"] == "Z" * 8000
    assert len(result["content"]) == 8000


# ------------------------------------------------------- download_attachment
def test_download_attachment_small_still_returns_base64(monkeypatch):
    payload = b"hello"
    message = _message_with_file("tiny.bin", payload)
    monkeypatch.setattr(
        attachment_mod, "find_message_for_account", lambda *a, **k: message
    )
    result = _run(
        DownloadAttachmentTool(_FakeClient()).execute(
            message_id="MID", attachment_name="tiny.bin"
        )
    )
    assert result["success"] is True
    assert result["content_base64"] == base64.b64encode(payload).decode("utf-8")
    schema = DownloadAttachmentTool(_FakeClient()).get_schema()
    assert schema["inputSchema"]["properties"]["return_as"]["default"] == "base64"


def test_download_attachment_refuses_oversize(monkeypatch):
    size = DEFAULT_DOWNLOAD_MAX_BYTES + 1
    message = _message_with_file("big.bin", b"x" * size)
    monkeypatch.setattr(
        attachment_mod, "find_message_for_account", lambda *a, **k: message
    )
    with pytest.raises(ValidationError, match=str(size)) as caught:
        _run(
            DownloadAttachmentTool(_FakeClient()).execute(
                message_id="MID", attachment_name="big.bin"
            )
        )
    err = str(caught.value)
    assert "max_bytes" in err
    assert "read_attachment" in err
    assert "file_url" in err
    assert "storage_file_id" in err


# -------------------------------------------------------- get_email_details
def _detail_message():
    return SimpleNamespace(
        id="MID-1",
        subject="Hi",
        sender=SimpleNamespace(email_address="a@b.com"),
        to_recipients=[],
        cc_recipients=[],
        attachments=[],
        text_body="Hello world",
        body=HTML_BODY,
        datetime_received=datetime(2026, 1, 1),
        datetime_sent=datetime(2026, 1, 1),
        is_read=True,
        has_attachments=False,
        importance="Normal",
    )


def test_get_email_details_defaults_to_markdown(monkeypatch):
    from src.tools import email_tools as email_mod

    monkeypatch.setattr(
        email_mod, "find_message_for_account", lambda *a, **k: _detail_message()
    )
    tool = GetEmailDetailsTool(_FakeClient())
    assert (
        tool.get_schema()["inputSchema"]["properties"]["format"]["default"]
        == "markdown"
    )
    result = _run(tool.execute(message_id="MID-1"))
    email = result["email"]
    assert email["body_format"] == "markdown"
    assert "Hello" in email["body"]
    assert "<p>" not in email["body"]
    assert email["body_html"] == ""


def test_get_email_details_html_escape_hatch(monkeypatch):
    from src.tools import email_tools as email_mod

    monkeypatch.setattr(
        email_mod, "find_message_for_account", lambda *a, **k: _detail_message()
    )
    result = _run(
        GetEmailDetailsTool(_FakeClient()).execute(
            message_id="MID-1", format="html"
        )
    )
    email = result["email"]
    assert email["body_format"] == "html"
    assert email["body"] == HTML_BODY
    assert email["body_html"] == HTML_BODY


# ---------------------------------------------------------- get_emails_bulk
def test_get_emails_bulk_defaults_to_list_fields():
    msg = SimpleNamespace(
        id="m1",
        subject="Subj",
        sender=SimpleNamespace(email_address="a@b.com"),
        to_recipients=[],
        cc_recipients=[],
        attachments=[],
        text_body="A" * 500,
        body="<p>html</p>",
        datetime_received=datetime(2026, 1, 1),
        datetime_sent=datetime(2026, 1, 1),
        is_read=True,
        has_attachments=False,
        importance="Normal",
    )
    listed = GetEmailsBulkTool._message_to_dict(
        msg, fields=list(LIST_DEFAULT_FIELDS)
    )
    assert "body" not in listed
    assert "body_html" not in listed
    assert "snippet" in listed
    assert listed["subject"] == "Subj"

    opted = GetEmailsBulkTool._message_to_dict(msg, fields=["subject", "body"])
    assert opted["body"] == "A" * 500
    assert "snippet" not in opted


# ----------------------------------------------------------- admin catalog
def _admin_tool_names() -> set[str]:
    client = SimpleNamespace(
        config=SimpleNamespace(
            smartermail_admin_tools=True, ews_email="probe@example.com"
        )
    )
    return {cls(client).get_schema()["name"] for cls in SM_ADMIN_TOOLS}


def _register_catalog(*, admin: bool) -> set[str]:
    settings = SimpleNamespace(
        enable_email=True,
        enable_calendar=False,
        enable_contacts=False,
        enable_tasks=False,
        enable_ai=False,
        enable_agent=False,
        enable_semantic_search=False,
        smartermail_enabled=True,
        smartermail_admin_tools=admin,
    )
    server = SimpleNamespace(
        settings=settings,
        logger=logging.getLogger("token-caps-catalog"),
        tools={},
        ews_client=SimpleNamespace(config=settings),
        server=object(),
        openapi_adapter=None,
    )
    EWSMCPServer.register_tools(server)
    return set(server.tools)


def test_admin_tools_absent_from_catalog_when_flag_off():
    admin_names = _admin_tool_names()
    assert any(name.startswith("manage_domain_") for name in admin_names)
    catalog = _register_catalog(admin=False)
    assert admin_names.isdisjoint(catalog)
    assert not any(name.startswith("manage_domain_") for name in catalog)


def test_admin_tools_present_in_catalog_when_flag_on():
    admin_names = _admin_tool_names()
    catalog = _register_catalog(admin=True)
    missing = admin_names - catalog
    assert not missing, f"admin tools missing from catalog: {sorted(missing)}"
