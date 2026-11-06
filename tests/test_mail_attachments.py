"""Attachment support on the non-Exchange mail backends (Graph + IMAP).

These exercise the parts that have no server dependency: attachment selection,
the Graph HTTP contract (via a mock transport) and the IMAP MIME walk (via a
raw message), including the draft-only guards that must fail fast instead of
hanging.
"""
from __future__ import annotations

import asyncio
import base64
import email
import json
import sys
from email.message import EmailMessage
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.mail_backends.accounts import MailAccount  # noqa: E402
from src.mail_backends.base import (  # noqa: E402
    MailAttachmentInfo,
    MailBackendError,
    OutgoingAttachment,
    OutgoingMessage,
    select_attachment,
)
from src.mail_backends.graph_backend import GraphBackend  # noqa: E402
from src.mail_backends.imap_backend import IMAPBackend  # noqa: E402

PDF_BYTES = b"%PDF-1.4 tiny"


# ---------------------------------------------------------------- selection
def test_select_attachment_prefers_id_then_name():
    items = [
        MailAttachmentInfo(name="a.pdf", attachment_id="1"),
        MailAttachmentInfo(name="b.pdf", attachment_id="2"),
    ]
    assert select_attachment(items, "2", None).name == "b.pdf"
    assert select_attachment(items, None, "A.PDF").attachment_id == "1"


def test_select_attachment_rejects_ambiguous_name():
    items = [
        MailAttachmentInfo(name="dup.pdf", attachment_id="1"),
        MailAttachmentInfo(name="dup.pdf", attachment_id="2"),
    ]
    with pytest.raises(MailBackendError, match="multiple attachments"):
        select_attachment(items, None, "dup.pdf")


def test_select_attachment_lists_what_is_available():
    items = [MailAttachmentInfo(name="only.pdf", attachment_id="1")]
    with pytest.raises(MailBackendError, match="only.pdf"):
        select_attachment(items, None, "missing.pdf")


# -------------------------------------------------------------------- graph
def graph_backend(handler) -> GraphBackend:
    account = MailAccount(
        email="user@outlook.de",
        provider="outlook",
        settings={"client_id": "cid", "refresh_token": "rt"},
    )
    backend = GraphBackend(account)
    backend._access_token = "token"
    backend._token_expires_at = 1 << 40
    backend._client = httpx.Client(transport=httpx.MockTransport(handler))
    return backend


def test_graph_list_attachments_maps_metadata_and_filters_inline():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/messages/MID/attachments")
        # contentId only exists on fileAttachment; naming it in $select is a
        # 400 BadRequest from Graph.
        assert "contentId" not in (request.url.params.get("$select") or "")
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "att1",
                        "name": "invoice.pdf",
                        "contentType": "application/pdf",
                        "size": 12,
                        "isInline": False,
                    },
                    {
                        "id": "att2",
                        "name": "logo.png",
                        "contentType": "image/png",
                        "size": 5,
                        "isInline": True,
                        "contentId": "logo",
                    },
                ]
            },
        )

    backend = graph_backend(handler)
    everything = backend.list_attachments("MID")
    assert [a.name for a in everything] == ["invoice.pdf", "logo.png"]
    assert everything[1].content_id == "logo"

    files_only = backend.list_attachments("MID", include_inline=False)
    assert [a.attachment_id for a in files_only] == ["att1"]


def test_graph_search_keeps_has_attachments_out_of_the_server_filter():
    """Graph 400s ("InefficientFilter") on hasAttachments beside an $orderby."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["filter"] = request.url.params.get("$filter")
        seen["orderby"] = request.url.params.get("$orderby")
        return httpx.Response(
            200,
            json={
                "value": [
                    {"id": "m1", "subject": "plain", "hasAttachments": False},
                    {"id": "m2", "subject": "with file", "hasAttachments": True},
                ]
            },
        )

    found = graph_backend(handler).search_messages(
        folder="inbox", has_attachments=True, unread_only=True, limit=5
    )
    assert seen["filter"] == "isRead eq false"
    assert seen["orderby"] == "receivedDateTime desc"
    assert [m.id for m in found] == ["m2"]


def test_graph_search_follows_pages_until_the_limit_is_filled():
    """$search takes no $filter, so recall depends on walking nextLink.

    One page of $top=limit used to be filtered down to a handful of hits and
    reported as "all there is".
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        page = int(request.url.params.get("page") or 0)
        if page >= 3:
            return httpx.Response(200, json={"value": []})
        rows = [
            {
                "id": f"m{page}{i}",
                "subject": "q",
                "from": {"emailAddress": {"address": "boss@corp.de" if i == 0 else "x@y.de"}},
            }
            for i in range(4)
        ]
        return httpx.Response(
            200,
            json={
                "value": rows,
                "@odata.nextLink": f"https://graph.microsoft.com/v1.0/next?page={page + 1}",
            },
        )

    found = graph_backend(handler).search_messages(
        folder="inbox", query="q", from_address="boss@corp.de", limit=3
    )
    assert [m.id for m in found] == ["m00", "m10", "m20"]
    assert len(calls) == 3


def test_graph_errors_name_the_wall_and_the_way_round_it():
    def denied(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}}
        )

    with pytest.raises(MailBackendError, match="Mail.ReadWrite"):
        graph_backend(denied).list_attachments("MID")

    def inefficient(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"code": "InefficientFilter", "message": "bad filter"}}
        )

    with pytest.raises(MailBackendError, match="narrow the result locally"):
        graph_backend(inefficient).search_messages(folder="inbox", subject="x")


def test_graph_get_attachment_decodes_content_bytes():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/attachments"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "att1",
                            "name": "invoice.pdf",
                            "contentType": "application/pdf",
                            "size": len(PDF_BYTES),
                        }
                    ]
                },
            )
        assert request.url.path.endswith("/attachments/att1")
        return httpx.Response(
            200,
            json={
                "id": "att1",
                "name": "invoice.pdf",
                "contentType": "application/pdf",
                "contentBytes": base64.b64encode(PDF_BYTES).decode(),
            },
        )

    attachment = graph_backend(handler).get_attachment("MID", attachment_name="invoice.pdf")
    assert attachment.content == PDF_BYTES
    assert attachment.size == len(PDF_BYTES)


def test_graph_get_attachment_falls_back_to_value_for_item_attachments():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/attachments"):
            return httpx.Response(
                200,
                json={"value": [{"id": "att9", "name": "fwd.eml", "size": 3}]},
            )
        if path.endswith("/$value"):
            return httpx.Response(200, content=b"raw")
        return httpx.Response(200, json={"id": "att9", "name": "fwd.eml"})

    attachment = graph_backend(handler).get_attachment("MID", attachment_id="att9")
    assert attachment.content == b"raw"


def test_graph_add_attachment_refuses_non_draft_without_posting():
    posted: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(request)
            return httpx.Response(201, json={"id": "new"})
        return httpx.Response(200, json={"id": "MID", "isDraft": False})

    with pytest.raises(MailBackendError, match="only allows attachment changes on a draft"):
        graph_backend(handler).add_attachment("MID", "x.pdf", PDF_BYTES)
    assert posted == []


def test_graph_add_attachment_posts_file_attachment_to_draft():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            import json as _json

            seen.update(_json.loads(request.content))
            return httpx.Response(201, json={"id": "att-new"})
        return httpx.Response(200, json={"id": "MID", "isDraft": True})

    result = graph_backend(handler).add_attachment(
        "MID", "x.pdf", PDF_BYTES, content_type="application/pdf"
    )
    assert seen["@odata.type"] == "#microsoft.graph.fileAttachment"
    assert base64.b64decode(seen["contentBytes"]) == PDF_BYTES
    assert result["attachment_id"] == "att-new"


def test_graph_inline_attachment_reports_the_cid_it_stored():
    """An inline image is only reachable as cid:<id> from the HTML body."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            import json as _json

            seen.update(_json.loads(request.content))
            return httpx.Response(201, json={"id": "att-new"})
        return httpx.Response(200, json={"id": "MID", "isDraft": True})

    result = graph_backend(handler).add_attachment(
        "MID",
        "logo.png",
        PDF_BYTES,
        content_type="image/png",
        is_inline=True,
        content_id="logo.png",
    )
    assert (seen["isInline"], seen["contentId"]) == (True, "logo.png")
    assert result["content_id"] == "logo.png"


def test_graph_get_mime_returns_raw_bytes():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/messages/MID/$value")
        return httpx.Response(200, content=b"From: a@b\r\n\r\nhi")

    assert graph_backend(handler).get_mime("MID").startswith(b"From: a@b")


# --------------------------------------------------------------------- imap
def raw_message_with_attachment() -> bytes:
    msg = EmailMessage()
    msg["From"] = "a@b.de"
    msg["To"] = "c@d.de"
    msg["Subject"] = "with files"
    msg.set_content("body text")
    msg.add_attachment(
        PDF_BYTES, maintype="application", subtype="pdf", filename="invoice.pdf"
    )
    msg.add_attachment(
        b"\x89PNG", maintype="image", subtype="png", filename="logo.png",
        disposition="inline", cid="<logo>",
    )
    return msg.as_bytes()


def imap_backend(monkeypatch, folder_map=None) -> IMAPBackend:
    account = MailAccount(
        email="user@icloud.com",
        provider="icloud",
        password="app-specific",
    )
    backend = IMAPBackend(account)
    raw = raw_message_with_attachment()
    monkeypatch.setattr(
        backend,
        "_fetch_full",
        lambda message_id, folder, writable=False: ("1", "42", {"raw": raw, "size": len(raw), "flags": ["\\Draft"]}),
    )
    mapping = folder_map or {"inbox": "INBOX", "drafts": "Drafts"}
    monkeypatch.setattr(
        backend, "_native_folder", lambda token: mapping[token.lower()]
    )
    return backend


def test_imap_list_attachments_uses_part_ids_and_flags_inline(monkeypatch):
    backend = imap_backend(monkeypatch)
    items = backend.list_attachments("1:42")
    assert [i.name for i in items] == ["invoice.pdf", "logo.png"]
    assert all(i.attachment_id.startswith("part-") for i in items)
    assert items[1].is_inline is True
    assert items[1].content_id == "logo"

    assert [i.name for i in backend.list_attachments("1:42", include_inline=False)] == [
        "invoice.pdf"
    ]


def test_imap_get_attachment_returns_decoded_part(monkeypatch):
    backend = imap_backend(monkeypatch)
    by_name = backend.get_attachment("1:42", attachment_name="invoice.pdf")
    assert by_name.content == PDF_BYTES

    by_id = backend.get_attachment("1:42", attachment_id=by_name.attachment_id)
    assert by_id.name == "invoice.pdf"


def test_imap_get_mime_returns_the_whole_message(monkeypatch):
    mime = imap_backend(monkeypatch).get_mime("1:42")
    assert b"Subject: with files" in mime


def test_imap_add_attachment_outside_drafts_fails_fast(monkeypatch):
    backend = imap_backend(monkeypatch)
    called = []
    monkeypatch.setattr(backend, "_connect", lambda: called.append(1))

    with pytest.raises(MailBackendError, match="drafts folder"):
        backend.add_attachment("1:42", "x.pdf", PDF_BYTES, folder="inbox")
    assert called == []


def test_imap_add_attachment_rewrites_the_draft(monkeypatch):
    backend = imap_backend(monkeypatch)
    appended: dict = {}

    def fake_append(native, payload):
        appended["native"] = native
        appended["payload"] = payload
        monkeypatch.setattr(
            backend, "_fetch_full",
            lambda message_id, folder, writable=False: ("1", "99", {"raw": payload, "flags": ["\\Draft"]}),
        )
        return "99"

    monkeypatch.setattr(backend, "_append_raw", fake_append)
    monkeypatch.setattr(backend, "_store", lambda *a: None)
    monkeypatch.setattr(backend, "_expunge", lambda uid: None)
    monkeypatch.setattr(backend, "_select", lambda native, readonly=True: (3, "1"))

    result = backend.add_attachment(
        "1:42", "extra.txt", b"hello there", folder="drafts", content_type="text/plain"
    )
    assert result["id"] == "1:99"
    assert result["previous_id"] == "1:42"
    assert appended["native"] == "Drafts"
    rewritten = email.message_from_bytes(appended["payload"])
    names = [p.get_filename() for p in rewritten.walk() if p.get_filename()]
    assert names == ["invoice.pdf", "logo.png", "extra.txt"]


def test_imap_inline_attachment_gets_a_cid_header_and_reports_it(monkeypatch):
    backend = imap_backend(monkeypatch)
    appended: dict = {}

    def append_inline(native, payload):
        appended["payload"] = payload
        monkeypatch.setattr(
            backend, "_fetch_full",
            lambda message_id, folder, writable=False: ("1", "99", {"raw": payload, "flags": ["\\Draft"]}),
        )
        return "99"

    monkeypatch.setattr(backend, "_append_raw", append_inline)
    monkeypatch.setattr(backend, "_store", lambda *a: None)
    monkeypatch.setattr(backend, "_expunge", lambda uid: None)
    monkeypatch.setattr(backend, "_select", lambda native, readonly=True: (3, "1"))

    result = backend.add_attachment(
        "1:42",
        "banner.png",
        b"\x89PNG",
        folder="drafts",
        content_type="image/png",
        is_inline=True,
        content_id="banner.png",
    )
    rewritten = email.message_from_bytes(appended["payload"])
    inline = [p for p in rewritten.walk() if p.get_filename() == "banner.png"]
    assert inline and inline[0]["Content-ID"] == "<banner.png>"
    assert inline[0].get_content_disposition() == "inline"
    # The reported value is the one the body writes: no angle brackets.
    assert result["content_id"] == "banner.png"


def test_imap_delete_attachment_drops_only_that_part(monkeypatch):
    backend = imap_backend(monkeypatch)
    appended: dict = {}

    def fake_append(native, payload):
        appended["payload"] = payload
        monkeypatch.setattr(
            backend, "_fetch_full",
            lambda message_id, folder, writable=False: ("1", "77", {"raw": payload, "flags": ["\\Draft"]}),
        )
        return "77"

    monkeypatch.setattr(backend, "_append_raw", fake_append)
    monkeypatch.setattr(backend, "_store", lambda *a: None)
    monkeypatch.setattr(backend, "_expunge", lambda uid: None)
    monkeypatch.setattr(backend, "_select", lambda native, readonly=True: (3, "1"))

    result = backend.delete_attachment(
        "1:42", folder="drafts", attachment_name="invoice.pdf"
    )
    assert result["attachment_name"] == "invoice.pdf"
    assert b"invoice.pdf" not in appended["payload"]
    assert b"logo.png" in appended["payload"]


# ---------------------------------------------------------- send with files
def test_imap_send_attaches_regular_and_inline_parts():
    account = MailAccount(
        email="user@icloud.com",
        provider="icloud",
        password="app-specific",
        settings={"smtp_host": "smtp.mail.me.com"},
    )
    backend = IMAPBackend(account)
    sent: list = []
    backend._smtp_send = lambda outgoing, envelope: sent.append((outgoing, list(envelope))) or {}
    backend._append_to_sent = lambda outgoing: "Sent Messages"

    result = backend.send_message(
        OutgoingMessage(
            to=["peer@example.com"],
            cc=["cc@example.com"],
            bcc=["hidden@example.com"],
            subject="files",
            body="<p>see attached</p>",
            body_format="html",
            attachments=[
                OutgoingAttachment(
                    name="invoice.pdf",
                    content=PDF_BYTES,
                    content_type="application/pdf",
                ),
                OutgoingAttachment(
                    name="logo.png",
                    content=b"\x89PNG",
                    content_type="image/png",
                    is_inline=True,
                    content_id="logo",
                ),
            ],
        )
    )
    assert result["sent"] is True
    assert result["saved_to_sent"] == "Sent Messages"
    mime, envelope = sent[0]
    assert envelope == [
        "peer@example.com",
        "cc@example.com",
        "hidden@example.com",
    ]
    by_name = {p.get_filename(): p for p in mime.walk() if p.get_filename()}
    assert set(by_name) == {"invoice.pdf", "logo.png"}
    assert by_name["logo.png"].get_content_disposition() == "inline"
    assert by_name["logo.png"]["Content-ID"] == "<logo>"
    assert by_name["invoice.pdf"].get_filename() == "invoice.pdf"


def test_graph_send_uploads_base64_attachment_before_submitting():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1.0/me/messages":
            return httpx.Response(201, json={"id": "DRAFT-1"})
        if request.method == "GET":
            return httpx.Response(200, json={"value": [{"id": "ATT-1", "name": "invoice.pdf"}]})
        if request.url.path.endswith("/attachments"):
            return httpx.Response(201, json={"id": "ATT-1"})
        return httpx.Response(202)

    graph_backend(handler).send_message(
        OutgoingMessage(
            to=["peer@example.com"],
            subject="invoice",
            body="see pdf",
            attachments=[
                OutgoingAttachment(
                    name="invoice.pdf",
                    content=PDF_BYTES,
                    content_type="application/pdf",
                )
            ],
        )
    )
    assert seen[-1].url.path.endswith("/messages/DRAFT-1/send")
    att = json.loads(next(request.content for request in seen
                          if request.method == "POST" and request.url.path.endswith("/attachments")))
    assert att["@odata.type"] == "#microsoft.graph.fileAttachment"
    assert att["name"] == "invoice.pdf"
    assert att["contentType"] == "application/pdf"
    assert att["isInline"] is False
    assert base64.b64decode(att["contentBytes"]) == PDF_BYTES


def test_graph_reply_with_attachments_goes_through_a_draft():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path.endswith("/createReply"):
            payload = json.loads(request.content)
            if "comment" in payload and "body" in payload.get("message", {}):
                return httpx.Response(400, json={"error": {
                    "code": "ErrorInvalidRequest",
                    "message": "Specify either comment or message.body, not both",
                }})
            return httpx.Response(201, json={"id": "DRAFT-1", "body": {
                "contentType": "html", "content": "<p>Original reply history</p>",
            }})
        if request.method == "PATCH":
            assert "Original reply history" in json.loads(request.content)["body"]["content"]
            return httpx.Response(200, json={"id": "DRAFT-1"})
        if request.method == "GET":
            return httpx.Response(200, json={"value": [{"id": "ATT-1", "name": "note.txt"}]})
        if path.endswith("/attachments"):
            return httpx.Response(201, json={"id": "ATT-1"})
        if path.endswith("/send"):
            return httpx.Response(202)
        return httpx.Response(500, json={"error": {"message": path}})

    graph_backend(handler).send_message(
        OutgoingMessage(
            to=[],
            body="with file",
            reply_to_message_id="M1",
            attachments=[
                OutgoingAttachment(
                    name="note.txt", content=b"hi", content_type="text/plain"
                )
            ],
        )
    )
    assert calls[-1].url.path == "/v1.0/me/messages/DRAFT-1/send"
    posted = json.loads(next(request.content for request in calls
                             if request.method == "POST" and request.url.path.endswith("/attachments")))
    assert posted["name"] == "note.txt"
    assert posted["@odata.type"] == "#microsoft.graph.fileAttachment"
    assert base64.b64decode(posted["contentBytes"]) == b"hi"


def test_graph_reply_without_attachments_preserves_quoted_history():
    stored = {"contentType": "html", "content": "<p>Quoted history</p>"}
    submitted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/createReply"):
            assert "body" not in json.loads(request.content)["message"]
            return httpx.Response(201, json={"id": "REPLY", "body": stored})
        if request.method == "PATCH":
            stored.update(json.loads(request.content)["body"])
            return httpx.Response(200, json={"id": "REPLY"})
        if request.method == "GET":
            return httpx.Response(200, json={"value": []})
        assert request.url.path.endswith("/REPLY/send")
        submitted.append(stored["content"])
        return httpx.Response(202)

    backend = graph_backend(handler)
    try:
        result = backend.send_message(OutgoingMessage(
            to=[], body="<em>Answer</em>", body_format="html", reply_to_message_id="M1",
        ))
    finally:
        backend.close()
    assert submitted == ["<em>Answer</em><p>Quoted history</p>"]
    assert result["accepted_by_mail_server"] is True


def test_graph_oversize_attachments_are_refused_before_any_request():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(202)

    oversize = b"x" * (3 * 1024 * 1024 + 1)
    with pytest.raises(MailBackendError):
        graph_backend(handler).send_message(
            OutgoingMessage(
                to=["peer@example.com"],
                subject="too big",
                body="no",
                attachments=[OutgoingAttachment(name="big.bin", content=oversize)],
            )
        )
    assert calls == []


def test_routed_send_loads_files_onto_the_outgoing_message():
    from src.tools.mailbox_tools import SendEmailTool

    captured: dict = {}

    class _Backend:
        def send_message(self, message):
            captured["attachments"] = list(message.attachments)
            return {"sent": True, "backend": "imap", "to": list(message.to)}

    class _Client:
        mailbox_registry = None
        config = None

    account = MailAccount(email="user@icloud.com", provider="icloud", settings={})
    payload = base64.b64encode(b"hello").decode("ascii")
    response = asyncio.run(
        SendEmailTool(_Client()).execute_routed(
            account,
            _Backend(),
            to=["peer@example.com"],
            body="hi",
            files=[
                {
                    "file_content": payload,
                    "file_name": "hello.txt",
                    "content_type": "text/plain",
                }
            ],
        )
    )
    att = captured["attachments"][0]
    assert att.name == "hello.txt"
    assert att.content == b"hello"
    assert att.content_type == "text/plain"
    assert response["attachments"] == [
        {
            "name": "hello.txt",
            "size": 5,
            "content_type": "text/plain",
            "is_inline": False,
        }
    ]


def test_imap_attachment_flag_covers_the_shapes_that_carry_files():
    """Listing has only headers, so the flag is a Content-Type judgement.

    multipart/signed (S/MIME) and a bare application/* body used to be
    reported as attachment-free, which also made a has_attachments search
    drop them.
    """

    def flag(content_type: str) -> bool:
        headers = email.message_from_string(
            f"Content-Type: {content_type}\n\n" if content_type else "\n"
        )
        return IMAPBackend._headers_suggest_attachments(headers)

    assert flag("multipart/mixed; boundary=x")
    assert flag("multipart/signed; protocol=application/pkcs7-signature")
    assert flag("application/pdf")
    assert not flag("multipart/alternative; boundary=x")
    assert not flag("text/plain; charset=utf-8")
    assert not flag("")
