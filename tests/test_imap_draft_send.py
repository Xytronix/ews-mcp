"""Stored draft submission, with only IMAP/SMTP network boundaries faked."""
from __future__ import annotations

import asyncio
import email
import email.policy
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.mail_backends.accounts import MailAccount
from src.mail_backends.base import MailBackendError, MailFolder
from src.mail_backends.imap_backend import IMAPBackend
from src.tools.mailbox_tools import SendEmailTool


@pytest.fixture
def draft_server(monkeypatch):
    draft = EmailMessage(policy=email.policy.SMTP)
    draft["From"] = "Author <author@example.com>"
    draft["To"] = "Reader <reader@example.com>"
    draft["Cc"] = "copy@example.com"
    draft["Bcc"] = "hidden@example.com, hidden2@example.com"
    draft["Resent-Bcc"] = "older-hidden@example.com"
    draft["Subject"] = "Stored subject"
    draft["Message-ID"] = "<stored-id@example.com>"
    draft["In-Reply-To"] = "<parent@example.com>"
    draft["References"] = "<root@example.com> <parent@example.com>"
    draft.set_content("Exact stored plain text.")
    draft.add_alternative("<p>Exact stored <b>HTML</b>.</p>", subtype="html")
    draft.add_attachment(b"\x00\xffattachment\r\n", maintype="application", subtype="octet-stream", filename="file.bin")
    # A folded blind-copy field exercises removal of continuation lines too.
    raw = draft.as_bytes().replace(b"Bcc: hidden@example.com, hidden2@example.com", b"Bcc: hidden@example.com,\r\n hidden2@example.com")
    state = SimpleNamespace(raw=raw, flags=["\\Draft"], validity="42", events=[], envelopes=[], transmitted=[], appended=[], refused={}, smtp_error=None, append_status="OK", store_error=None, capabilities=(b"IMAP4rev1", b"UIDPLUS"))
    state.uid_expunge_status = "OK"
    backend = IMAPBackend(MailAccount(email="author@example.com", provider="imap", password="unused", settings={"smtp_host": "smtp.example.com", "smtp_starttls": False}))
    backend._folders = [MailFolder(id=name, name=name, path=name, well_known=alias) for name, alias in [("Drafts", "drafts"), ("Sent", "sent"), ("INBOX", "inbox")]]

    def select(native, readonly=True):
        state.events.append(("select", native, readonly))
        return 1, state.validity

    def fetch(uids, spec):
        state.events.append(("fetch", tuple(uids)))
        return {7: {"raw": state.raw, "flags": state.flags}}

    def store(uid, operation, flags):
        state.events.append(("store", uid, flags))
        if state.store_error:
            raise state.store_error

    def uid(command, *args):
        state.events.append((command, *args))
        if command == "EXPUNGE":
            return state.uid_expunge_status, [b"UID EXPUNGE response"]
        return "OK", []

    def append(folder, flags, date, payload):
        state.events.append(("append", folder))
        state.appended.append(payload)
        return state.append_status, [b"append response"]

    def expunge():
        state.events.append(("EXPUNGE_ALL",))
        return "OK", []

    class SMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            pass

        def login(self, *args):
            pass

        def sendmail(self, sender, recipients, payload, **kwargs):
            state.events.append(("smtp",))
            if state.smtp_error:
                raise state.smtp_error
            state.envelopes.append(list(recipients))
            state.transmitted.append(payload)
            return state.refused

        def send_message(self, message, to_addrs=None):
            return self.sendmail("author@example.com", to_addrs, message.as_bytes(policy=email.policy.SMTP))

    monkeypatch.setattr(backend, "_select", select)
    monkeypatch.setattr(backend, "_fetch", fetch)
    monkeypatch.setattr(backend, "_store", store)
    monkeypatch.setattr(backend, "_connect", lambda: SimpleNamespace(capabilities=state.capabilities, append=append, uid=uid, expunge=expunge))
    monkeypatch.setattr(smtplib, "SMTP", SMTP)
    return backend, state


def test_send_stored_mime_preserves_body_attachments_and_thread(draft_server):
    backend, state = draft_server
    # imaplib may cache pre-login capabilities without advertising UIDPLUS.
    state.capabilities = (b"IMAP4rev1",)
    client = SimpleNamespace(mailbox_registry=SimpleNamespace(
        find_account={backend.email: backend.account}.get,
        get_backend={backend.email: backend}.__getitem__,
    ))
    result = asyncio.run(SendEmailTool(client).execute(
        draft_id="42:7", target_mailbox=backend.email,
    ))
    sent = email.message_from_bytes(state.transmitted[0], policy=email.policy.default)
    original = email.message_from_bytes(state.raw, policy=email.policy.default)
    assert state.transmitted[0].split(b"\r\n\r\n", 1)[1] == state.raw.split(b"\r\n\r\n", 1)[1]
    assert sent["Message-ID"] == original["Message-ID"]
    assert sent["References"] == original["References"]
    assert sent["In-Reply-To"] == original["In-Reply-To"]
    assert list(sent.iter_attachments())[0].get_payload(decode=True) == b"\x00\xffattachment\r\n"
    assert result["sent"] is True and result["accepted_by_mail_server"] is True
    assert result["delivery_confirmed"] is False
    assert result["draft_id"] == "42:7" and result["backend"] == "imap"
    assert result["mailbox"] == backend.email
    assert result.get("id") != "42:7"
    assert state.events.index(("smtp",)) < next(i for i, event in enumerate(state.events) if event[0] == "store")
    assert ("EXPUNGE", "7") in state.events
    assert ("EXPUNGE_ALL",) not in state.events
    assert result["draft_removed"] is True


def test_lf_serialized_draft_is_submitted_with_smtp_line_endings(draft_server):
    backend, state = draft_server
    # Draft attachment rewrites serialize with email.policy.default (LF).
    state.raw = state.raw.replace(b"\r\n", b"\n").replace(b"\nSubject:", b"\rSubject:")
    backend.send_draft("42:7")
    payload = state.transmitted[0]
    assert b"\r\n\r\n" in payload
    assert b"\n" not in payload.replace(b"\r\n", b"")
    assert b"\r" not in payload.replace(b"\r\n", b"")
    sent = email.message_from_bytes(payload, policy=email.policy.default)
    assert sent["Subject"] == "Stored subject"
    assert list(sent.iter_attachments())[0].get_payload(decode=True) == b"\x00\xffattachment\r\n"
    assert sent.get("Bcc") is None and sent.get("Resent-Bcc") is None


def test_blind_recipients_only_appear_in_envelope(draft_server):
    backend, state = draft_server
    backend.send_draft("42:7")
    assert set(state.envelopes[0]) == {"reader@example.com", "copy@example.com", "hidden@example.com", "hidden2@example.com"}
    headers = state.transmitted[0].split(b"\r\n\r\n", 1)[0].lower()
    assert b"bcc:" not in headers
    assert b"hidden@example.com" not in headers
    assert b"hidden2@example.com" not in headers
    assert b"older-hidden@example.com" not in headers


def test_dry_run_reads_stored_content_without_mutations(draft_server):
    backend, state = draft_server
    result = backend.send_draft("42:7", dry_run=True)
    assert result["dry_run"] is True and result["sent"] is False
    assert result["would_send_to"] == ["reader@example.com"]
    assert result["would_cc"] == ["copy@example.com"]
    assert result["would_bcc"] == ["hidden@example.com", "hidden2@example.com"]
    assert result["subject"] == "Stored subject"
    assert "Exact stored plain text" in result["body_preview"]
    assert result["attachments_count"] == 1
    assert [event[0] for event in state.events] == ["select", "fetch"]
    assert state.events[0][2] is True


@pytest.mark.parametrize("invalid", ["stale", "folder", "flag", "deleted"])
def test_non_drafts_are_rejected_before_smtp(draft_server, invalid):
    backend, state = draft_server
    if invalid == "stale":
        state.validity = "43"
    elif invalid == "flag":
        state.flags = ["\\Seen"]
    elif invalid == "deleted":
        state.flags.append("\\Deleted")
    with pytest.raises(MailBackendError):
        backend.send_draft("42:7", folder="inbox" if invalid == "folder" else "drafts")
    assert not state.transmitted and not state.appended
    assert not any(event[0] in ("store", "EXPUNGE", "EXPUNGE_ALL") for event in state.events)


def test_smtp_failure_keeps_original_draft(draft_server):
    backend, state = draft_server
    state.smtp_error = smtplib.SMTPRecipientsRefused({"reader@example.com": (550, b"refused")})
    with pytest.raises(MailBackendError):
        backend.send_draft("42:7")
    assert not state.appended
    assert not any(event[0] == "store" for event in state.events)


@pytest.mark.parametrize("failure", ["append", "cleanup"])
def test_post_acceptance_failure_returns_warning_not_retryable_error(draft_server, failure):
    backend, state = draft_server
    if failure == "append":
        state.append_status = "NO"
    else:
        state.store_error = MailBackendError("cleanup denied")
    result = backend.send_draft("42:7")
    assert result["sent"] is True and result["accepted_by_mail_server"] is True
    assert result["delivery_confirmed"] is False
    assert result["warnings"]
    if failure == "append":
        assert result["saved_to_sent"] is None
    else:
        assert result["draft_removed"] is False


def test_partial_smtp_acceptance_is_explicit_and_does_not_invite_full_retry(draft_server):
    backend, state = draft_server
    state.refused = {"copy@example.com": (550, b"recipient rejected")}
    result = backend.send_draft("42:7")
    assert result["sent"] is False and result["accepted_by_mail_server"] is True
    assert result["partial_failure"] is True
    assert result["refused_recipients"]["copy@example.com"]["code"] == 550
    assert "copy@example.com" not in result["accepted_recipients"]
    assert "reader@example.com" in result["accepted_recipients"]
    assert result["warnings"]


def test_without_uid_expunge_other_deleted_messages_are_not_removed(draft_server):
    backend, state = draft_server
    state.uid_expunge_status = "BAD"
    result = backend.send_draft("42:7")
    assert result["accepted_by_mail_server"] is True
    assert ("EXPUNGE_ALL",) not in state.events
    assert ("EXPUNGE", "7") in state.events
    assert result["draft_removed"] is False
    assert result["warnings"]
