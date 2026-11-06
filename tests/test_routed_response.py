"""Response shaping for the routed (IMAP/Graph) mail tools.

Every routed tool merges three sources into one payload: mailbox identity,
the tool's own fields and the backend result. They share keys — a backend
``send_message`` result carries ``replied_to`` and ``backend``, a draft
rewrite carries a new ``id`` — so a response built from two ``**`` splats
blows up with ``TypeError: got multiple values for keyword argument`` *after*
the provider has already accepted the action. These tests pin the merge.

Needs exchangelib (the routed tools subclass the Exchange ones), so run them
inside the image (``docker build -t ews-mcp .``):

    docker run --rm -v "$PWD:/app" -w /app --entrypoint sh \\
        ews-mcp -c "pip install -q pytest && \\
        python -m pytest tests/test_routed_response.py -q"
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from src import attachment_sources as sources  # noqa: E402
from src.exceptions import ValidationError  # noqa: E402
from src.mail_backends.accounts import MailAccount  # noqa: E402
from src.tools.mailbox_tools import (  # noqa: E402
    AddAttachmentTool,
    DeleteEmailTool,
    MoveEmailTool,
    ReplyEmailTool,
    UpdateEmailTool,
)


class _StubClient:
    """The bits of EWSClient that BaseTool.__init__ and the tools touch."""

    mailbox_registry = None
    config = None


class _StubBackend:
    """Returns a canned backend result for whichever verb is called."""

    def __init__(self, result: Dict[str, Any]):
        self.result = result
        self.calls: list = []

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        return self.result

    def __getattr__(self, name):
        return lambda *a, **k: self._record(name, *a, **k)


def run(tool, account, backend, **kwargs) -> Dict[str, Any]:
    return asyncio.run(tool.execute_routed(account, backend, **kwargs))


GRAPH = MailAccount(email="user@outlook.de", provider="outlook", settings={})
IMAP = MailAccount(email="user@icloud.com", provider="icloud", settings={})


def test_reply_merges_the_backend_replied_to_instead_of_crashing():
    """The live 500: Graph accepted the reply, then the response blew up."""
    backend = _StubBackend(
        {
            "sent": True,
            "backend": "graph",
            "to": ["them@example.com"],
            "subject": "Re: hi",
            "replied_to": "MSG-1",
        }
    )
    response = run(
        ReplyEmailTool(_StubClient()),
        GRAPH,
        backend,
        message_id="MSG-1",
        body="thanks",
        reply_all=True,
    )
    assert response["success"] is True
    assert response["replied_to"] == "MSG-1"
    assert response["reply_all"] is True
    assert response["sent"] is True
    assert response["backend"] == "graph"
    assert response["mailbox"] == "user@outlook.de"
    assert response["provider"] == "outlook"


def test_backend_result_wins_over_the_tool_field():
    """IMAP replaces a draft, so its new id must survive the merge."""
    backend = _StubBackend(
        {
            "added": True,
            "backend": "imap",
            "id": "UID-9",
            "previous_id": "UID-8",
            "folder": "drafts",
            "attachment_name": "invoice.pdf",
            "attachment_size": 3,
        }
    )
    response = run(
        AddAttachmentTool(_StubClient()),
        IMAP,
        backend,
        message_id="UID-8",
        file_name="invoice.pdf",
        file_content="AAAA",
        folder="drafts",
    )
    assert response["id"] == "UID-9"
    assert response["previous_id"] == "UID-8"
    assert response["message_id"] == "UID-9"
    assert response["attachment_name"] == "invoice.pdf"
    assert response["backend"] == "imap"


@pytest.mark.parametrize(
    "tool_cls,kwargs,result,expected",
    [
        (
            MoveEmailTool,
            {"message_id": "M1", "destination_folder": "Archive"},
            {"moved": True, "backend": "graph", "id": "M2", "destination": "archive"},
            {"moved": True, "id": "M2", "destination_folder": "archive"},
        ),
        (
            DeleteEmailTool,
            {"message_id": "M1", "permanent": True},
            {"deleted": True, "hard_delete": True, "backend": "imap", "id": "M1"},
            {"deleted": True, "hard_delete": True, "permanent": True},
        ),
        (
            UpdateEmailTool,
            {"message_id": "M1", "is_read": True},
            {"changed": True, "backend": "imap", "id": "M1", "is_read": True},
            {"changed": True, "message_id": "M1"},
        ),
    ],
)
def test_routed_verbs_never_double_key(tool_cls, kwargs, result, expected):
    response = run(tool_cls(_StubClient()), IMAP, _StubBackend(result), **kwargs)
    assert response["success"] is True
    for key, value in expected.items():
        assert response[key] == value
    assert response["mailbox"] == IMAP.email


def test_routed_response_precedence_is_identity_then_field_then_result():
    tool = ReplyEmailTool(_StubClient())
    response = tool.routed_response(
        "msg",
        GRAPH,
        {"backend": "graph", "folder": "sent"},
        backend="ignored",
        folder="inbox",
        extra=1,
    )
    assert response == {
        "success": True,
        "message": "msg",
        "mailbox": "user@outlook.de",
        "provider": "outlook",
        "backend": "graph",
        "folder": "sent",
        "extra": 1,
    }


# ------------------------------------------- remote add_attachment sources
def test_routed_add_attachment_fetches_a_url_and_names_it_from_the_response(monkeypatch):
    """No shared filesystem with a remote agent: the server fetches the bytes itself."""
    def handler(request):
        assert str(request.url) == "https://cdn.example/d?id=7"
        return httpx.Response(
            200,
            content=b"%PDF-1.4 tiny",
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="invoice 42.pdf"',
            },
        )

    monkeypatch.setattr(sources, "_resolve_host", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(
        sources,
        "_build_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
    )

    backend = _StubBackend({"added": True, "backend": "graph", "id": "MID"})
    response = run(
        AddAttachmentTool(_StubClient()),
        GRAPH,
        backend,
        message_id="MID",
        file_url="https://cdn.example/d?id=7",
        folder="drafts",
    )
    _, args, _ = backend.calls[0]
    assert args[1] == "invoice 42.pdf"
    assert args[2] == b"%PDF-1.4 tiny"
    assert args[4] == "application/pdf"
    assert response["source"] == "file_url"
    assert response["source_detail"] == "https://cdn.example/d?id=7"


def test_routed_add_attachment_refuses_storage_ids_with_a_usable_alternative():
    """iCloud/Outlook mailboxes have no SmarterMail drive to download from."""
    with pytest.raises(ValidationError, match="user@icloud.com.*file_url or file_content"):
        run(
            AddAttachmentTool(_StubClient()),
            IMAP,
            _StubBackend({}),
            message_id="UID-1",
            storage_file_id="GUID-1",
        )


def test_routed_add_attachment_requires_exactly_one_source():
    with pytest.raises(ValidationError, match="exactly one content source"):
        run(
            AddAttachmentTool(_StubClient()),
            IMAP,
            _StubBackend({}),
            message_id="UID-1",
            file_url="https://cdn.example/x.pdf",
            file_content="AAAA",
        )


def test_add_attachment_schema_advertises_every_source():
    schema = AddAttachmentTool(_StubClient()).get_schema()["inputSchema"]["properties"]
    assert set(sources.SOURCE_FIELDS) <= set(schema)
    assert "file_name" in schema and "folder" in schema
