"""SmarterMail scheduling retains saved content and verifies the entire queue."""
from __future__ import annotations

import base64
import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest

from exchangelib import FileAttachment, HTMLBody, Mailbox, Message
from exchangelib.folders import Drafts, Root
from exchangelib.services import CreateAttachment

from tests.support import offline_account
from src.tools import smartermail_mail_tools as schedule_tools
from src.exceptions import ToolExecutionError, ValidationError
from src.smartermail import SCHEDULED_FOLDER
from src.tools.smartermail_mail_tools import CancelScheduledEmailTool, ScheduleEmailTool


class QueueClient:
    email = "sender@example.com"

    def __init__(self, *, remove=True):
        self.messages = [{"uid": n} for n in range(1, 76)]
        self.remove = remove
        self.visited = []
        self.deleted = []
        self.sent = []
        self.expired = False

    def request(self, method, path, *, json):
        if path.endswith("/message-undo"):
            # Live server also acknowledges stale UIDs without doing anything.
            return {"success": True}
        assert json["folder"] == SCHEDULED_FOLDER
        if path.endswith("/messages"):
            start, count = json["skip"], json["take"]
            self.visited.append(start)
            return {"results": self.messages[start:start + count], "totalCount": len(self.messages)}
        assert path.endswith("/delete-messages")
        if self.expired:
            self.sent.extend(json["UID"])
            self.messages = [m for m in self.messages if m["uid"] not in json["UID"]]
        self.deleted.extend(json["UID"])
        if self.remove:
            self.messages = [m for m in self.messages if m["uid"] not in json["UID"]]
        return {"success": True, "newUIDs": []}


def tool_for(cls, rest):
    client = SimpleNamespace(
        config=SimpleNamespace(ews_email=rest.email),
        smartermail=SimpleNamespace(enabled=True, get=lambda target: rest),
        mailbox_registry=SimpleNamespace(find_account=lambda target: SimpleNamespace(kind="ews")),
    )
    return cls(client)


def test_cancel_does_not_claim_removal_for_item_after_first_page():
    rest = QueueClient(remove=False)
    result = asyncio.run(tool_for(CancelScheduledEmailTool, rest).safe_execute(uid=73))
    assert result["success"] is False
    assert result["cancelled"] is None
    assert result["cancellation_confirmed"] is False
    assert result["removed_from_queue"] is False
    assert result["remaining_in_scheduled_folder"] is True
    assert 50 in rest.visited


def test_cancel_verifies_removal_after_first_page():
    rest = QueueClient()
    result = asyncio.run(tool_for(CancelScheduledEmailTool, rest).execute(uid=73))
    assert result["success"] is True
    assert result["removed_from_queue"] is True
    assert result["cancellation_confirmed"] is False
    assert result["cancelled"] is None
    assert all(m["uid"] != 73 for m in rest.messages)


def test_cancel_unknown_uid_is_not_reported_as_cancelled():
    rest = QueueClient()
    result = asyncio.run(tool_for(CancelScheduledEmailTool, rest).safe_execute(uid=999))
    assert result["cancelled"] is None
    assert result["cancel_requested"] is False
    assert result["cancellation_confirmed"] is False
    assert rest.deleted == []


def test_cancel_postdelete_verification_failure_preserves_mutation_state():
    rest = QueueClient()
    original = rest.request
    def request(method, path, *, json):
        if path.endswith("/messages") and rest.deleted:
            return {"success": True}
        return original(method, path, json=json)
    rest.request = request
    result = asyncio.run(tool_for(CancelScheduledEmailTool, rest).safe_execute(uid=73))
    assert result["success"] is False
    assert result["cancelled"] is None
    assert result["cancellation_confirmed"] is False
    assert result["delete_submitted"] is True
    assert result["uid"] == 73
    assert result["cleanup_confirmed"] is False


def test_cancel_race_with_sending_is_not_reported_as_cancelled():
    rest = QueueClient()
    rest.expired = True
    result = asyncio.run(tool_for(CancelScheduledEmailTool, rest).safe_execute(uid=73))
    assert result["success"] is True
    assert result["removed_from_queue"] is True
    assert result["cancelled"] is None
    assert result["cancellation_confirmed"] is False
    assert result["delivery_prevented"] is None
    assert rest.sent == [73]


class ScheduleClient(QueueClient):
    def __init__(self, accepted=True):
        super().__init__()
        self.messages = []
        self.accepted = accepted

    def request(self, method, path, *, json):
        if path.endswith("/message-put"):
            if self.accepted:
                self.messages.append({"uid": 81, "subject": json["subject"]})
            return {"success": True, "uid": 81, "isScheduledMessage": self.accepted}
        return super().request(method, path, json=json)


def test_compose_schedule_returns_uid_that_is_visible_and_cancellable():
    rest = ScheduleClient()
    result = asyncio.run(tool_for(ScheduleEmailTool, rest).execute(
        to=["recipient@example.com"], subject="Future", body="Message",
        send_at="2099-01-01T12:00:00Z",
    ))
    assert result["is_scheduled"] is True
    assert result["uid"] == rest.messages[0]["uid"]
    cancelled = asyncio.run(tool_for(CancelScheduledEmailTool, rest).execute(uid=result["uid"]))
    assert cancelled["removed_from_queue"] is True
    assert rest.messages == []


def test_schedule_does_not_claim_success_when_server_did_not_schedule():
    rest = ScheduleClient(accepted=False)
    result = asyncio.run(tool_for(ScheduleEmailTool, rest).safe_execute(
        to=["recipient@example.com"], subject="Future", body="Message",
        send_at="2099-01-01T12:00:00Z",
    ))
    assert result["success"] is False
    assert result["submission_uncertain"] is True
    assert result["uid"] == 81


def ews_id(mid):
    mailbox = UUID("11111111-1111-1111-1111-111111111111")
    raw = (b"\x00\x03\x24\x00" + str(mailbox).encode() + bytes.fromhex("002c0002000100")
           + mailbox.bytes_le + (73).to_bytes(8, "little"))
    return base64.b64encode(raw + mid.to_bytes(8, "little") + bytes(8)).decode()


@pytest.fixture
def draft_state(monkeypatch):
    rest = ScheduleClient()
    account = offline_account(rest.email)
    account.__dict__["drafts"] = Drafts(root=Root(account=account))
    draft = Message(
        account=account, is_draft=True, subject="Saved subject",
        body=HTMLBody('<p>Saved body<img src="cid:stored-image"></p>'),
        to_recipients=[Mailbox(email_address="recipient@example.com")],
        cc_recipients=[Mailbox(email_address="cc@example.com")],
        bcc_recipients=[Mailbox(email_address="bcc@example.com")],
        attachments=[FileAttachment(name="stored.txt", content=b"stored")],
        message_id="stable@example.com",
    )
    draft.id, draft.changekey = ews_id(220032), "CK"
    state = {"exists": True, "files": [("stored.txt", b"stored")], "submitted": None, "fail": None}
    monkeypatch.setattr(schedule_tools, "find_message_for_account", lambda account_, id_: draft)

    def upload(service, parent_item, items):
        from exchangelib.attachments import AttachmentId
        if state["fail"] == "upload":
            raise RuntimeError("attachment refused")
        attachment, = items
        state["files"].append((attachment.name, attachment.content))
        return FileAttachment(attachment_id=AttachmentId(
            id=f"ATT-{len(state['files'])}", root_id=draft.id, root_changekey="CK2"))
    monkeypatch.setattr(CreateAttachment, "get", upload)
    original = rest.request

    def request(method, path, *, json):
        if path.endswith("/mid-to-uid"):
            return {"uid": 75}
        if path.endswith("/message"):
            return {"messageData": {
                "mid": 220032, "uid": 75, "isDraft": True, "folder": "drafts",
                "messageID": draft.message_id, "subject": draft.subject,
                "to": "recipient@example.com", "cc": "cc@example.com", "bcc": "bcc@example.com",
                "messageHTML": str(draft.body), "originalCidLinks": {"stored-image": "cid:stored-image"},
                "attachments": [{"filename": name, "partID": index}
                                for index, (name, _) in enumerate(state["files"], 1)],
            }}
        if path.endswith("/message-put"):
            if state["fail"] == "schedule":
                raise ToolExecutionError("schedule refused")
            if json.get("draftUid") != 75:
                raise ToolExecutionError("No existing draft attachment context")
            excluded = {str(value) for value in json.get("excludeFiles", [])}
            remaining_files = [item for index, item in enumerate(state["files"], 1)
                               if str(index) not in excluded]
            cid_links = dict(json.get("originalCidLinks") or {})
            for cid in json.get("inlineToRemove", []):
                cid_links.pop(cid, None)
            state["submitted"] = {**json, "files": remaining_files, "originalCidLinks": cid_links}
            state["exists"] = False  # Server consumes only after accepting native submission.
        return original(method, path, json=json)
    rest.request = request
    tool = tool_for(ScheduleEmailTool, rest)
    tool.ews_client.get_account = lambda target: account
    return tool, draft, rest, state


def test_schedule_saved_draft_preserves_recipients_body_and_existing_files(draft_state):
    tool, draft, rest, state = draft_state
    result = asyncio.run(tool.execute(draft_id=draft.id, send_at="2099-01-01T12:00:00Z"))
    submitted = state["submitted"]
    assert submitted["to"] == "recipient@example.com"
    assert submitted["cc"] == "cc@example.com"
    assert submitted["bcc"] == "bcc@example.com"
    assert submitted["messageHTML"] == str(draft.body)
    assert submitted["subject"] == draft.subject
    assert submitted["files"] == [("stored.txt", b"stored")]
    assert submitted["originalCidLinks"] == {"stored-image": "cid:stored-image"}
    assert result["uid"] == rest.messages[0]["uid"]
    assert not state["exists"]


def test_schedule_saved_draft_appends_canonical_file(draft_state):
    tool, draft, _, state = draft_state
    asyncio.run(tool.execute(
        draft_id=draft.id, send_at="2099-01-01T12:00:00Z",
        files=[{"file_name": "new.txt", "file_content": "bmV3"}],
    ))
    assert state["submitted"]["files"] == [("stored.txt", b"stored"), ("new.txt", b"new")]


@pytest.mark.parametrize("failure", ["upload", "schedule"])
def test_schedule_failure_preserves_draft_and_reports_recoverable_id(draft_state, failure):
    tool, draft, _, state = draft_state
    state["fail"] = failure
    result = asyncio.run(tool.safe_execute(
        draft_id=draft.id, send_at="2099-01-01T12:00:00Z",
        files=[{"file_name": "new.txt", "file_content": "bmV3"}],
    ))
    assert result["success"] is False
    assert result["draft_id"] == draft.id
    assert result["submission_uncertain"] is (failure == "schedule")
    assert result["uid"] is None
    assert state["exists"]
    assert state["submitted"] is None


def test_saved_schedule_rejects_even_empty_compose_override(draft_state):
    tool, draft, _, state = draft_state
    with pytest.raises(ValidationError, match="omit"):
        asyncio.run(tool.execute(draft_id=draft.id, body="", send_at="2099-01-01T12:00:00Z"))
    assert state["exists"]
    assert state["submitted"] is None


def test_draft_identity_mismatch_does_not_mutate_wrong_message(draft_state):
    tool, draft, rest, state = draft_state
    original = rest.request
    def request(method, path, *, json):
        data = original(method, path, json=json)
        if path.endswith("/message"):
            data["messageData"]["messageID"] = "different@example.com"
        return data
    rest.request = request
    with pytest.raises(ToolExecutionError, match="identity"):
        asyncio.run(tool.execute(
            draft_id=draft.id, send_at="2099-01-01T12:00:00Z",
            files=[{"file_name": "new.txt", "file_content": "bmV3"}],
        ))
    assert state["files"] == [("stored.txt", b"stored")]
    assert state["exists"]


def test_compose_files_are_persisted_before_native_scheduling(draft_state, monkeypatch):
    from exchangelib.items.base import BulkCreateResult
    from exchangelib.services import CreateItem
    tool, saved, _, state = draft_state
    state["files"] = []

    def create(service, items, **kwargs):
        message, = items
        # SmarterMail ignores SaveOnly inline attachments; only explicit
        # CreateAttachment below can populate the simulated server's files.
        assert message.attachments == []
        saved.subject, saved.body = message.subject, message.body
        saved.author = message.author
        return BulkCreateResult(id=saved.id, changekey="CK", attachments=[])
    monkeypatch.setattr(CreateItem, "get", create)
    result = asyncio.run(tool.execute(
        to=["recipient@example.com"], subject="New subject", body="<p>New body</p>",
        files=[{"file_name": "new.txt", "file_content": "bmV3"}],
        send_at="2099-01-01T12:00:00Z",
    ))
    assert state["submitted"]["files"] == [("new.txt", b"new")]
    assert state["submitted"]["subject"] == "New subject"
    assert state["submitted"]["messageHTML"] == "<p>New body</p>"
    assert state["submitted"]["from"] == saved.account.primary_smtp_address
    assert result["is_scheduled"] is True


def test_bad_file_source_leaves_saved_draft_unchanged(draft_state):
    tool, draft, _, state = draft_state
    with pytest.raises(ValidationError):
        asyncio.run(tool.execute(
            draft_id=draft.id, send_at="2099-01-01T12:00:00Z",
            files=[{"file_name": "broken.txt", "file_content": "!invalid!"}],
        ))
    assert state["files"] == [("stored.txt", b"stored")]
    assert state["exists"]
    assert state["submitted"] is None


def test_cancel_cleanup_transport_failure_preserves_mutation_state():
    rest = QueueClient()
    original = rest.request
    def request(method, path, *, json):
        if path.endswith("/delete-messages"):
            raise ToolExecutionError("cleanup transport failure")
        return original(method, path, json=json)
    rest.request = request
    result = asyncio.run(tool_for(CancelScheduledEmailTool, rest).safe_execute(uid=73))
    assert result["success"] is False
    assert result["cancelled"] is None
    assert result["cancellation_confirmed"] is False
    assert result["delete_submitted"] is True
    assert result["cleanup_confirmed"] is False
    assert result["uid"] == 73


def test_cancel_empty_new_uids_does_not_invent_deleted_items_identity():
    rest = QueueClient()
    result = asyncio.run(tool_for(CancelScheduledEmailTool, rest).safe_execute(uid=73))
    assert result["success"] is True
    assert result["removed_from_queue"] is True
    assert result["moved_to_deleted"] is None
    assert result["deleted_item_uids"] == []
    assert result["delivery_prevented"] is None


def test_queue_drain_during_paging_is_not_treated_as_verified_absence():
    rest = QueueClient(remove=False)
    original = rest.request
    def request(method, path, *, json):
        if path.endswith("/messages") and json["skip"] == 50:
            rest.messages = rest.messages[1:]
        return original(method, path, json=json)
    rest.request = request
    with pytest.raises(ToolExecutionError, match="total changed"):
        schedule_tools._scheduled_contains(tool_for(ScheduleEmailTool, rest), rest, 999)


def test_undo_acknowledgement_does_not_claim_send_was_stopped():
    rest = QueueClient()
    rest.sent = [73]
    result = asyncio.run(tool_for(schedule_tools.UndoSendTool, rest).safe_execute(uid=73))
    assert result["success"] is True
    assert result["undo_requested"] is True
    assert result["undo_acknowledged"] is True
    assert result["undone"] is None
    assert result["delivery_prevented"] is None
    assert rest.sent == [73]


def test_expired_undo_is_not_reported_as_acknowledged():
    rest = QueueClient()
    def request(*args, **kwargs):
        raise ToolExecutionError("400 Bad Request")
    rest.request = request
    result = asyncio.run(tool_for(schedule_tools.UndoSendTool, rest).safe_execute(uid=73))
    assert result["success"] is False
    assert result["uid"] == 73
    assert result["undo_acknowledged"] is False
    assert result["undone"] is False
