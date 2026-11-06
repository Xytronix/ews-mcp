"""Graph calendar public paths and conservative free/busy scheduling."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.mail_backends.accounts import MailAccount
from src.mail_backends.base import MailBackendError
from src.mail_backends.graph_backend import GraphBackend
from src.tools import graph_calendar_tools as tools


@pytest.fixture
def make_backend():
    backends = []

    def make(handler, **settings):
        account = MailAccount(email="owner@example.com", provider="outlook", settings={
            "client_id": "cid", "refresh_token": "rt", **settings,
        })
        backend = GraphBackend(account)
        backend._access_token = "token"
        backend._token_expires_at = 1 << 40
        backend._client = httpx.Client(transport=httpx.MockTransport(handler))
        backends.append(backend)
        return backend

    yield make
    for backend in backends:
        backend.close()


def run(tool_cls, backend, **kwargs):
    registry = SimpleNamespace(find_account=lambda address: backend._account,
                               get_backend=lambda address: backend)
    tool = tool_cls(SimpleNamespace(mailbox_registry=registry, config=None))
    return asyncio.run(tool.execute(**kwargs))


def event(**fields):
    return {"id": "event", "subject": "Kept", "start": {"dateTime": "2026-09-16T09:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-09-16T10:00:00", "timeZone": "UTC"}, **fields}


def test_create_timezone_and_optional_fields(make_backend):
    stored = {}
    def handler(request):
        assert request.url.path == "/v1.0/me/events"
        stored.update(json.loads(request.content))
        return httpx.Response(201, json={"id": "created", **stored})
    result = run(tools.CreateAppointmentTool, make_backend(handler), subject="Call",
                 start_time="2026-09-16T09:00:00+02:00", end_time="2026-09-16T10:00:00+02:00",
                 attendees=["peer@example.com"], categories=["Work"], body="Notes", reminder_minutes=0)
    assert result["item_id"] == "created"
    assert stored["start"] == {"dateTime": "2026-09-16T07:00:00", "timeZone": "UTC"}
    assert stored["attendees"][0]["emailAddress"]["address"] == "peer@example.com"
    assert stored["reminderMinutesBeforeStart"] == 0


def test_all_day_bounds_and_timezone(make_backend, monkeypatch):
    monkeypatch.setenv("TIMEZONE", "Europe/Berlin")
    stored = {}
    def handler(request):
        stored.update(json.loads(request.content))
        return httpx.Response(201, json={"id": "all-day", **stored})
    backend = make_backend(handler)
    result = run(tools.CreateAppointmentTool, backend, subject="Day", is_all_day=True,
                 start_time="2026-09-16", end_time="2026-09-17")
    assert result["success"]
    assert stored["start"]["dateTime"] == "2026-09-16T00:00:00"
    assert stored["start"]["timeZone"] == "Europe/Berlin"
    with pytest.raises(MailBackendError, match="midnight"):
        backend.create_appointment(subject="Bad", is_all_day=True,
                                   start_time="2026-09-16T12:00:00+02:00", end_time="2026-09-17T12:00:00+02:00")


def test_calendar_pagination_and_date_only_end(make_backend):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={"value": [event(id="first")],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendarView?$skiptoken=next"})
        return httpx.Response(200, json={"value": [event(id="second", location=None, organizer=None, attendees=None)]})
    result = run(tools.GetCalendarTool, make_backend(handler), start_date="2026-09-16", end_date="2026-09-16")
    assert [e["item_id"] for e in result["events"]] == ["first", "second"]
    assert calls[0].url.params["endDateTime"].startswith("2026-09-17T00:00:00")
    assert result["events"][1]["attendees"] == []
    assert result["events"][0]["start"].endswith("+00:00")


def test_partial_update_clears_only_explicit_fields(make_backend):
    stored = event(body={"content": "Kept body"}, categories=["Keep"], location={"displayName": "Room"})
    def handler(request):
        if request.method == "PATCH":
            stored.update(json.loads(request.content))
        return httpx.Response(200, json=stored)
    result = run(tools.UpdateAppointmentTool, make_backend(handler), item_id="event", location="", categories=[])
    assert result["success"]
    assert stored["subject"] == "Kept"
    assert stored["body"]["content"] == "Kept body"
    assert stored["categories"] == []
    assert stored["location"]["displayName"] == ""


def test_partial_time_update_rejects_end_before_existing_start(make_backend):
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json=event())
    result = run(tools.UpdateAppointmentTool, make_backend(handler), item_id="event", end_time="2026-09-16T08:00:00Z")
    assert result["success"] is False
    assert "after" in result["error"]


@pytest.mark.parametrize("organizer,attendees,send,action", [
    (True, [{"emailAddress": {"address": "peer@example.com"}}], True, "cancel"),
    (False, [{"emailAddress": {"address": "peer@example.com"}}], True, "delete"),
    (True, [], False, "delete"),
])
def test_delete_reports_actual_cancellation(make_backend, organizer, attendees, send, action):
    mutations = []
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=event(isOrganizer=organizer, attendees=attendees))
        mutations.append((request.method, request.url.path))
        return httpx.Response(202 if action == "cancel" else 204)
    result = run(tools.DeleteAppointmentTool, make_backend(handler), item_id="event", send_cancellation=send)
    assert result["cancellation_sent"] is (action == "cancel")
    assert mutations == ([("POST", "/v1.0/me/events/event/cancel")] if action == "cancel" else [("DELETE", "/v1.0/me/events/event")])


def test_organizer_cannot_silently_delete_graph_meeting(make_backend):
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json=event(isOrganizer=True, attendees=[{}]))
    result = run(tools.DeleteAppointmentTool, make_backend(handler), item_id="event", send_cancellation=False)
    assert result["success"] is False
    assert "cannot suppress" in result["error"]


@pytest.mark.parametrize("response,action", [("Accept", "accept"), ("Tentative", "tentativelyAccept"), ("Decline", "decline")])
def test_response_uses_native_action(make_backend, response, action):
    def handler(request):
        assert request.url.path.endswith("/events/event/" + action)
        assert json.loads(request.content) == {"comment": "Thanks", "sendResponse": True}
        return httpx.Response(202)
    result = run(tools.RespondToMeetingTool, make_backend(handler), item_id="event", response=response, message="Thanks")
    assert result["success"] and result["response"] == response


def test_inbox_meeting_request_resolves_event_before_response(make_backend):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/events/invite/accept"):
            return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
        if request.url.path.endswith("/messages/invite/event"):
            return httpx.Response(200, json=event(id="linked-event"))
        assert request.url.path.endswith("/events/linked-event/accept")
        return httpx.Response(202)
    result = run(tools.RespondToMeetingTool, make_backend(handler), item_id="invite", response="Accept")
    assert result["success"] and result["event_id"] == "linked-event"


def test_missing_calendar_scope_is_actionable(make_backend):
    backend = make_backend(lambda request: httpx.Response(403, json={"error": {"code": "ErrorAccessDenied"}}))
    result = run(tools.GetCalendarTool, backend, start_date="2026-09-16", days_ahead=1)
    assert result["success"] is False
    assert result["required_scope"] == "Calendars.ReadWrite"
    assert result["scope_setting"] == "account.settings.graph_scope"
    assert result["scope_to_add"] == "https://graph.microsoft.com/Calendars.ReadWrite"


def test_personal_freebusy_is_explicitly_unsupported(make_backend):
    def handler(request):
        pytest.fail("Personal free/busy must not query other calendars")
    result = run(tools.CheckAvailabilityTool, make_backend(handler, tenant_id="consumers"),
                 email_addresses=["peer@example.com"], start_time="2026-09-16T09:00:00Z", end_time="2026-09-16T10:00:00Z")
    assert result["success"] is False
    assert "personal" in result["error"] and "getSchedule" in result["error"]


def test_availability_preserves_unknown_not_free(make_backend):
    def handler(request):
        assert request.url.path.endswith("/calendar/getSchedule")
        return httpx.Response(200, json={"value": [
            {"scheduleId": "owner@example.com", "availabilityView": "02", "scheduleItems": []},
            {"scheduleId": "peer@example.com", "error": {"message": "No access"}},
        ]})
    result = run(tools.CheckAvailabilityTool, make_backend(handler), email_addresses=["peer@example.com"],
                 start_time="2026-09-16T09:00:00Z", end_time="2026-09-16T10:00:00Z")
    assert result["checked_email_addresses"] == ["owner@example.com", "peer@example.com"]
    peer = result["availability"][1]
    assert peer["merged_free_busy"] == "44" and peer["error"] == "No access"


def test_suggestions_never_cross_busy_slot_or_closing_time(make_backend):
    def handler(request):
        return httpx.Response(200, json={"value": [{"scheduleId": "peer@example.com",
            "availabilityView": "020000", "scheduleItems": []}]})
    result = run(tools.FindMeetingTimesTool, make_backend(handler), attendees=["peer@example.com"],
                 date_range_start="2026-09-16T16:00:00Z", date_range_end="2026-09-16T17:30:00Z",
                 duration_minutes=20, preferences={"avoid_back_to_back": False})
    assert [(s["start_time"], s["end_time"]) for s in result["suggestions"]] == [
        ("2026-09-16T16:30:00+00:00", "2026-09-16T16:50:00+00:00")]


def test_missing_attendee_schedule_never_produces_suggestion(make_backend):
    backend = make_backend(lambda request: httpx.Response(200, json={"value": []}))
    result = run(tools.FindMeetingTimesTool, backend, attendees=["peer@example.com"],
                 date_range_start="2026-09-16T09:00:00Z", date_range_end="2026-09-16T10:00:00Z")
    assert result["suggestions"] == []
    assert result["availability_errors"] == [{"email": "peer@example.com", "error": "No schedule returned"}]


@pytest.mark.parametrize("tool_cls,args", [
    (tools.CreateAppointmentTool, {"subject": "Demo", "start_time": "2026-09-16T09:00:00+00:00", "end_time": "2026-09-16T10:00:00+00:00"}),
    (tools.GetCalendarTool, {}),
    (tools.UpdateAppointmentTool, {"item_id": "/cal/demo.ics", "subject": "Demo"}),
    (tools.DeleteAppointmentTool, {"item_id": "/cal/demo.ics"}),
    (tools.RespondToMeetingTool, {"item_id": "/cal/demo.ics", "response": "Accept"}),
    (tools.CheckAvailabilityTool, {"email_addresses": ["peer@example.com"], "start_time": "2026-09-16T09:00:00+00:00", "end_time": "2026-09-16T10:00:00+00:00"}),
    (tools.FindMeetingTimesTool, {"attendees": ["peer@example.com"], "date_range_start": "2026-09-16T09:00:00+00:00", "date_range_end": "2026-09-16T17:00:00+00:00"}),
])
def test_generic_imap_calendar_requires_caldav_configuration(tool_cls, args):
    from src.mail_backends.imap_backend import IMAPBackend
    account = MailAccount(email="owner@example.com", provider="imap", password="pw",
                          settings={"imap_host": "imap.example.com", "smtp_host": "smtp.example.com"})
    backend = IMAPBackend(account)
    backend._account = account
    try:
        result = run(tool_cls, backend, **args)
    finally:
        backend.close()
    assert result["success"] is False and ("CalDAV" in result["error"] or "unavailable for CalDAV" in result["error"])


def test_empty_update_does_not_rewrite_event(make_backend):
    def handler(request):
        pytest.fail("No fields means no provider mutation")
    result = run(tools.UpdateAppointmentTool, make_backend(handler), item_id="event")
    assert result["success"] and result["updated_fields"] == []


def test_all_day_partial_update_uses_requested_timezone_not_default_utc(make_backend, monkeypatch):
    monkeypatch.setenv("TIMEZONE", "Europe/Berlin")
    stored = event(isAllDay=True,
        start={"dateTime": "2026-09-16T00:00:00", "timeZone": "Europe/Berlin"},
        end={"dateTime": "2026-09-17T00:00:00", "timeZone": "Europe/Berlin"})
    def handler(request):
        if request.method == "PATCH":
            stored.update(json.loads(request.content))
            return httpx.Response(200, json=stored)
        if request.headers.get("Prefer") == 'outlook.timezone="Europe/Berlin"':
            return httpx.Response(200, json=stored)
        return httpx.Response(200, json=event(isAllDay=True,
            start={"dateTime": "2026-09-15T22:00:00", "timeZone": "UTC"},
            end={"dateTime": "2026-09-16T22:00:00", "timeZone": "UTC"}))
    result = run(tools.UpdateAppointmentTool, make_backend(handler), item_id="event",
                 end_time="2026-09-18")
    assert result["success"]
    assert stored["end"] == {"dateTime": "2026-09-18T00:00:00", "timeZone": "Europe/Berlin"}


def test_calendar_result_limit_stops_before_next_page(make_backend):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"value": [event(id="one"), event(id="two")],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendarView?$skiptoken=more"})
    result = run(tools.GetCalendarTool, make_backend(handler), start_date="2026-09-16T00:00:00Z",
                 end_date="2026-09-17T00:00:00Z", max_results=1)
    assert len(calls) == 1
    assert [row["item_id"] for row in result["events"]] == ["one"]
    assert result["end_date"] == "2026-09-17T00:00:00+00:00"


def test_calendar_paging_cannot_leak_token_to_other_origin(make_backend):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"value": [],
            "@odata.nextLink": "https://elsewhere.invalid/calendar"})
    result = run(tools.GetCalendarTool, make_backend(handler), start_date="2026-09-16", days_ahead=1)
    assert result["success"] is False
    assert "untrusted" in result["error"]
    assert len(calls) == 1


def test_calendar_attachment_rejection_precedes_creation(make_backend):
    def handler(request):
        pytest.fail("Unsupported files must not leave a partially-created event")
    result = run(tools.CreateAppointmentTool, make_backend(handler), subject="No partial event",
        start_time="2026-09-16T09:00:00Z", end_time="2026-09-16T10:00:00Z",
        inline_attachments=[{"filename": "file.txt", "content": "eA=="}])
    assert result["success"] is False and "Exchange-only" in result["error"]


@pytest.mark.parametrize("day,view_length,last_offset", [
    ("2026-10-25", 16, "+01:00"), ("2026-03-29", 8, "+02:00"),
])
def test_dst_availability_uses_elapsed_slots(make_backend, monkeypatch, day, view_length, last_offset):
    monkeypatch.setenv("TIMEZONE", "Europe/Berlin")
    backend = make_backend(lambda request: httpx.Response(200, json={"value": [{
        "scheduleId": "peer@example.com", "availabilityView": "0" * view_length,
    }]}))
    result = run(tools.CheckAvailabilityTool, backend, email_addresses=["peer@example.com"],
        include_self=False, interval_minutes=15, start_time=day + "T01:00:00", end_time=day + "T04:00:00")
    row = result["availability"][0]
    assert row["merged_free_busy"] == "0" * view_length
    assert row["slot_summaries"][-1]["end"] == day + "T04:00:00" + last_offset
    from datetime import datetime
    for slot in row["slot_summaries"]:
        assert (datetime.fromisoformat(slot["end"]) - datetime.fromisoformat(slot["start"])).total_seconds() == 900


@pytest.mark.parametrize("start_day,end_day,total_slots,free_index,offset", [
    ("2026-10-24", "2026-10-25", 76, 68, "+01:00"),
    ("2026-03-28", "2026-03-29", 68, 60, "+02:00"),
])
def test_dst_meeting_search_checks_correct_real_hour(make_backend, monkeypatch, start_day, end_day, total_slots, free_index, offset):
    monkeypatch.setenv("TIMEZONE", "Europe/Berlin")
    view = "2" * free_index + "0000" + "2" * (total_slots - free_index - 4)
    backend = make_backend(lambda request: httpx.Response(200, json={"value": [{
        "scheduleId": "peer@example.com", "availabilityView": view,
    }]}))
    result = run(tools.FindMeetingTimesTool, backend, attendees=["peer@example.com"],
        date_range_start=start_day + "T17:00:00", date_range_end=end_day + "T11:00:00",
        preferences={"avoid_back_to_back": False})
    assert [s["start_time"] for s in result["suggestions"]] == [end_day + "T09:00:00" + offset]


def test_find_meeting_times_surfaces_attendee_permission_errors(make_backend):
    backend = make_backend(lambda request: httpx.Response(200, json={"value": [{
        "scheduleId": "peer@example.com", "error": {"message": "No access to this schedule"},
    }]}))
    result = run(tools.FindMeetingTimesTool, backend, attendees=["peer@example.com"],
        date_range_start="2026-09-16T09:00:00Z", date_range_end="2026-09-16T10:00:00Z")
    assert result["suggestions"] == []
    assert result["availability_errors"] == [{"email": "peer@example.com", "error": "No access to this schedule"}]


def test_working_elsewhere_remains_available_with_official_zero_encoding(make_backend):
    backend = make_backend(lambda request: httpx.Response(200, json={"value": [{
        "scheduleId": "peer@example.com", "availabilityView": "00", "scheduleItems": [{
            "start": {"dateTime": "2026-09-16T09:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-09-16T10:00:00", "timeZone": "UTC"},
            "status": "workingElsewhere",
        }],
    }]}))
    result = run(tools.CheckAvailabilityTool, backend, email_addresses=["peer@example.com"],
        include_self=False, start_time="2026-09-16T09:00:00Z", end_time="2026-09-16T10:00:00Z")
    row = result["availability"][0]
    assert row["availability_summary"]["has_working_elsewhere"] is True
    assert row["blocking_slots"] == []


def test_meeting_duration_stays_elapsed_across_spring_jump(make_backend, monkeypatch):
    monkeypatch.setenv("TIMEZONE", "Europe/Berlin")
    backend = make_backend(lambda request: httpx.Response(200, json={"value": [{
        "scheduleId": "peer@example.com", "availabilityView": "0000",
    }]}))
    result = run(tools.FindMeetingTimesTool, backend, attendees=["peer@example.com"],
        date_range_start="2026-03-29T01:30:00", date_range_end="2026-03-29T03:30:00",
        preferences={"working_hours_only": False, "avoid_back_to_back": False})
    assert [(s["start_time"], s["end_time"]) for s in result["suggestions"]] == [
        ("2026-03-29T01:30:00+01:00", "2026-03-29T03:30:00+02:00")]
