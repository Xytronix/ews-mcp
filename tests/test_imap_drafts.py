"""Draft workflows exercise real MIME parsing over a stateful IMAP boundary."""
from __future__ import annotations

import email
import email.policy
from email.message import EmailMessage

import pytest

from src.mail_backends.base import MailBackendError, OutgoingAttachment, OutgoingMessage
from test_imap_safety import mailbox  # noqa: F401


def parsed(backend, result):
    return email.message_from_bytes(backend.get_mime(result["id"], result["folder"]), policy=email.policy.default)


def source(mailbox):
    backend, server = mailbox
    original = EmailMessage()
    original["From"] = "Sender <sender@example.net>"
    original["Reply-To"] = "reply@example.net"
    original["To"] = "author@example.com, other@example.net"
    original["Cc"] = "copy@example.net, reply@example.net"
    original["Bcc"] = "secret@example.net"
    original["Subject"] = "Report"
    original["Message-ID"] = "<original@example.net>"
    original["References"] = "<ancestor@example.net>"
    original.set_content("Original plain <not-html>")
    original.add_alternative('<p>Original HTML <img src="cid:logo"></p>', subtype="html")
    original.get_payload()[1].add_related(b"image", maintype="image", subtype="png", cid="<logo>", filename="logo.png", disposition="inline")
    nested = EmailMessage()
    nested["Subject"] = "Attached"
    nested.set_content("Nested untouched")
    original.add_attachment(nested, filename="original.eml")
    original.add_attachment(b"File untouched", maintype="text", subtype="plain", filename="notes.txt")
    server.messages["INBOX"]["7"] = [original.as_bytes(), set()]
    return original


@pytest.fixture(autouse=True)
def forbid_smtp(mailbox, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("draft operation submitted SMTP")
    monkeypatch.setattr(mailbox[0], "_smtp_send", unexpected)


def test_compose_draft_is_readable_with_recipients_and_cid_files(mailbox):
    backend, server = mailbox
    result = backend.create_draft(OutgoingMessage(
        to=["to@example.net"], cc=["cc@example.net"], bcc=["hidden@example.net"],
        subject="New draft", body='<p>Review <img src="cid:new-logo"></p>', body_format="html",
        attachments=[OutgoingAttachment("new.png", b"png", "image/png", True, "new-logo")],
    ))
    stored = parsed(backend, result)
    assert stored["Bcc"] == "hidden@example.net"
    assert stored["Cc"] == "cc@example.net"
    assert "Review" in stored.get_body(preferencelist=("plain",)).get_content()
    assert 'cid:new-logo' in stored.get_body(preferencelist=("html",)).get_content()
    assert "\\Draft" in server.messages["Drafts"][result["id"].split(":")[1]][1]
    assert backend.get_attachment(result["id"], result["folder"], attachment_id=result["attachments"][0]["attachment_id"]).content == b"png"
    preview = backend.send_draft(result["id"], result["folder"], dry_run=True)
    assert preview["sent"] is False


def test_reply_all_draft_preserves_thread_and_quotes_only_outer_body(mailbox):
    source(mailbox)
    backend, _ = mailbox
    result = backend.create_reply_draft("41:7", OutgoingMessage(to=[], body="My reply", reply_all=True))
    stored = parsed(backend, result)
    assert stored["To"] == "reply@example.net, other@example.net"
    assert stored["Cc"] == "copy@example.net"
    assert stored["Bcc"] is None
    assert stored["In-Reply-To"] == "<original@example.net>"
    assert stored["References"] == "<ancestor@example.net> <original@example.net>"
    assert stored["Subject"] == "Re: Report"
    html = stored.get_body(preferencelist=("html",)).get_content()
    text = stored.get_body(preferencelist=("plain",)).get_content()
    assert "My reply" in html and "Original plain &lt;not-html&gt;" in html
    assert "> Original plain <not-html>" in text
    assert "Nested untouched" not in text and "File untouched" not in text


def test_forward_draft_preserves_original_mime_and_new_files(mailbox):
    original = source(mailbox)
    backend, server = mailbox
    result = backend.create_forward_draft("41:7", OutgoingMessage(
        to=["next@example.net"], bcc=["new-hidden@example.net"], body="Forward note",
        attachments=[OutgoingAttachment("extra.bin", b"extra")],
    ))
    stored = parsed(backend, result)
    assert stored["Bcc"] == "new-hidden@example.net"
    assert b"secret@example.net" not in stored.as_bytes()
    assert "Original HTML" in stored.get_body(preferencelist=("html",)).get_content()
    assert "Forward note" in stored.get_body(preferencelist=("plain",)).get_content()
    attachments = {item["name"]: item for item in result["attachments"]}
    assert set(attachments) == {"logo.png", "original.eml", "notes.txt", "extra.bin"}
    nested = backend.get_attachment(result["id"], "drafts", attachment_id=attachments["original.eml"]["attachment_id"])
    assert email.message_from_bytes(nested.content)["Subject"] == "Attached"
    assert server.messages["INBOX"]["7"][0] == original.as_bytes()


def test_update_changes_uid_and_both_bodies_without_changing_attachments(mailbox):
    source(mailbox)
    backend, _ = mailbox
    created = backend.create_forward_draft("41:7", OutgoingMessage(to=["next@example.net"], bcc=["hidden@example.net"]))
    old = parsed(backend, created)
    result = backend.update_draft(created["id"], body="Replacement", body_format="text")
    assert result["id"] != created["id"] and result["original_removed"] is True
    updated = parsed(backend, result)
    assert updated["Subject"] == old["Subject"] and updated["Bcc"] == old["Bcc"]
    assert updated.get_body(preferencelist=("plain",)).get_content().strip() == "Replacement"
    assert "Replacement" in updated.get_body(preferencelist=("html",)).get_content()
    assert "Original HTML" not in updated.get_body(preferencelist=("html",)).get_content()
    assert {item["name"] for item in result["attachments"]} == {"logo.png", "original.eml", "notes.txt"}
    for item in result["attachments"]:
        data = backend.get_attachment(result["id"], "drafts", attachment_id=item["attachment_id"]).content
        assert data
        if item["name"] == "original.eml":
            assert b"Nested untouched" in data
        if item["name"] == "notes.txt":
            assert data == b"File untouched"
    assert "Nested untouched" not in backend.get_message(result["id"], "drafts", "text").body


def test_empty_fields_clear_but_omission_preserves_body(mailbox):
    backend, _ = mailbox
    created = backend.create_draft(OutgoingMessage(to=[], subject="Subject", body="Body"))
    subject_only = backend.update_draft(created["id"], subject="")
    assert parsed(backend, subject_only)["Subject"] == ""
    assert backend.get_message(subject_only["id"], "drafts", "text").body.strip() == "Body"
    cleared = backend.update_draft(subject_only["id"], body="")
    assert backend.get_message(cleared["id"], "drafts", "text").body.strip() == ""


@pytest.mark.parametrize("folder,flags", [("inbox", {"\\Draft"}), ("drafts", set()), ("drafts", {"\\Draft", "\\Deleted"})])
def test_update_refuses_non_draft_without_append(mailbox, folder, flags):
    backend, server = mailbox
    native = "INBOX" if folder == "inbox" else "Drafts"
    server.messages[native]["7"][1] = flags
    before = server.next_uid
    with pytest.raises(MailBackendError):
        backend.update_draft(f'{server.validities[native]}:7', body="no", folder=folder)
    assert server.next_uid == before


def test_create_without_appenduid_finds_only_its_marker(mailbox):
    backend, server = mailbox
    server.appenduid = False
    result = backend.create_draft(OutgoingMessage(to=[], subject="Drafts"))
    assert result["id"] == "40:20"
    assert backend.get_message(result["id"], result["folder"]).subject == "Drafts"


def test_unknown_creation_identity_reports_recovery_without_stale_id(mailbox):
    backend, server = mailbox
    server.appenduid = False
    server.search_matches = b""
    with pytest.raises(MailBackendError) as failure:
        backend.create_draft(OutgoingMessage(to=[], body="keep"))
    assert failure.value.details["draft_id"] is None
    assert failure.value.details["folder"] == "drafts"
    assert failure.value.details["sent"] is False
    assert failure.value.details["revision"]
    assert "20" in server.messages["Drafts"]


def test_update_cleanup_failure_returns_readable_new_identity(mailbox):
    backend, server = mailbox
    server.expunge_status = "NO"
    result = backend.update_draft("40:7", subject="Updated")
    assert result["id"] == "40:20" and result["original_removed"] is False
    assert backend.get_message(result["id"], result["folder"]).subject == "Updated"
    assert result["warnings"]


def test_html_only_body_update_keeps_nested_message_and_adds_plain_alternative(mailbox):
    backend, server = mailbox
    draft = EmailMessage()
    draft["To"] = "reader@example.net"
    draft.set_content("<p>Old HTML</p>", subtype="html")
    nested = EmailMessage()
    nested.set_content("Nested original")
    draft.add_attachment(nested, filename="attached.eml")
    server.messages["Drafts"]["7"] = [draft.as_bytes(), {"\\Draft"}]
    result = backend.update_draft("40:7", body="<p>New HTML</p>", body_format="html")
    stored = parsed(backend, result)
    assert "New HTML" in stored.get_body(preferencelist=("plain",)).get_content()
    assert "<p>New HTML</p>" in stored.get_body(preferencelist=("html",)).get_content()
    attachment = backend.get_attachment(result["id"], "drafts", attachment_name="attached.eml")
    assert b"Nested original" in attachment.content


def test_compose_attached_email_remains_downloadable_message(mailbox):
    backend, _ = mailbox
    nested = EmailMessage()
    nested["Subject"] = "Nested"
    nested.set_content("Do not expose as outer body")
    result = backend.create_draft(OutgoingMessage(
        to=[], body="Outer",
        attachments=[OutgoingAttachment("nested.eml", nested.as_bytes(), "message/rfc822")],
    ))
    assert backend.get_message(result["id"], "drafts", "text").body.strip() == "Outer"
    content = backend.get_attachment(result["id"], "drafts", attachment_id=result["attachments"][0]["attachment_id"]).content
    assert email.message_from_bytes(content)["Subject"] == "Nested"


@pytest.mark.parametrize("cleanup", [False, True])
def test_creation_failure_retains_identity_or_removes_only_new_draft(mailbox, cleanup):
    backend, server = mailbox
    server.replacement_fetch_status = "NO"
    with pytest.raises(MailBackendError) as failure:
        backend.create_draft(OutgoingMessage(to=[], cleanup_on_failure=cleanup))
    details = failure.value.details
    assert details["draft_id"] == "40:20"
    assert details["stage"] == "attachments" and details["sent"] is False
    assert details["cleanup_attempted"] is cleanup
    assert details["draft_removed"] is cleanup
    assert ("20" in server.messages["Drafts"]) is not cleanup
    assert {"7", "8"}.issubset(server.messages["Drafts"])


@pytest.mark.parametrize("inline_target", ["plain", "html", "alternative"])
def test_inline_body_dispositions_remain_readable_and_editable(mailbox, inline_target):
    backend, server = mailbox
    draft = EmailMessage()
    draft["To"] = "reader@example.net"
    draft.set_content("Original plain body")
    draft.add_alternative("<p>Original HTML body</p>", subtype="html")
    target = draft if inline_target == "alternative" else draft.get_body(preferencelist=(inline_target,))
    target["Content-Disposition"] = "inline"
    target["Content-ID"] = "<body-root>"
    draft.add_attachment(b"keep file", maintype="application", subtype="octet-stream", filename="keep.bin")
    draft.add_attachment(b"inline image", maintype="image", subtype="png", disposition="inline", cid="<image>")
    server.messages["Drafts"]["7"] = [draft.as_bytes(), {"\\Draft"}]

    assert backend.get_message("40:7", "drafts", "text").body.strip() == "Original plain body"
    assert "Original HTML body" in backend.get_message("40:7", "drafts", "html").body
    assert {item.name for item in backend.list_attachments("40:7", "drafts")} == {"keep.bin", "part.png"}
    result = backend.update_draft("40:7", body="Replacement body", body_format="text")
    assert backend.get_message(result["id"], "drafts", "text").body.strip() == "Replacement body"
    assert "Replacement body" in backend.get_message(result["id"], "drafts", "html").body
    assert {item["name"] for item in result["attachments"]} == {"keep.bin", "part.png"}
    for item in result["attachments"]:
        content = backend.get_attachment(result["id"], "drafts", attachment_id=item["attachment_id"]).content
        assert content == (b"keep file" if item["name"] == "keep.bin" else b"inline image")
