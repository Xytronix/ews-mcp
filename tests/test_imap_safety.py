"""Stateful protocol regressions for mailbox isolation and targeted mutations."""
from __future__ import annotations

import email
import email.policy
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.mail_backends.accounts import MailAccount
from src.mail_backends.base import MailBackendError, MailFolder, OutgoingMessage
from src.mail_backends.imap_backend import IMAPBackend


def mime(subject="draft", attachments=()):
    message = EmailMessage()
    message["From"] = "author@example.com"
    message["To"] = "reader@example.com"
    message["Subject"] = subject
    message["Message-ID"] = f"<{subject}@example.com>"
    message.set_content("Stored content")
    for name in attachments:
        message.add_attachment(name.encode(), maintype="application", subtype="octet-stream", filename=name)
    return message.as_bytes()


class Server:
    """Only the IMAP network boundary is replaced; UIDs are folder-local."""

    capabilities = (b"IMAP4rev1", b"UIDPLUS")

    def __init__(self):
        self.messages = {
            name: {"7": [mime(name, ("first.bin", "second.bin")), {"\\Draft"}],
                   "8": [mime("unrelated"), {"\\Deleted"}]}
            for name in ("Drafts", "INBOX", "Trash", "Sent")
        }
        self.validities = {name: str(index + 40) for index, name in enumerate(self.messages)}
        self.untagged_responses = {}
        self.selected = None
        self.appenduid = True
        self.search_matches = None
        self.expunge_status = "OK"
        self.append_status = "OK"
        self.store_status = "OK"
        self.replacement_fetch_status = "OK"
        self.copyuid = True
        self.next_uid = 20

    def noop(self):
        return "OK", []

    def select(self, folder, readonly=True):
        self.selected = folder.strip('"')
        self.untagged_responses["UIDVALIDITY"] = [self.validities[self.selected].encode()]
        return "OK", [str(len(self.messages[self.selected])).encode()]

    def response(self, code):
        return code, self.untagged_responses.pop(code, [None])

    def list(self):
        return "OK", [f'(\\{name}) "/" "{name}"'.encode() for name in self.messages]

    def append(self, folder, flags, date, payload):
        if self.append_status != "OK":
            return self.append_status, [b"append failed"]
        folder = folder.strip('"')
        uid = str(self.next_uid)
        self.next_uid += 1
        self.messages[folder][uid] = [payload, {"\\Draft", "\\Seen"}]
        if self.appenduid:
            # imaplib normally stores APPENDUID separately from tagged text.
            self.untagged_responses["APPENDUID"] = [f'{self.validities[folder]} {uid}'.encode()]
        return "OK", [b"APPEND completed"]

    def uid(self, command, *args):
        if command == "FETCH":
            if int(args[0].split(",")[0]) >= 20 and self.replacement_fetch_status != "OK":
                return self.replacement_fetch_status, [b"FETCH refused"]
            rows = []
            for uid in args[0].split(","):
                if uid in self.messages[self.selected]:
                    raw, flags = self.messages[self.selected][uid]
                    rows.append((f'1 (UID {uid} FLAGS ({" ".join(flags)}) RFC822.SIZE {len(raw)} BODY[] {{{len(raw)}}}'.encode(), raw))
            return "OK", rows
        if command == "SEARCH":
            if self.search_matches is not None:
                return "OK", [self.search_matches]
            if "HEADER" in args:
                index = args.index("HEADER")
                header, expected = args[index + 1:index + 3]
                uids = [uid for uid, (raw, _) in self.messages[self.selected].items()
                        if str(email.message_from_bytes(raw).get(header, "")) == expected.strip('"')]
            else:
                uids = list(self.messages[self.selected])
            return "OK", [" ".join(uids).encode()]
        if command == "STORE":
            if self.store_status != "OK":
                return self.store_status, [b"STORE refused"]
            uid, operation, flags = args
            values = set(flags.strip("()").split())
            if operation.startswith("+"):
                self.messages[self.selected][uid][1].update(values)
            else:
                self.messages[self.selected][uid][1].difference_update(values)
            return "OK", []
        if command == "EXPUNGE":
            if self.expunge_status != "OK":
                return self.expunge_status, [b"unsupported"]
            uid = args[0]
            if "\\Deleted" in self.messages[self.selected][uid][1]:
                del self.messages[self.selected][uid]
            return "OK", []
        if command in ("COPY", "MOVE"):
            uid, target = args
            target = target.strip('"')
            new_uid = str(self.next_uid)
            self.next_uid += 1
            raw, flags = self.messages[self.selected][uid]
            self.messages[target][new_uid] = [raw, set(flags)]
            if command == "MOVE":
                del self.messages[self.selected][uid]
            if self.copyuid:
                self.untagged_responses["COPYUID"] = [f'{self.validities[target]} {uid} {new_uid}'.encode()]
            return "OK", [b"completed"]
        raise AssertionError(command)

    def expunge(self):
        # Model the actual destructive behavior instead of asserting call shape.
        self.messages[self.selected] = {
            uid: item for uid, item in self.messages[self.selected].items()
            if "\\Deleted" not in item[1]
        }
        return "OK", []


@pytest.fixture
def mailbox():
    backend = IMAPBackend(MailAccount(email="author@example.com", provider="imap", password="unused"))
    server = Server()
    backend._imap = server
    backend._folders = [MailFolder(id=name, name=name, path=name, well_known=alias)
                        for name, alias in (("Drafts", "drafts"), ("INBOX", "inbox"), ("Trash", "trash"), ("Sent", "sent"))]
    return backend, server


@pytest.mark.parametrize("operation", ["rewrite", "move", "delete", "trash"])
def test_mutation_preserves_unrelated_deleted_messages(mailbox, operation):
    backend, server = mailbox
    if operation == "rewrite":
        result = backend.add_attachment("40:7", "new.bin", b"new")
        folder = "Drafts"
        assert result["id"] == "40:20"
    elif operation == "move":
        result = backend.move_message("41:7", "trash")
        folder = "INBOX"
        assert result["id"] == "42:20"
        assert result["previous_id"] == "41:7"
        assert result["folder"] == "trash"
    elif operation == "trash":
        backend.delete_message("42:7", "trash")
        folder = "Trash"
    else:
        backend.delete_message("41:7", hard_delete=True)
        folder = "INBOX"
    assert "8" in server.messages[folder]
    assert "7" not in server.messages[folder]


def test_uid_expunge_failure_never_falls_back_to_global(mailbox):
    backend, server = mailbox
    server.expunge_status = "BAD"
    with pytest.raises(MailBackendError):
        backend.delete_message("41:7", hard_delete=True)
    assert set(server.messages["INBOX"]) == {"7", "8"}


@pytest.mark.parametrize("cleanup_failure", ["store", "expunge"])
@pytest.mark.parametrize("mutation", ["add", "delete"])
def test_replacement_identity_survives_original_cleanup_failure(mailbox, cleanup_failure, mutation):
    backend, server = mailbox
    if cleanup_failure == "store":
        server.store_status = "NO"
    else:
        server.expunge_status = "BAD"
    if mutation == "add":
        result = backend.add_attachment("40:7", "new.bin", b"new")
        expected_names = ["first.bin", "second.bin", "new.bin"]
    else:
        result = backend.delete_attachment("40:7", attachment_name="first.bin")
        expected_names = ["second.bin"]
    assert result["id"] == "40:20"
    assert result["previous_id"] == "40:7"
    assert result["original_removed"] is False
    assert result["warnings"]
    assert [item["name"] for item in result["attachments"]] == expected_names
    for item in result["attachments"]:
        assert backend.get_attachment(
            result["id"], result["folder"], attachment_id=item["attachment_id"],
        ).name == item["name"]
    assert set(server.messages["Drafts"]) == {"7", "8", "20"}
    assert ("\\Deleted" in server.messages["Drafts"]["7"][1]) == (cleanup_failure == "expunge")


def test_replacement_relist_failure_preserves_identity_without_claiming_verification(mailbox):
    backend, server = mailbox
    server.replacement_fetch_status = "NO"
    result = backend.add_attachment("40:7", "new.bin", b"new")
    assert result["id"] == "40:20"
    assert result["original_removed"] is False
    assert result["attachments_verified"] is False
    assert result["warnings"]
    assert "\\Deleted" not in server.messages["Drafts"]["7"][1]
    server.replacement_fetch_status = "OK"
    assert backend.get_attachment(
        result["id"], "drafts", attachment_id=result["attachment_id"],
    ).content == b"new"


def test_sending_rewritten_draft_keeps_internal_revision_off_smtp(mailbox, monkeypatch):
    backend, _ = mailbox
    result = backend.add_attachment("40:7", "new.bin", b"new")
    submissions = []

    def smtp(outgoing, envelope):
        submissions.append(outgoing)
        return {}

    monkeypatch.setattr(backend, "_smtp_send", smtp)
    backend.send_draft(result["id"])
    wire = email.message_from_bytes(submissions[0])
    assert wire.get("X-EWS-MCP-Draft-Revision") is None
    assert [part.get_filename() for part in wire.walk() if part.get_filename()] == [
        "first.bin", "second.bin", "new.bin",
    ]



@pytest.mark.parametrize("native_move", [False, True])
def test_move_without_destination_identity_never_returns_source_id(mailbox, native_move):
    backend, server = mailbox
    server.copyuid = False
    if native_move:
        server.capabilities += (b"MOVE",)
        result = backend.move_message("41:7", "trash")
        assert result["id"] is None
        assert result["previous_id"] == "41:7"
        assert result["folder"] == "trash"
        assert "7" not in server.messages["INBOX"]
    else:
        with pytest.raises(MailBackendError):
            backend.move_message("41:7", "trash")
        assert "\\Deleted" not in server.messages["INBOX"]["7"][1]
    assert "20" in server.messages["Trash"]
    assert "8" in server.messages["INBOX"]


def test_soft_delete_returns_the_trash_identity(mailbox):
    backend, server = mailbox
    result = backend.delete_message("41:7")
    assert result["id"] == "42:20"
    assert result["previous_id"] == "41:7"
    assert result["folder"] == "trash"
    assert backend.get_message(result["id"], result["folder"]).subject == "INBOX"
    assert "8" in server.messages["INBOX"]

def test_append_failure_keeps_original_undeleted(mailbox):
    backend, server = mailbox
    server.append_status = "NO"
    with pytest.raises(MailBackendError):
        backend.add_attachment("40:7", "new.bin", b"new")
    assert set(server.messages["Drafts"]) == {"7", "8"}
    assert "\\Deleted" not in server.messages["Drafts"]["7"][1]


def test_missing_appenduid_resolves_only_the_new_revision(mailbox):
    backend, server = mailbox
    server.appenduid = False
    result = backend.add_attachment("40:7", "new.bin", b"new")
    assert result["id"] == "40:20"
    assert "7" not in server.messages["Drafts"]
    assert backend.get_attachment(result["id"], "drafts", attachment_name="new.bin").content == b"new"


@pytest.mark.parametrize("matches", [b"", b"7", b"20 21"])
def test_unresolved_append_identity_preserves_original(mailbox, matches):
    backend, server = mailbox
    server.appenduid = False
    server.search_matches = matches
    original = server.messages["Drafts"]["7"][0]
    with pytest.raises(MailBackendError):
        backend.add_attachment("40:7", "new.bin", b"new")
    assert server.messages["Drafts"]["7"][0] == original
    assert "\\Deleted" not in server.messages["Drafts"]["7"][1]
    assert "8" in server.messages["Drafts"]


def test_attachment_mutation_returns_usable_relisted_part_ids(mailbox):
    backend, _ = mailbox
    before = backend.list_attachments("40:7", "drafts")
    result = backend.delete_attachment("40:7", attachment_name="first.bin")
    assert result["previous_id"] == "40:7"
    assert result["folder"] == "drafts"
    assert [item["name"] for item in result["attachments"]] == ["second.bin"]
    remaining = result["attachments"][0]
    assert remaining["attachment_id"] != before[1].attachment_id
    assert backend.get_attachment(result["id"], "drafts", attachment_id=remaining["attachment_id"]).content == b"second.bin"
    added = backend.add_attachment(result["id"], "third.bin", b"third")
    assert [item["name"] for item in added["attachments"]] == ["second.bin", "third.bin"]
    assert backend.get_attachment(added["id"], "drafts", attachment_id=added["attachment_id"]).content == b"third"


@pytest.mark.parametrize("first_operation", ["read", "write"])
def test_whole_operation_blocks_cross_folder_mutation(mailbox, monkeypatch, first_operation):
    backend, server = mailbox
    selected = threading.Event()
    release = threading.Event()
    attempted = threading.Event()
    select = backend._select

    def pause_after_select(native, readonly=True):
        result = select(native, readonly)
        if native == "Drafts":
            selected.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(backend, "_select", pause_after_select)

    def mark_other():
        attempted.set()
        return backend.mark_message("41:7", "inbox", is_read=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        if first_operation == "read":
            first = pool.submit(backend.get_message, "40:7", "drafts")
        else:
            first = pool.submit(backend.mark_message, "40:7", "drafts", is_flagged=True)
        assert selected.wait(5)
        write = pool.submit(mark_other)
        assert attempted.wait(5)
        # Pause BETWEEN SELECT and FETCH/STORE: locking individual protocol
        # commands is insufficient even if the connection itself is serialized.
        acquired = backend._lock.acquire(blocking=False)
        if acquired:
            backend._lock.release()
        release.set()
        result = first.result(timeout=5)
        if first_operation == "read":
            assert result.subject == "Drafts"
        write.result(timeout=5)
    assert acquired is False
    assert "\\Seen" not in server.messages["Drafts"]["7"][1]
    assert "\\Seen" in server.messages["INBOX"]["7"][1]
    assert ("\\Flagged" in server.messages["Drafts"]["7"][1]) == (first_operation == "write")
    assert "\\Flagged" not in server.messages["INBOX"]["7"][1]


@pytest.mark.parametrize("reply", [False, True])
def test_compose_and_reply_report_partial_smtp_acceptance(mailbox, monkeypatch, reply):
    backend, _ = mailbox
    submissions = []

    def smtp(outgoing, envelope):
        submissions.append(list(envelope))
        return {"refused@example.com": (550, b"recipient rejected")}

    monkeypatch.setattr(backend, "_smtp_send", smtp)
    monkeypatch.setattr(backend, "_append_to_sent", lambda outgoing: "Sent")
    result = backend.send_message(OutgoingMessage(
        to=["accepted@example.com", "refused@example.com"], body="hello",
        reply_to_message_id="41:7" if reply else None,
    ))
    assert result["sent"] is False
    assert result["partial_failure"] is True
    assert result["accepted_by_mail_server"] is True
    assert result["delivery_confirmed"] is False
    assert result["accepted_recipients"] == ["accepted@example.com"]
    assert result["refused_recipients"] == {"refused@example.com": {"code": 550, "message": "recipient rejected"}}
    assert len(submissions) == 1
    assert result["warnings"]


@pytest.mark.parametrize("failure", [None, MailBackendError("Sent unavailable")])
def test_compose_sent_copy_failure_does_not_invite_resubmission(mailbox, monkeypatch, failure):
    backend, _ = mailbox
    monkeypatch.setattr(backend, "_smtp_send", lambda outgoing, envelope: {})

    def append(outgoing):
        if failure:
            raise failure
        return None

    monkeypatch.setattr(backend, "_append_to_sent", append)
    result = backend.send_message(OutgoingMessage(to=["reader@example.com"], body="hello"))
    assert result["sent"] is True
    assert result["accepted_by_mail_server"] is True
    assert result["accepted_recipients"] == ["reader@example.com"]
    assert result["saved_to_sent"] is None
    assert result["warnings"]
