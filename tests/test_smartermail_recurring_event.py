"""SmarterMail recurring series: reminder "none" and inclusive until dates.

The webmail bundle encodes reminderIndex 0 as NONE and 1 as a reminder at the
event start. Its "until" picker stores local 23:59:59 of the chosen day, in
UTC. These tests pin what manage_recurring_event POSTs to events/save.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.tools.smartermail_calendar_contacts_tools import ManageRecurringEventTool

CAL_ID = "0f0e0d0c0b0a09080706050403020100"
DETAILS_PATH = f"/api/v1/calendars/events/owner/{CAL_ID}/42"


class CalendarRest:
    """Only the events/save round trip manage_recurring_event makes."""

    email = "owner@example.com"

    def __init__(self, details=None):
        self.saved = []
        self.details = details or {}

    def request(self, method, path, **kwargs):
        if path == "/api/v1/calendars/sources":
            return {"calendars": [{"id": CAL_ID, "name": "Calendar", "isPrimary": True}]}
        if path.startswith("/api/v1/calendars/events/save/owner/"):
            self.saved.append(kwargs["json"])
            return {"events": [{"id": 42}]}
        if method == "GET" and path == DETAILS_PATH:
            return {"details": dict(self.details)}
        raise AssertionError(f"unexpected {method} {path}")


def run(rest, server_tz="UTC", **kwargs):
    client = SimpleNamespace(
        config=SimpleNamespace(ews_email=rest.email, timezone=server_tz),
        smartermail=SimpleNamespace(enabled=True, get=lambda target: rest),
        mailbox_registry=None,
    )
    return asyncio.run(ManageRecurringEventTool(client).execute(**kwargs))


def create(rest, **overrides):
    kwargs = dict(
        action="create",
        subject="Standup",
        start="2026-09-01T09:00:00",
        end="2026-09-01T09:15:00",
        timezone="Europe/Berlin",
        pattern="daily",
    )
    kwargs.update(overrides)
    run(rest, **kwargs)
    assert len(rest.saved) == 1
    return rest.saved[0]


# ------------------------------------------------------------------ reminders
@pytest.mark.parametrize("value", [0, "0", "none"], ids=["int", "str", "none"])
def test_create_with_no_reminder_sends_none_not_at_start(value):
    payload = create(CalendarRest(), reminder_minutes=value)
    assert payload["reminderIndex"] == 0


def test_create_still_maps_real_reminder_minutes():
    payload = create(CalendarRest(), reminder_minutes=15)
    assert payload["reminderIndex"] == 4


def test_update_can_clear_an_existing_reminder():
    rest = CalendarRest(details={
        "subject": "Standup",
        "reminderIndex": 4,
        "start": {"dt": "2026-09-01T09:00:00.000", "tz": "Europe/Berlin"},
        "end": {"dt": "2026-09-01T09:15:00.000", "tz": "Europe/Berlin"},
        "recurrence": {"type": 4},
    })
    run(rest, action="update", event_id=42, reminder_minutes=0)
    assert rest.saved[-1]["reminderIndex"] == 0


# ------------------------------------------------------------------ until date
def test_date_only_until_includes_that_days_occurrence():
    payload = create(CalendarRest(), until_date="2026-09-10")
    rec = payload["recurrence"]
    assert rec["untilDateEnabled"] is True
    # Local 23:59:59 on 10 Sep in Berlin (CEST, UTC+2).
    assert rec["untilDate"] == "2026-09-10T21:59:59Z"
    # The 09:00 Berlin occurrence on the last day must not be cut off.
    assert rec["untilDate"] >= "2026-09-10T07:00:00Z"


def test_date_only_until_uses_the_event_zone_west_of_utc():
    payload = create(
        CalendarRest(),
        start="2026-09-01T09:00:00",
        end="2026-09-01T09:15:00",
        timezone="America/New_York",
        until_date="2026-09-10",
    )
    assert payload["recurrence"]["untilDate"] == "2026-09-11T03:59:59Z"


def test_full_datetime_until_is_kept_as_given():
    payload = create(CalendarRest(), until_date="2026-09-10T12:00:00+02:00")
    assert payload["recurrence"]["untilDate"] == "2026-09-10T10:00:00Z"
