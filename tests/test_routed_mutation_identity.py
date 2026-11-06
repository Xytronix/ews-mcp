"""Public routed mutation results remain usable after a draft UID changes."""
from __future__ import annotations

import asyncio
import base64
import email
import email.policy
import sys
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.mail_backends.accounts import MailAccount
from src.mail_backends.base import MailBackendError
from src.mail_backends.imap_backend import IMAPBackend
from src.tools.mailbox_tools import (
    AddAttachmentTool,
    AttachEmailToDraftTool,
    DeleteAttachmentTool,
    DeleteEmailTool,
    DownloadAttachmentTool,
    ListAttachmentsTool,
    MoveEmailTool,
)


@pytest.fixture
def mailbox(monkeypatch):
    account = MailAccount(
        email="rewrite@icloud.com", provider="icloud", password="unused", settings={}
    )
    backend = IMAPBackend(account)
    draft = EmailMessage()
    draft.set_content("Draft body")
    draft.add_attachment(b"remove me", maintype="application", subtype="octet-stream", filename="remove.bin")
    draft.add_attachment(b"keep me", maintype="application", subtype="octet-stream", filename="keep.bin")
    source = EmailMessage()
    source["Subject"] = "Source mail"
    source.set_content("Source body")
    stored = {
        ("Drafts/Review", "7:41"): draft.as_bytes(),
        ("inbox", "9:22"): source.as_bytes(),
    }

    def fetch(message_id, folder, writable=False):
        try:
            raw = stored[(folder, message_id)]
        except KeyError:
            raise MailBackendError("Message identity is stale or belongs to another folder")
        validity, uid = message_id.split(":")
        return validity, uid, {"raw": raw, "flags": ["\\Draft"] if folder == "Drafts/Review" else []}

    def rewrite(message_id, folder, mutate, operation):
        # Simulate only the protocol persistence boundary; MIME mutation and
        # attachment indexing still run in the real backend.
        _, _, meta = fetch(message_id, folder)
        message = email.message_from_bytes(meta["raw"], policy=email.policy.default)
        mutate(message)
        new_id = "7:42"
        stored[(folder, new_id)] = message.as_bytes()
        del stored[(folder, message_id)]
        return new_id, backend.list_attachments(new_id, folder), {"original_removed": True}

    monkeypatch.setattr(backend, "_fetch_full", fetch)
    monkeypatch.setattr(backend, "_rewrite_draft", rewrite)

    class Registry:
        def find_account(self, address):
            assert address == account.email
            return account

        def get_backend(self, address):
            assert address == account.email
            return backend

    return SimpleNamespace(mailbox_registry=Registry(), config=None), backend, account


def execute(tool_cls, client, **kwargs):
    return asyncio.run(tool_cls(client).execute(**kwargs))


@pytest.mark.parametrize(
    "tool_cls,arguments",
    [
        (AddAttachmentTool, {"message_id": "7:41", "file_name": "new.txt", "file_content": "bmV3"}),
        (DeleteAttachmentTool, {"message_id": "7:41", "attachment_name": "remove.bin"}),
        (AttachEmailToDraftTool, {"draft_id": "7:41", "source_message_id": "9:22", "source_folder": "inbox"}),
    ],
)
def test_attachment_mutation_returns_a_reusable_message_and_attachment_identity(mailbox, tool_cls, arguments):
    client, backend, account = mailbox
    response = execute(tool_cls, client, target_mailbox=account.email, folder="Drafts/Review", **arguments)

    assert response["message_id"] == response["id"] == "7:42"
    assert response["previous_id"] == "7:41"
    assert response["mailbox"] == account.email
    assert response["folder"] == "Drafts/Review"
    assert response["backend"] == "imap"
    if tool_cls is AttachEmailToDraftTool:
        assert response["draft_id"] == "7:42"
        assert response["source_message_id"] == "9:22"
        assert response["source_folder"] == "inbox"

    address = {
        "target_mailbox": response["mailbox"],
        "folder": response["folder"],
        "message_id": response["message_id"],
    }
    listing = execute(ListAttachmentsTool, client, **address)
    returned = {item["name"]: item["attachment_id"] for item in response["attachments"]}
    listed = {item["name"]: item["id"] for item in listing["attachments"]}
    assert returned == listed
    kept = execute(DownloadAttachmentTool, client, attachment_id=returned["keep.bin"], **address)
    assert base64.b64decode(kept["content_base64"]) == b"keep me"
    if tool_cls is DeleteAttachmentTool:
        # Removing the first part reindexes keep.bin from part-3 to part-2.
        assert returned["keep.bin"] == "part-2"
        assert "remove.bin" not in returned
    with pytest.raises(MailBackendError):
        backend.get_mime("7:41", "Drafts/Review")


@pytest.mark.parametrize(
    "tool_cls,arguments,method",
    [
        (MoveEmailTool, {"destination_folder": "archive"}, "move_message"),
        (DeleteEmailTool, {}, "delete_message"),
    ],
)
@pytest.mark.parametrize("new_id", ["11:80", None])
def test_relocated_identity_never_uses_the_source_id(mailbox, monkeypatch, tool_cls, arguments, method, new_id):
    client, backend, account = mailbox
    destination = "archive" if tool_cls is MoveEmailTool else "trash"
    monkeypatch.setattr(backend, method, lambda *args: {
        "id": new_id, "previous_id": "7:41", "folder": destination, "backend": "imap",
    })
    response = execute(tool_cls, client, target_mailbox=account.email, message_id="7:41", folder="Drafts/Review", **arguments)
    assert response["message_id"] == new_id
    assert response["previous_id"] == "7:41"
    assert (response["mailbox"], response["folder"]) == (account.email, destination)
