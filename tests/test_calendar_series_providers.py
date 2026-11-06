"""Recurrence, reminders and the series guard on the Graph and CalDAV routes.

Graph payloads are captured from the HTTP requests the backend sends;
CalDAV resources are read back from the PUT bodies on a scripted server.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from icalendar import Calendar

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.exceptions import ValidationError
from src.mail_backends.accounts import MailAccount
from src.mail_backends.graph_backend import GraphBackend
from src.tools import graph_calendar_tools as tools
from tests.test_dav_calendar_contacts import DavServer, invoke, make_client


@pytest.fixture(autouse=True)
def berlin(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "Europe/Berlin")


class GraphServer:
    """Records every Graph request and answers from a small event table."""

    def __init__(self):
        self.requests = []
        self.events = {}
        self.view = []

    def handler(self, request):
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, dict(request.url.params), body))
        event_id = request.url.path.rsplit("/", 1)[-1]
        if request.method == "POST" and request.url.path.endswith("/me/events"):
            return httpx.Response(201, json={"id": "NEW", **body})
        if request.method == "GET" and request.url.path.endswith("/calendarView"):
            return httpx.Response(200, json={"value": self.view})
        if request.method == "GET":
            return httpx.Response(200, json={"id": event_id, **self.events[event_id]})
        if request.method == "PATCH":
            return httpx.Response(200, json={"id": event_id, **body})
        return httpx.Response(202 if request.url.path.endswith("/cancel") else 204)

    def mutations(self):
        return [(method, path, body) for method, path, _, body in self.requests if method != "GET"]


@pytest.fixture
def graph():
    server = GraphServer()
    account = MailAccount(email="owner@example.com", provider="outlook",
                          settings={"client_id": "cid", "refresh_token": "rt"})
    backend = GraphBackend(account)
    backend._access_token = "token"
    backend._token_expires_at = 1 << 40
    backend._client = httpx.Client(transport=httpx.MockTransport(server.handler))
    registry = SimpleNamespace(find_account=lambda address: account, get_backend=lambda address: backend)
    server.client = SimpleNamespace(mailbox_registry=registry, config=None)
    yield server
    backend.close()


def run(tool_cls, server, **kwargs):
    return asyncio.run(tool_cls(server.client).execute(**kwargs))


def graph_event(kind, start="2026-04-02T07:00:00", end="2026-04-02T07:30:00", **fields):
    return {"type": kind, "subject": "Sync", "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": end, "timeZone": "UTC"}, **fields}


def posted(server):
    (body,) = [body for method, _, _, body in server.requests if method == "POST"]
    return body


# ------------------------------------------------------------ Graph create
def test_graph_series_keeps_berlin_wall_clock_across_dst(graph):
    result = run(tools.CreateAppointmentTool, graph, subject="Sync",
                 start_time="2026-03-26T09:00:00+01:00", end_time="2026-03-26T09:30:00+01:00",
                 recurrence={"pattern": "weekly", "weekdays": ["thursday", "monday"], "until": "2026-04-30"})
    body = posted(graph)
    # Local wall-clock time in a named zone, not UTC: after the 2026-03-29
    # DST change the occurrences stay at 09:00 Berlin time.
    assert body["start"] == {"dateTime": "2026-03-26T09:00:00", "timeZone": "Europe/Berlin"}
    assert body["end"] == {"dateTime": "2026-03-26T09:30:00", "timeZone": "Europe/Berlin"}
    assert body["recurrence"] == {
        "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["monday", "thursday"], "firstDayOfWeek": "monday"},
        "range": {"type": "endDate", "startDate": "2026-03-26", "endDate": "2026-04-30",
                  "recurrenceTimeZone": "Europe/Berlin"},
    }
    assert result["type"] == "series_master" and result["start_time"] == "2026-03-26T09:00:00+01:00"
    assert result["recurrence"]["summary"] == "Every week on Monday and Thursday, from 2026-03-26 until 2026-04-30"


@pytest.mark.parametrize("start,recurrence,pattern,span", [
    ("2026-03-26T09:00:00", {"pattern": "daily", "interval": 2, "count": 10},
     {"type": "daily", "interval": 2}, {"type": "numbered", "numberOfOccurrences": 10}),
    ("2026-03-26T09:00:00", {"pattern": "monthly"},
     {"type": "absoluteMonthly", "interval": 1, "dayOfMonth": 26}, {"type": "noEnd"}),
    ("2026-03-10T09:00:00", {"pattern": "monthly", "week_of_month": "second", "weekday": "tuesday"},
     {"type": "relativeMonthly", "interval": 1, "daysOfWeek": ["tuesday"], "index": "second"}, {"type": "noEnd"}),
    ("2026-03-26T09:00:00", {"pattern": "yearly", "interval": 2, "until": "2032-03-26"},
     {"type": "absoluteYearly", "interval": 2, "dayOfMonth": 26, "month": 3},
     {"type": "endDate", "endDate": "2032-03-26"}),
    ("2026-11-26T09:00:00", {"pattern": "yearly", "week_of_month": "last", "weekday": "thursday"},
     {"type": "relativeYearly", "interval": 1, "daysOfWeek": ["thursday"], "index": "last", "month": 11},
     {"type": "noEnd"}),
], ids=["daily-count", "monthly-day", "monthly-second-tuesday", "yearly-every-2", "yearly-last-thursday"])
def test_graph_series_patterns_and_ends(graph, start, recurrence, pattern, span):
    run(tools.CreateAppointmentTool, graph, subject="Series", start_time=start,
        end_time=start[:11] + "10:00:00", recurrence=recurrence)
    sent = posted(graph)["recurrence"]
    assert sent["pattern"] == pattern
    assert {key: sent["range"][key] for key in span} == span
    assert sent["range"]["startDate"] == start[:10]


@pytest.mark.parametrize("kwargs,expected", [
    ({}, {"isReminderOn": True, "reminderMinutesBeforeStart": 15}),
    ({"reminder_minutes": 45}, {"isReminderOn": True, "reminderMinutesBeforeStart": 45}),
    ({"reminder_minutes": None}, {"isReminderOn": False}),
], ids=["default", "set", "null-none"])
def test_graph_create_reminder(graph, kwargs, expected):
    result = run(tools.CreateAppointmentTool, graph, subject="Call",
                 start_time="2026-09-16T09:00:00Z", end_time="2026-09-16T10:00:00Z", **kwargs)
    body = posted(graph)
    assert {key: body.get(key) for key in ("isReminderOn", "reminderMinutesBeforeStart") if key in body} == expected
    assert result["reminder_minutes"] == expected.get("reminderMinutesBeforeStart")
    assert "recurrence" not in body and body["start"]["timeZone"] == "UTC"


@pytest.mark.parametrize("kwargs,message", [
    ({"recurrence": {"pattern": "weekly", "weekdays": ["monday"]}}, "not in recurrence.weekdays"),
    ({"recurrence": {"pattern": "fortnightly"}}, "pattern must be one of"),
    ({"reminder_minutes": -5}, "reminder_minutes"),
])
def test_graph_bad_input_fails_before_any_request(graph, kwargs, message):
    with pytest.raises(ValidationError, match=message):
        run(tools.CreateAppointmentTool, graph, subject="Sync",
            start_time="2026-03-26T09:00:00", end_time="2026-03-26T10:00:00", **kwargs)
    assert graph.requests == []


# ------------------------------------------------------- Graph get_calendar
def graph_view(graph):
    graph.view = [
        {"id": "ONE", **graph_event("singleInstance", "2026-03-25T08:00:00", "2026-03-25T09:00:00"), "isReminderOn": False,
         "reminderMinutesBeforeStart": 15},
        {"id": "OCC", **graph_event("occurrence"), "seriesMasterId": "MASTER", "isReminderOn": True,
         "reminderMinutesBeforeStart": 10},
        {"id": "EXC", **graph_event("exception", "2026-04-09T09:00:00", "2026-04-09T09:30:00"),
         "seriesMasterId": "MASTER", "isReminderOn": True, "reminderMinutesBeforeStart": 0},
    ]
    graph.events["MASTER"] = {"recurrence": {
        "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["thursday"], "firstDayOfWeek": "monday"},
        "range": {"type": "numbered", "startDate": "2026-03-26", "numberOfOccurrences": 10,
                  "recurrenceTimeZone": "Europe/Berlin"},
    }}


def test_graph_calendar_reports_type_reminder_and_master(graph):
    graph_view(graph)
    result = run(tools.GetCalendarTool, graph, start_date="2026-03-23", end_date="2026-04-12")
    rows = {row["item_id"]: row for row in result["events"]}
    assert [(rows[i]["type"], rows[i]["series_master_id"], rows[i]["reminder_minutes"]) for i in ("ONE", "OCC", "EXC")] == [
        ("single", None, None), ("occurrence", "MASTER", 10), ("exception", "MASTER", 0)]
    (_, _, params, _), = graph.requests
    assert {"type", "seriesMasterId", "isReminderOn", "reminderMinutesBeforeStart"} <= set(params["$select"].split(","))
    assert all("recurrence" not in row for row in result["events"])


def test_graph_calendar_reads_the_rule_once_per_series_on_request(graph):
    graph_view(graph)
    result = run(tools.GetCalendarTool, graph, start_date="2026-03-23", end_date="2026-04-12", include_recurrence=True)
    rows = {row["item_id"]: row for row in result["events"]}
    assert rows["OCC"]["recurrence"] == rows["EXC"]["recurrence"] == {
        "pattern": "weekly", "interval": 1, "weekdays": ["thursday"], "start_date": "2026-03-26", "count": 10,
        "summary": "Every week on Thursday, from 2026-03-26, 10 occurrences"}
    assert rows["ONE"]["recurrence"] is None
    lookups = [(path, params) for method, path, params, _ in graph.requests if path.endswith("/events/MASTER")]
    assert lookups == [("/v1.0/me/events/MASTER", {"$select": "recurrence"})]


# ------------------------------------------------------- Graph series guard
def graph_series(graph, **occurrence):
    graph.events["MASTER"] = graph_event("seriesMaster", "2026-03-26T08:00:00", "2026-03-26T08:30:00",
                                         isOrganizer=True, attendees=[])
    graph.events["OCC"] = graph_event("occurrence", seriesMasterId="MASTER", isOrganizer=True, attendees=[], **occurrence)


@pytest.mark.parametrize("tool_cls,extra", [
    (tools.UpdateAppointmentTool, {"subject": "Renamed"}),
    (tools.DeleteAppointmentTool, {}),
])
def test_graph_master_id_needs_scope_series(graph, tool_cls, extra):
    graph_series(graph)
    with pytest.raises(ValidationError, match="whole recurring series"):
        run(tool_cls, graph, item_id="MASTER", **extra)
    assert graph.mutations() == []


def test_graph_occurrence_update_patches_only_that_occurrence(graph):
    graph_series(graph)
    result = run(tools.UpdateAppointmentTool, graph, item_id="OCC", subject="Moved")
    assert graph.mutations() == [("PATCH", "/v1.0/me/events/OCC", {"subject": "Moved"})]
    assert result["type"] == "occurrence"


def test_graph_scope_series_patches_the_master_in_local_time(graph):
    graph_series(graph)
    result = run(tools.UpdateAppointmentTool, graph, item_id="OCC", scope="series",
                 start_time="2026-03-26T10:00:00", end_time="2026-03-26T10:30:00")
    assert graph.mutations() == [("PATCH", "/v1.0/me/events/MASTER", {
        "start": {"dateTime": "2026-03-26T10:00:00", "timeZone": "Europe/Berlin"},
        "end": {"dateTime": "2026-03-26T10:30:00", "timeZone": "Europe/Berlin"},
    })]
    assert result["series_master_id"] == "MASTER" and result["type"] == "series_master"


def test_graph_scope_series_cannot_move_to_another_date(graph):
    graph_series(graph)
    with pytest.raises(ValidationError, match="first date 2026-03-26"):
        run(tools.UpdateAppointmentTool, graph, item_id="MASTER", scope="series", start_time="2026-04-02T10:00:00")
    assert graph.mutations() == []


@pytest.mark.parametrize("value,payload", [
    (5, {"isReminderOn": True, "reminderMinutesBeforeStart": 5}),
    (None, {"isReminderOn": False}),
], ids=["set", "clear"])
def test_graph_update_sets_and_clears_the_reminder(graph, value, payload):
    graph.events["ONE"] = graph_event("singleInstance")
    result = run(tools.UpdateAppointmentTool, graph, item_id="ONE", reminder_minutes=value)
    assert graph.mutations() == [("PATCH", "/v1.0/me/events/ONE", payload)]
    assert result["updated_fields"] == list(payload)


@pytest.mark.parametrize("item_id,scope,deleted", [
    ("OCC", None, "/v1.0/me/events/OCC"),
    ("OCC", "series", "/v1.0/me/events/MASTER"),
    ("MASTER", "series", "/v1.0/me/events/MASTER"),
])
def test_graph_delete_targets_the_occurrence_or_the_series(graph, item_id, scope, deleted):
    graph_series(graph)
    run(tools.DeleteAppointmentTool, graph, item_id=item_id, scope=scope)
    assert graph.mutations() == [("DELETE", deleted, None)]


# ------------------------------------------------------------------ CalDAV
def vevent(server, path):
    return next(iter(Calendar.from_ical(server.events[path][0]).walk("VEVENT")))


def alarms(event):
    return [alarm.decoded("TRIGGER") for alarm in event.walk("VALARM")]


def test_caldav_refuses_recurrence_before_writing():
    server = DavServer()
    client, backend = make_client(server)
    try:
        with pytest.raises(ValidationError, match="recurrence is not supported for CalDAV"):
            invoke(tools.CreateAppointmentTool, client, subject="Series", start_time="2026-03-26T09:00:00+01:00",
                   end_time="2026-03-26T10:00:00+01:00", recurrence={"pattern": "weekly"})
    finally:
        backend.close()
    assert [request for request in server.requests if request[0] == "PUT"] == []


@pytest.mark.parametrize("kwargs,triggers,minutes", [
    ({}, [-15], 15),
    ({"reminder_minutes": 30}, [-30], 30),
    ({"reminder_minutes": None}, [], None),
], ids=["default", "set", "null-none"])
def test_caldav_create_writes_the_reminder_as_valarm(kwargs, triggers, minutes):
    server = DavServer()
    client, backend = make_client(server)
    try:
        created = invoke(tools.CreateAppointmentTool, client, subject="Call", start_time="2026-09-17T09:00:00+00:00",
                         end_time="2026-09-17T10:00:00+00:00", **kwargs)
        event = vevent(server, created["item_id"])
        assert [int(t.total_seconds() // 60) for t in alarms(event)] == triggers
        assert created["reminder_minutes"] == minutes
        listed = invoke(tools.GetCalendarTool, client, start_date="2026-09-17T00:00:00+00:00",
                        end_date="2026-09-18T00:00:00+00:00")
        assert [(row["type"], row["reminder_minutes"]) for row in listed["events"]] == [("single", minutes)]
    finally:
        backend.close()


@pytest.mark.parametrize("value,triggers", [(5, [-5]), (None, [])], ids=["change", "clear"])
def test_caldav_update_changes_the_reminder_and_keeps_the_rest(value, triggers):
    server = DavServer()
    client, backend = make_client(server)
    try:
        created = invoke(tools.CreateAppointmentTool, client, subject="Call", start_time="2026-09-17T09:00:00+00:00",
                         end_time="2026-09-17T10:00:00+00:00", attendees=["peer@example.com"])
        updated = invoke(tools.UpdateAppointmentTool, client, item_id=created["item_id"], reminder_minutes=value)
        assert updated["updated_fields"] == ["reminder_minutes"]
        event = vevent(server, created["item_id"])
        assert [int(t.total_seconds() // 60) for t in alarms(event)] == triggers
        assert str(event["SUMMARY"]) == "Call"
        assert str(event["ATTENDEE"]) == "MAILTO:peer@example.com"
    finally:
        backend.close()


def test_caldav_update_without_reminder_keeps_alarm_and_attendees():
    server = DavServer()
    client, backend = make_client(server)
    try:
        created = invoke(tools.CreateAppointmentTool, client, subject="Call", start_time="2026-09-17T09:00:00+00:00",
                         end_time="2026-09-17T10:00:00+00:00", attendees=["peer@example.com"], reminder_minutes=20)
        invoke(tools.UpdateAppointmentTool, client, item_id=created["item_id"], subject="Renamed")
        event = vevent(server, created["item_id"])
        assert str(event["SUMMARY"]) == "Renamed"
        assert str(event.get("ATTENDEE")) == "MAILTO:peer@example.com"
        assert [int(t.total_seconds() // 60) for t in alarms(event)] == [-20]
    finally:
        backend.close()


SERIES_ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Example//EN\r\nBEGIN:VEVENT\r\nUID:series-1\r\n"
    "DTSTAMP:20260301T000000Z\r\nDTSTART:20260326T080000Z\r\nDTEND:20260326T083000Z\r\n"
    "SUMMARY:Standup\r\nRRULE:FREQ=WEEKLY;BYDAY=TH;COUNT=10\r\n"
    "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:Reminder\r\nTRIGGER:-PT10M\r\nEND:VALARM\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)
SERIES_HREF = "/u/calendars/home/series-1.ics"


def dav_series():
    server = DavServer()
    client, backend = make_client(server)
    server.events[SERIES_HREF] = (SERIES_ICS, '"s1"')
    return server, client, backend


def test_caldav_lists_a_series_as_its_master_with_rule_and_reminder():
    server, client, backend = dav_series()
    try:
        listed = invoke(tools.GetCalendarTool, client, start_date="2026-04-01T00:00:00+00:00",
                        end_date="2026-04-08T00:00:00+00:00")
    finally:
        backend.close()
    (row,) = listed["events"]
    assert (row["type"], row["reminder_minutes"], row["series_master_id"]) == ("series_master", 10, None)
    assert row["recurrence"] == {
        "pattern": "weekly", "interval": 1, "weekdays": ["thursday"], "start_date": "2026-03-26", "count": 10,
        "rrule": "FREQ=WEEKLY;COUNT=10;BYDAY=TH",
        "summary": "Every week on Thursday, from 2026-03-26, 10 occurrences"}


@pytest.mark.parametrize("tool_cls,extra", [
    (tools.UpdateAppointmentTool, {"subject": "Renamed"}),
    (tools.DeleteAppointmentTool, {}),
])
def test_caldav_series_resource_needs_scope_series(tool_cls, extra):
    server, client, backend = dav_series()
    try:
        with pytest.raises(ValidationError, match="whole recurring series"):
            invoke(tool_cls, client, item_id=SERIES_HREF, **extra)
    finally:
        backend.close()
    assert [request[0] for request in server.requests if request[0] in ("PUT", "DELETE")] == []
    assert server.events[SERIES_HREF][0] == SERIES_ICS


def test_caldav_scope_series_edits_the_master_and_keeps_its_rule():
    server, client, backend = dav_series()
    try:
        result = invoke(tools.UpdateAppointmentTool, client, item_id=SERIES_HREF, subject="Renamed",
                        reminder_minutes=None, scope="series")
    finally:
        backend.close()
    event = vevent(server, SERIES_HREF)
    assert str(event["SUMMARY"]) == "Renamed" and alarms(event) == []
    assert event["RRULE"].to_ical() == b"FREQ=WEEKLY;COUNT=10;BYDAY=TH"
    assert result["type"] == "series_master"


def test_caldav_series_time_change_is_refused():
    server, client, backend = dav_series()
    try:
        with pytest.raises(ValidationError, match="changing the time of a CalDAV series is not supported"):
            invoke(tools.UpdateAppointmentTool, client, item_id=SERIES_HREF, scope="series",
                   start_time="2026-03-26T09:00:00+00:00")
    finally:
        backend.close()
    assert server.events[SERIES_HREF][0] == SERIES_ICS


def test_caldav_scope_series_deletes_the_series_resource():
    server, client, backend = dav_series()
    try:
        result = invoke(tools.DeleteAppointmentTool, client, item_id=SERIES_HREF, scope="series")
    finally:
        backend.close()
    assert SERIES_HREF not in server.events and result["type"] == "series_master"
