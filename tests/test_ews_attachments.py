"""EWS attachment payload handling: base64, MIME type, embedded messages.

Run inside the image (needs exchangelib; build it with ``docker build -t ews-mcp .``):

    docker run --rm -v "$PWD:/app" -w /app --entrypoint sh \\
        ews-mcp -c "pip install -q pytest && \\
        python -m pytest tests/test_ews_attachments.py -q"
"""
from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from exchangelib import FileAttachment, ItemAttachment, Mailbox, Message

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import attachment_sources as sources  # noqa: E402
from src.exceptions import ToolExecutionError, ValidationError  # noqa: E402
from src.smartermail import SmarterMailError  # noqa: E402
from src.tools import attachment_tools  # noqa: E402
from src.tools.attachment_tools import (  # noqa: E402
    AddAttachmentTool,
    attachment_bytes,
    attachment_content_type,
    build_embedded_message,
    inline_content_id,
)
from src.utils import decode_base64_payload, guess_content_type  # noqa: E402
from tests.support import offline_account  # noqa: E402

PDF = b"%PDF-1.4 tiny"
PDF_B64 = base64.b64encode(PDF).decode("ascii")


# ------------------------------------------------------------------ base64
def test_data_url_prefix_no_longer_decodes_to_garbage():
    """b64decode(validate=False) drops ':;,/' and returns corrupt bytes."""
    payload = f"data:application/pdf;base64,{PDF_B64}"
    assert base64.b64decode(payload) != PDF  # the old, silent corruption
    assert decode_base64_payload(payload) == PDF


def test_wrapped_and_unpadded_base64_are_accepted():
    wrapped = f"{PDF_B64[:8]}\n  {PDF_B64[8:]}"
    assert decode_base64_payload(wrapped) == PDF
    assert decode_base64_payload(PDF_B64.rstrip("=")) == PDF


def test_non_base64_is_rejected_with_a_usable_message():
    with pytest.raises(ValidationError, match="not valid base64"):
        decode_base64_payload("this is not base64!!")
    with pytest.raises(ValidationError, match="empty"):
        decode_base64_payload("   ")


# -------------------------------------------------------------- mime types
def test_content_type_follows_the_file_name_unless_pinned():
    assert guess_content_type("report.pdf") == "application/pdf"
    assert guess_content_type("logo.png") == "image/png"
    assert guess_content_type("blob.unknownext") == "application/octet-stream"
    assert guess_content_type("report.pdf", "text/plain") == "text/plain"


# --------------------------------------------------------------- payloads
def test_embedded_message_is_served_as_eml_instead_of_reported_empty():
    """ItemAttachment has no .content — reading it reported every embedded
    e-mail as 'Attachment content is empty'."""
    mime = b"From: a@b.c\r\nSubject: hi\r\n\r\nbody"
    attachment = ItemAttachment(name="Fwd: hi", item=Message(mime_content=mime))
    assert not hasattr(attachment, "content")

    content, name = attachment_bytes(attachment)
    assert content == mime
    assert name == "Fwd: hi.eml"
    assert attachment_content_type(attachment, name) == "message/rfc822"


def test_embedding_prefers_raw_mime_so_the_nested_mail_stays_whole():
    """MimeContent *is* the item for EWS: sending fields beside it conflicts.

    Only the raw MIME carries the source's own attachments and its original
    From/Date/Message-ID, so it must win and never be mixed with a field copy.
    """
    account = offline_account()
    mime = b"From: a@b.c\r\nSubject: hi\r\n\r\nbody"
    source = SimpleNamespace(
        mime_content=mime,
        subject="hi",
        body="body",
        to_recipients=[Mailbox(email_address="user@example.com")],
    )

    embedded, fidelity = build_embedded_message(account, source)

    assert fidelity == "mime"
    assert embedded.mime_content == mime
    assert embedded.subject is None
    assert embedded.to_recipients is None


def test_embedding_without_mime_falls_back_to_a_field_copy():
    account = offline_account()
    source = SimpleNamespace(
        mime_content=None,
        subject="quarterly numbers",
        body="the body",
        to_recipients=[Mailbox(email_address="user@example.com")],
    )

    embedded, fidelity = build_embedded_message(account, source)

    assert fidelity == "fields"
    assert embedded.subject == "quarterly numbers"
    assert embedded.mime_content is None


def test_file_attachment_content_and_guessed_type():
    attachment = FileAttachment(name="report.pdf", content=PDF)
    content, name = attachment_bytes(attachment)
    assert (content, name) == (PDF, "report.pdf")
    assert attachment_content_type(attachment, name) == "application/pdf"


def test_fetch_failure_is_not_reported_as_an_empty_attachment():
    class Failing:
        name = "big.zip"

        @property
        def content(self):
            raise ConnectionResetError("connection reset by peer")

    with pytest.raises(ToolExecutionError, match="Failed to fetch attachment content"):
        attachment_bytes(Failing())


def test_genuinely_empty_attachment_still_says_empty():
    with pytest.raises(ToolExecutionError, match="content is empty"):
        attachment_bytes(FileAttachment(name="void.txt", content=b""))


# ------------------------------------------------- remote content sources
class _FakeFolder:
    def __init__(self, message=None):
        self.message = message

    def get(self, id=None):
        if self.message is None:
            raise Exception(f"not found: {id}")
        return self.message


class _FakeMessage:
    def __init__(self):
        self.attached = []

    def attach(self, attachment):
        attachment.attachment_id = "AID-1"
        self.attached.append(attachment)


class _FakeEWSClient:
    """Only what AddAttachmentTool.execute touches on the Exchange path."""

    mailbox_registry = None

    class config:
        ews_email = "user@example.com"

    def __init__(self, message):
        folder = _FakeFolder(message)
        self.account = SimpleNamespace(drafts=folder, inbox=_FakeFolder(), sent=_FakeFolder())

    def get_account(self, target_mailbox=None):
        return self.account


def _exchange_tool(message=None):
    return AddAttachmentTool(_FakeEWSClient(message if message is not None else _FakeMessage()))


def test_exchange_add_attachment_validates_the_source_before_any_mailbox_call():
    """A bad source must not cost an EWS round trip (or a 25MB fetch)."""
    tool = AddAttachmentTool(object())  # no client surface at all
    with pytest.raises(ValidationError, match="one content source is required"):
        asyncio.run(tool.execute(message_id="M1"))
    with pytest.raises(ValidationError, match="exactly one content source"):
        asyncio.run(
            tool.execute(
                message_id="M1", file_url="https://cdn.example/a.pdf", file_content="AAAA"
            )
        )


def test_exchange_add_attachment_fetches_a_url(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            content=PDF,
            headers={
                "content-type": "application/pdf",
                "content-disposition": "attachment; filename=quote.pdf",
            },
        )

    monkeypatch.setattr(sources, "_resolve_host", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(
        sources,
        "_build_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
    )

    message = _FakeMessage()
    response = asyncio.run(
        _exchange_tool(message).execute(
            message_id="M1", file_url="https://cdn.example/dl"
        )
    )
    attachment = message.attached[0]
    assert (attachment.name, attachment.content) == ("quote.pdf", PDF)
    assert attachment.content_type == "application/pdf"
    assert response["attachment_name"] == "quote.pdf"
    assert response["attachment_size"] == len(PDF)
    assert response["source"] == "file_url"
    assert response["source_detail"] == "https://cdn.example/dl"


def test_exchange_add_attachment_downloads_a_storage_file(monkeypatch):
    class _Client:
        def download(self, method, path, **kw):
            assert (method, path) == ("GET", "/api/v1/filestorage/GUID-1/download")
            return PDF

        def request(self, method, path, **kw):
            return {"folder": {"id": 1, "files": [{"id": "GUID-1", "fileName": "stored.pdf"}]}}

    monkeypatch.setattr(
        attachment_tools, "smartermail_client", lambda ews_client, mailbox=None: _Client()
    )
    message = _FakeMessage()
    response = asyncio.run(
        _exchange_tool(message).execute(message_id="M1", storage_file_id="GUID-1")
    )
    attachment = message.attached[0]
    assert (attachment.name, attachment.content) == ("stored.pdf", PDF)
    assert attachment.content_type == "application/pdf"
    assert response["source"] == "storage_file_id"
    assert response["source_detail"] == "GUID-1"


def test_exchange_add_attachment_reports_a_storage_failure(monkeypatch):
    class _Client:
        def download(self, method, path, **kw):
            raise SmarterMailError("HTTP 404 file not found")

    monkeypatch.setattr(
        attachment_tools, "smartermail_client", lambda ews_client, mailbox=None: _Client()
    )
    with pytest.raises(ToolExecutionError, match="storage_file_id GUID-9: HTTP 404"):
        asyncio.run(_exchange_tool().execute(message_id="M1", storage_file_id="GUID-9"))


# ------------------------------------------ attach_email_to_draft response
def _source(mime=None, **overrides):
    fields = dict(
        mime_content=mime,
        subject="Quarterly report",
        body="<p>see attached</p>",
        text_body="see attached",
        to_recipients=[],
        cc_recipients=[],
        bcc_recipients=[],
        sender=None,
        author=None,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


class _FakeDraft:
    def __init__(self):
        self.attached = []

    def attach(self, attachment):
        attachment.attachment_id = "AID-EML"
        self.attached.append(attachment)


def _attach_email(monkeypatch, source):
    draft = _FakeDraft()
    monkeypatch.setattr(
        attachment_tools,
        "find_message_for_account",
        lambda account, message_id: draft if message_id == "DRAFT" else source,
    )
    client = _FakeEWSClient(_FakeMessage())
    # exchangelib refuses a stub in Message(account=...).
    client.account = offline_account()
    tool = attachment_tools.AttachEmailToDraftTool(client)
    response = asyncio.run(tool.execute(draft_id="DRAFT", source_message_id="SRC"))
    return draft, response


def test_attach_email_to_draft_reports_a_faithful_embed(monkeypatch):
    draft, response = _attach_email(monkeypatch, _source(mime=b"RAW MIME"))
    assert isinstance(draft.attached[0], ItemAttachment)
    assert draft.attached[0].name == "Quarterly report.eml"
    assert response["embedded_from"] == "mime"
    assert "note" not in response


def test_attach_email_to_draft_says_when_the_copy_is_lossy(monkeypatch):
    """A field copy silently drops the source's attachments — say so."""
    _draft, response = _attach_email(monkeypatch, _source(mime=None))
    assert response["embedded_from"] == "fields"
    assert "attachments" in response["note"]


# ------------------------------------------------------------ inline parts
def test_inline_attachment_gets_a_cid_the_html_body_can_reference():
    assert inline_content_id("My Logo.png", True, None) == "My-Logo.png"
    assert inline_content_id("x.png", True, "<abc>") == "abc"
    assert inline_content_id("x.png", False, None) is None
    # An explicit id is honoured even for a non-inline part (Outlook uses it
    # to de-duplicate), but nothing is invented for one.
    assert inline_content_id("x.png", False, "abc") == "abc"


def test_exchange_add_attachment_makes_a_url_image_inline(monkeypatch):
    """file_url + is_inline is the no-base64 path to an inline HTML image."""
    png = b"\x89PNG\r\n\x1a\n"

    def handler(request):
        return httpx.Response(200, content=png, headers={"content-type": "image/png"})

    monkeypatch.setattr(sources, "_resolve_host", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(
        sources,
        "_build_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
    )
    message = _FakeMessage()
    response = asyncio.run(
        _exchange_tool(message).execute(
            message_id="M1", file_url="https://cdn.example/logo.png", is_inline=True
        )
    )
    attachment = message.attached[0]
    assert attachment.is_inline is True
    assert attachment.content_id == "logo.png"
    assert attachment.content_type == "image/png"
    assert response["content_id"] == "logo.png"
