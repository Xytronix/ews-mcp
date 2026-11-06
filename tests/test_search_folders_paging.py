"""Multi-folder ``search_emails`` pages through one merged result.

Advanced and full_text mode applied the page ``offset`` inside every folder
and then filled a per-folder share of the page, while ``next_offset`` counted
the merged page. Page two therefore skipped whatever each folder held between
its share and the offset. Every test here walks real pages of the response.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exchangelib import Mailbox  # noqa: E402

from src.tools import email_tools  # noqa: E402
from tests.support import ADDRESS  # noqa: E402


def _mail(folder: str, day: int, subject: str = "status report", sender: str = "Peer"):
    return SimpleNamespace(
        id=f"{folder}-{day}",
        subject=subject,
        sender=Mailbox(name=sender, email_address=f"{sender.lower()}@example.com"),
        to_recipients=[],
        text_body="",
        body="",
        datetime_received=datetime(2026, 3, day, tzinfo=timezone.utc),
        is_read=True,
        has_attachments=False,
        importance="Normal",
        categories=[],
    )


class _Query:
    """A folder query that sorts and slices like FindItem.

    Like SmarterMail it ignores restrictions, so any filtering a test
    expects has to come from the tool's own verification.
    """

    def __init__(self, items, count_fails=False):
        self.items = list(items)
        self.count_fails = count_fails

    def filter(self, *args, **kwargs):
        return self

    exclude = filter

    def only(self, *fields):
        return self

    def order_by(self, field):
        # An unknown field raises, as exchangelib does. A mailbox orders by
        # its display name.
        name = field.lstrip("-")

        def value(item):
            found = getattr(item, name)
            return found.name.casefold() if isinstance(found, Mailbox) else found

        ordered = sorted(self.items, key=value, reverse=field.startswith("-"))
        return _Query(ordered, self.count_fails)

    def count(self):
        if self.count_fails:
            raise RuntimeError("count unavailable")
        return len(self.items)

    def __getitem__(self, window):
        return self.items[window]


class _Folder:
    def __init__(self, name, items, count_fails=False):
        self.name = name
        self.items = items
        self.count_fails = count_fails

    def filter(self, *args, **kwargs):
        return _Query(self.items, self.count_fails).order_by("-datetime_received")

    def all(self):
        return self.filter()


class _Client:
    mailbox_registry = None

    def __init__(self, inbox, sent):
        self.config = SimpleNamespace(ews_email=ADDRESS)
        self.account = SimpleNamespace(
            primary_smtp_address=ADDRESS,
            inbox=inbox, sent=sent,
            drafts=_Folder("Drafts", []), trash=_Folder("Deleted Items", []),
            junk=_Folder("Junk Email", []),
        )

    def get_account(self, target_mailbox=None):
        return self.account


def _client(inbox_days, sent_days, **folder_options):
    return _Client(
        _Folder("Inbox", [_mail("inbox", d) for d in inbox_days], **folder_options),
        _Folder("Sent Items", [_mail("sent", d) for d in sent_days]),
    )


def _page(client, **kwargs):
    tool = email_tools.SearchEmailsTool(client)
    return asyncio.run(tool.execute(fields=["message_id", "received_time"], **kwargs))


def _ids(response):
    return [item["message_id"] for item in response["items"]]


# Inbox and Sent interleave: 12 11 10 9 | 8 7 | 3 | 2.
INBOX_DAYS = (12, 11, 10, 9, 3)
SENT_DAYS = (8, 7, 2)
NEWEST_FIRST = [
    "inbox-12", "inbox-11", "inbox-10", "inbox-9",
    "sent-8", "sent-7", "inbox-3", "sent-2",
]


def test_advanced_pages_one_and_two_cover_every_item_once():
    client = _client(INBOX_DAYS, SENT_DAYS)
    common = dict(mode="advanced", search_scope=["inbox", "sent"], is_read=True,
                  max_results=4)

    first = _page(client, **common)
    second = _page(client, offset=first["next_offset"], **common)

    assert _ids(first) == NEWEST_FIRST[:4]
    assert first["next_offset"] == 4
    assert first["total_available"] == 8
    assert _ids(second) == NEWEST_FIRST[4:]
    assert "next_offset" not in second
    assert sorted(_ids(first) + _ids(second)) == sorted(NEWEST_FIRST)


def test_advanced_merge_honours_ascending_order():
    client = _client(INBOX_DAYS, SENT_DAYS)
    common = dict(mode="advanced", search_scope=["inbox", "sent"], is_read=True,
                  sort_order="ascending", max_results=4)

    first = _page(client, **common)
    second = _page(client, offset=first["next_offset"], **common)

    assert _ids(first) + _ids(second) == NEWEST_FIRST[::-1]


def test_advanced_pages_over_locally_verified_subjects():
    inbox = [_mail("inbox", 12, "Budget Q1"), _mail("inbox", 11, "lunch"),
             _mail("inbox", 10, "re: budget q1"), _mail("inbox", 9, "lunch")]
    sent = [_mail("sent", 8, "BUDGET Q1 draft"), _mail("sent", 7, "budget q1 final"),
            _mail("sent", 6, "lunch")]
    client = _Client(_Folder("Inbox", inbox), _Folder("Sent Items", sent))
    common = dict(mode="advanced", search_scope=["inbox", "sent"],
                  subject_contains="budget q1", max_results=2)

    first = _page(client, **common)
    second = _page(client, offset=first["next_offset"], **common)

    assert _ids(first) == ["inbox-12", "inbox-10"]
    assert _ids(second) == ["sent-8", "sent-7"]
    assert first["total_available"] == 4
    assert "next_offset" not in second


def test_advanced_single_folder_still_pages_on_the_server():
    client = _client(INBOX_DAYS, SENT_DAYS)

    response = _page(client, mode="advanced", search_scope=["inbox"], is_read=True,
                     max_results=2, offset=2)

    assert _ids(response) == ["inbox-10", "inbox-9"]
    assert response["next_offset"] == 4
    assert response["total_available"] == 5


def test_unknown_folder_count_leaves_the_total_unknown_but_paging_intact():
    client = _client(INBOX_DAYS, SENT_DAYS, count_fails=True)
    common = dict(mode="advanced", search_scope=["inbox", "sent"], is_read=True,
                  max_results=4)

    first = _page(client, **common)
    second = _page(client, offset=first["next_offset"], **common)

    assert first["total_available"] is None
    assert _ids(first) + _ids(second) == NEWEST_FIRST
    assert "next_offset" not in second


def test_full_text_pages_one_and_two_cover_every_item_once():
    client = _client(INBOX_DAYS, SENT_DAYS)
    common = dict(mode="full_text", query="report", max_results=4)

    first = _page(client, **common)
    second = _page(client, offset=first["next_offset"], **common)

    assert _ids(first) == NEWEST_FIRST[:4]
    assert first["next_offset"] == 4
    assert first["total_available"] == 8
    assert _ids(second) == NEWEST_FIRST[4:]
    assert "next_offset" not in second


def test_full_text_exact_phrase_pages_and_counts_real_matches():
    inbox = [_mail("inbox", 12, "quarterly report"), _mail("inbox", 11, "report quarterly"),
             _mail("inbox", 10, "the quarterly report v2")]
    sent = [_mail("sent", 9, "Quarterly Report sent"), _mail("sent", 8, "report, quarterly"),
            _mail("sent", 7, "fwd: quarterly report")]
    client = _Client(_Folder("Inbox", inbox), _Folder("Sent Items", sent))
    common = dict(mode="full_text", query="quarterly report", exact_phrase=True,
                  max_results=2)

    first = _page(client, **common)
    second = _page(client, offset=first["next_offset"], **common)

    assert _ids(first) == ["inbox-12", "inbox-10"]
    assert _ids(second) == ["sent-9", "sent-7"]
    assert first["total_available"] == 4
    assert "next_offset" not in second


def test_advanced_sort_by_from_merges_folders_by_sender():
    inbox = [_mail("inbox", 12, sender="Dana"), _mail("inbox", 11, sender="bea"),
             _mail("inbox", 10, sender="Ed")]
    sent = [_mail("sent", 9, sender="Cy"), _mail("sent", 8, sender="Al"),
            _mail("sent", 7, sender="Fay")]
    client = _Client(_Folder("Inbox", inbox), _Folder("Sent Items", sent))
    common = dict(mode="advanced", search_scope=["inbox", "sent"], is_read=True,
                  sort_by="from", sort_order="ascending", max_results=3)

    first = _page(client, **common)
    second = _page(client, offset=first["next_offset"], **common)

    assert "meta" not in first
    # Al, bea, Cy | Dana, Ed, Fay
    assert _ids(first) == ["sent-8", "inbox-11", "sent-9"]
    assert _ids(second) == ["inbox-12", "inbox-10", "sent-7"]
    assert "next_offset" not in second
