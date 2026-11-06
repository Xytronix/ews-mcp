"""Recurring appointments, reminders and the series guard over Exchange.

Every test drives the real exchangelib services against a scripted EWS
server: the SOAP payloads asserted here are the ones exchangelib would POST
(CreateItem, GetItem, FindItem, UpdateItem, DeleteItem), and the responses
are parsed by exchangelib itself.
"""
from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest
from exchangelib.folders import Calendar, Root, SentItems
from exchangelib.services.common import EWSService
from lxml import etree

from src.exceptions import ValidationError
from src.tools import calendar_tools
from tests.support import offline_account

M = "http://schemas.microsoft.com/exchange/services/2006/messages"
T = "http://schemas.microsoft.com/exchange/services/2006/types"
NS = {"m": M, "t": T}
BERLIN_MS_ID = "W. Europe Standard Time"

WEEKLY_THURSDAY_X10 = (
    "<t:Recurrence><t:WeeklyRecurrence><t:Interval>1</t:Interval>"
    "<t:DaysOfWeek>Thursday</t:DaysOfWeek><t:FirstDayOfWeek>Monday</t:FirstDayOfWeek>"
    "</t:WeeklyRecurrence><t:NumberedRecurrence><t:StartDate>2026-03-26+01:00</t:StartDate>"
    "<t:NumberOfOccurrences>10</t:NumberOfOccurrences></t:NumberedRecurrence></t:Recurrence>"
)


def calendar_item(item_id, kind, *, start="2026-03-26T08:00:00Z", end="2026-03-26T08:30:00Z",
                  reminder=None, attendees=(), recurrence=""):
    reminder_xml = f"<t:ReminderIsSet>{'true' if reminder is not None else 'false'}</t:ReminderIsSet>"
    if reminder is not None:
        reminder_xml += f"<t:ReminderMinutesBeforeStart>{reminder}</t:ReminderMinutesBeforeStart>"
    attendees_xml = ""
    if attendees:
        attendees_xml = "<t:RequiredAttendees>" + "".join(
            f"<t:Attendee><t:Mailbox><t:EmailAddress>{address}</t:EmailAddress></t:Mailbox></t:Attendee>"
            for address in attendees
        ) + "</t:RequiredAttendees>"
    return (
        f'<t:CalendarItem><t:ItemId Id="{item_id}" ChangeKey="CK-{item_id}"/>'
        f"<t:Subject>Sync</t:Subject>{reminder_xml}{recurrence}"
        f"<t:Start>{start}</t:Start><t:End>{end}</t:End><t:IsAllDayEvent>false</t:IsAllDayEvent>"
        f"<t:CalendarItemType>{kind}</t:CalendarItemType>{attendees_xml}"
        f'<t:StartTimeZone Id="{BERLIN_MS_ID}"/><t:EndTimeZone Id="{BERLIN_MS_ID}"/></t:CalendarItem>'
    )


def response_message(service, body="", ok=True):
    status = "" if ok else (
        "<m:MessageText>The specified object was not found in the store.</m:MessageText>"
    )
    code = "NoError" if ok else "ErrorItemNotFound"
    return etree.fromstring(
        f'<m:{service}ResponseMessage xmlns:m="{M}" xmlns:t="{T}" '
        f'ResponseClass="{"Success" if ok else "Error"}">{status}'
        f"<m:ResponseCode>{code}</m:ResponseCode>{body}</m:{service}ResponseMessage>"
    )


class FakeEWS:
    """Answers exchangelib's SOAP calls from a small in-memory calendar."""

    def __init__(self):
        self.requests = []
        self.items = {}    # item id -> CalendarItem XML
        self.masters = {}  # occurrence id -> master id
        self.view = []     # CalendarItem XML returned by FindItem

    def add(self, item_id, kind, **fields):
        self.items[item_id] = calendar_item(item_id, kind, **fields)

    def respond(self, service, payload, **_):
        name = service.SERVICE_NAME
        xml = etree.fromstring(etree.tostring(payload))
        self.requests.append((name, xml))
        return getattr(self, "_" + name)(xml)

    def sent(self, name):
        return [xml for service, xml in self.requests if service == name]

    def _GetItem(self, xml):
        messages = []
        for ref in xml.find("m:ItemIds", NS):
            if ref.tag == f"{{{T}}}RecurringMasterItemId":
                item_id = self.masters.get(ref.get("OccurrenceId"))
            else:
                item_id = ref.get("Id")
            if item_id in self.items:
                messages.append(response_message("GetItem", f"<m:Items>{self.items[item_id]}</m:Items>"))
            else:
                messages.append(response_message("GetItem", "<m:Items/>", ok=False))
        return messages

    def _FindItem(self, xml):
        return [response_message("FindItem", (
            f'<m:RootFolder TotalItemsInView="{len(self.view)}" IncludesLastItemInRange="true">'
            f'<t:Items>{"".join(self.view)}</t:Items></m:RootFolder>'
        ))]

    def _CreateItem(self, xml):
        if xml.find(".//t:CancelCalendarItem", NS) is not None:
            return [response_message("CreateItem", "<m:Items/>")]
        return [response_message("CreateItem", '<m:Items><t:CalendarItem><t:ItemId Id="NEW" ChangeKey="CK-NEW"/></t:CalendarItem></m:Items>')]

    def _UpdateItem(self, xml):
        target = xml.find(".//t:ItemChange/t:ItemId", NS).get("Id")
        return [response_message("UpdateItem", (
            f'<m:Items><t:CalendarItem><t:ItemId Id="{target}" ChangeKey="CK2"/></t:CalendarItem></m:Items>'
            "<m:ConflictResults><t:Count>0</t:Count></m:ConflictResults>"
        ))]

    def _DeleteItem(self, xml):
        return [response_message("DeleteItem")]

    def _CreateAttachment(self, xml):
        parent = xml.find("m:ParentItemId", NS).get("Id")
        return [response_message("CreateAttachment", (
            f'<m:Attachments><t:FileAttachment><t:AttachmentId Id="ATT-{parent}" '
            f'RootItemId="{parent}" RootItemChangeKey="CK3"/></t:FileAttachment></m:Attachments>'
        ))]


@pytest.fixture(autouse=True)
def berlin(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "Europe/Berlin")


@pytest.fixture
def ews(monkeypatch):
    server = FakeEWS()
    monkeypatch.setattr(EWSService, "_get_response_xml", lambda service, payload, **kw: server.respond(service, payload))
    return server


@pytest.fixture
def client():
    account = offline_account()
    # Resolved folders, so no GetFolder round trip is needed. Cancellations
    # keep their copy in Sent Items.
    root = Root(account=account, id="ROOT", changekey="R")
    account.__dict__["root"] = root
    account.__dict__["calendar"] = Calendar(root=root, id="CAL", changekey="C")
    account.__dict__["sent"] = SentItems(root=root, id="SENT", changekey="S")
    return SimpleNamespace(
        account=account,
        config=SimpleNamespace(ews_email=account.primary_smtp_address),
        mailbox_registry=None,
        get_account=lambda target_mailbox=None: account,
    )


def run(tool_cls, client, **kwargs):
    return asyncio.run(tool_cls(client).execute(**kwargs))


def text(xml, path):
    node = xml.find(path, NS)
    return None if node is None else node.text


def is_true(value):
    return value in ("1", "true")


def created(ews):
    (payload,) = ews.sent("CreateItem")
    return payload.find(".//t:CalendarItem", NS)


# --------------------------------------------------------------- create
@pytest.mark.parametrize("start,end", [
    ("2026-03-26T09:00:00+01:00", "2026-03-26T09:30:00+01:00"),
    ("2026-03-26T08:00:00Z", "2026-03-26T08:30:00Z"),
    ("2026-03-26T09:00:00", "2026-03-26T09:30:00"),
], ids=["fixed-offset", "utc", "naive"])
def test_series_is_written_in_the_named_zone_across_dst(ews, client, start, end):
    # Berlin switches to summer time on 2026-03-29. A series pinned to the
    # fixed +01:00 offset (or to UTC) would then start at 10:00 local time;
    # a named StartTimeZone keeps every occurrence at 09:00.
    result = run(calendar_tools.CreateAppointmentTool, client, subject="Sync", start_time=start, end_time=end,
                 recurrence={"pattern": "weekly", "weekdays": ["thursday", "monday"], "until": "2026-04-30"})
    item = created(ews)
    assert text(item, "t:Start") == "2026-03-26T09:00:00+01:00"
    assert item.find("t:StartTimeZone", NS).get("Id") == BERLIN_MS_ID
    assert item.find("t:EndTimeZone", NS).get("Id") == BERLIN_MS_ID
    weekly = item.find("t:Recurrence/t:WeeklyRecurrence", NS)
    assert text(weekly, "t:DaysOfWeek") == "Monday Thursday"
    assert text(item, "t:Recurrence/t:EndDateRecurrence/t:StartDate") == "2026-03-26"
    assert text(item, "t:Recurrence/t:EndDateRecurrence/t:EndDate") == "2026-04-30"
    assert result["item_id"] == "NEW" and result["type"] == "series_master"
    assert result["recurrence"]["summary"] == "Every week on Monday and Thursday, from 2026-03-26 until 2026-04-30"


@pytest.mark.parametrize("start,recurrence,element,fields,boundary", [
    ("2026-03-26T09:00:00", {"pattern": "daily", "interval": 2, "count": 10},
     "DailyRecurrence", {"Interval": "2"}, ("NumberedRecurrence", {"NumberOfOccurrences": "10"})),
    ("2026-03-26T09:00:00", {"pattern": "weekly"},
     "WeeklyRecurrence", {"Interval": "1", "DaysOfWeek": "Thursday", "FirstDayOfWeek": "Monday"},
     ("NoEndRecurrence", {"StartDate": "2026-03-26"})),
    ("2026-03-26T09:00:00", {"pattern": "monthly", "interval": 3, "until": "2027-03-26"},
     "AbsoluteMonthlyRecurrence", {"Interval": "3", "DayOfMonth": "26"},
     ("EndDateRecurrence", {"EndDate": "2027-03-26"})),
    ("2026-03-10T09:00:00", {"pattern": "monthly", "week_of_month": "second", "weekday": "tuesday"},
     "RelativeMonthlyRecurrence", {"Interval": "1", "DaysOfWeek": "Tuesday", "DayOfWeekIndex": "Second"},
     ("NoEndRecurrence", {"StartDate": "2026-03-10"})),
    ("2026-03-31T09:00:00", {"pattern": "monthly", "week_of_month": "last", "count": 6},
     "RelativeMonthlyRecurrence", {"DaysOfWeek": "Tuesday", "DayOfWeekIndex": "Last"},
     ("NumberedRecurrence", {"NumberOfOccurrences": "6"})),
    ("2026-03-26T09:00:00", {"pattern": "yearly"},
     "AbsoluteYearlyRecurrence", {"DayOfMonth": "26", "Month": "March"},
     ("NoEndRecurrence", {"StartDate": "2026-03-26"})),
    ("2026-11-26T09:00:00", {"pattern": "yearly", "week_of_month": "fourth", "weekday": "thursday", "count": 3},
     "RelativeYearlyRecurrence", {"DaysOfWeek": "Thursday", "DayOfWeekIndex": "Fourth", "Month": "November"},
     ("NumberedRecurrence", {"NumberOfOccurrences": "3"})),
], ids=["daily-count", "weekly-noend", "monthly-day-until", "monthly-second-tuesday",
        "monthly-last-tuesday", "yearly-date", "yearly-fourth-thursday"])
def test_each_pattern_and_end_reaches_create_item(ews, client, start, recurrence, element, fields, boundary):
    end = start[:11] + "10:00:00"
    run(calendar_tools.CreateAppointmentTool, client, subject="Series", start_time=start, end_time=end,
        recurrence=recurrence)
    rule = created(ews).find("t:Recurrence", NS)
    pattern = rule.find(f"t:{element}", NS)
    assert pattern is not None, etree.tostring(rule)
    assert {name: text(pattern, f"t:{name}") for name in fields} == fields
    kind, values = boundary
    span = rule.find(f"t:{kind}", NS)
    assert span is not None, etree.tostring(rule)
    assert {name: text(span, f"t:{name}") for name in values} == values


@pytest.mark.parametrize("kwargs,is_set,minutes", [
    ({}, True, "15"),
    ({"reminder_minutes": 30}, True, "30"),
    ({"reminder_minutes": 0}, True, "0"),
    ({"reminder_minutes": None}, False, None),
], ids=["default-15", "thirty", "at-start", "null-none"])
def test_create_reminder_is_set_or_left_off(ews, client, kwargs, is_set, minutes):
    result = run(calendar_tools.CreateAppointmentTool, client, subject="Call",
                 start_time="2026-09-16T09:00:00", end_time="2026-09-16T10:00:00", **kwargs)
    item = created(ews)
    assert is_true(text(item, "t:ReminderIsSet")) is is_set
    if is_set:
        assert text(item, "t:ReminderMinutesBeforeStart") == minutes
    assert result["reminder_minutes"] == (int(minutes) if is_set else None)
    assert result["type"] == "single" and result["recurrence"] is None
    assert item.find("t:Recurrence", NS) is None


@pytest.mark.parametrize("recurrence,message", [
    ({"pattern": "hourly"}, "pattern must be one of"),
    ({"interval": 1}, "pattern must be one of"),
    ({"pattern": "weekly", "weekdays": ["monday"]}, "not in recurrence.weekdays"),
    ({"pattern": "weekly", "weekdays": ["someday"]}, "day name"),
    ({"pattern": "weekly", "weekdays": []}, "non-empty list"),
    ({"pattern": "daily", "weekdays": ["thursday"]}, "does not apply to a daily pattern"),
    ({"pattern": "weekly", "interval": 0}, "interval must be a whole number from 1 to 99"),
    ({"pattern": "daily", "interval": 1000}, "from 1 to 999"),
    ({"pattern": "weekly", "until": "2026-12-31", "count": 3}, "not both"),
    ({"pattern": "weekly", "until": "soon"}, "YYYY-MM-DD"),
    ({"pattern": "weekly", "until": "2026-01-01"}, "before the first occurrence"),
    ({"pattern": "weekly", "count": 0}, "count must be a whole number"),
    ({"pattern": "monthly", "day_of_month": 5}, "not recurrence.day_of_month 5"),
    ({"pattern": "monthly", "week_of_month": "first", "weekday": "thursday"}, "is not the first thursday"),
    ({"pattern": "monthly", "weekday": "thursday"}, "week_of_month must be"),
    ({"pattern": "monthly", "day_of_month": 26, "week_of_month": "last"}, "not both"),
    ({"pattern": "yearly", "month": 4}, "not recurrence.month 4"),
    ({"pattern": "yearly", "interval": 2}, "yearly recurrence has no interval"),
    ({"pattern": "weekly", "first_day_of_week": "sunday"}, "first_day_of_week is not supported"),
    ("weekly", "must be an object"),
])
def test_bad_recurrence_is_refused_before_anything_is_sent(ews, client, recurrence, message):
    # 2026-03-26 is the fourth and last Thursday of March.
    with pytest.raises(ValidationError, match=message):
        run(calendar_tools.CreateAppointmentTool, client, subject="Series",
            start_time="2026-03-26T09:00:00", end_time="2026-03-26T10:00:00", recurrence=recurrence)
    assert ews.requests == []


@pytest.mark.parametrize("value", [-1, "soon", 1.5])
def test_bad_reminder_is_refused(ews, client, value):
    with pytest.raises(ValidationError, match="reminder_minutes"):
        run(calendar_tools.CreateAppointmentTool, client, subject="Call",
            start_time="2026-09-16T09:00:00", end_time="2026-09-16T10:00:00", reminder_minutes=value)
    assert ews.requests == []


# ---------------------------------------------------------- get_calendar
def calendar_view(ews):
    ews.add("ONE", "Single", start="2026-03-25T08:00:00Z", end="2026-03-25T09:00:00Z")
    ews.add("OCC", "Occurrence", start="2026-04-02T07:00:00Z", end="2026-04-02T07:30:00Z", reminder=10)
    ews.add("EXC", "Exception", start="2026-04-09T09:00:00Z", end="2026-04-09T09:30:00Z", reminder=0)
    ews.view = [ews.items[item_id] for item_id in ("ONE", "OCC", "EXC")]
    ews.add("MASTER", "RecurringMaster", recurrence=WEEKLY_THURSDAY_X10)
    ews.masters.update(OCC="MASTER", EXC="MASTER")


def master_lookup(ews):
    """The GetItem that resolves masters, apart from the one loading the view's fields."""
    (lookup,) = [xml for xml in ews.sent("GetItem")
                 if xml.find("m:ItemIds/t:RecurringMasterItemId", NS) is not None]
    return lookup


def test_calendar_reports_type_reminder_and_the_series_master(ews, client):
    calendar_view(ews)
    result = run(calendar_tools.GetCalendarTool, client, start_date="2026-03-23", end_date="2026-04-12")
    rows = {row["item_id"]: row for row in result["events"]}
    assert [(rows[i]["type"], rows[i]["series_master_id"], rows[i]["reminder_minutes"]) for i in ("ONE", "OCC", "EXC")] == [
        ("single", None, None), ("occurrence", "MASTER", 10), ("exception", "MASTER", 0)]
    assert all("recurrence" not in row for row in result["events"])
    assert "warnings" not in result

    # The view's fields come from FindItem plus a GetItem for the complex ones.
    fields = {node.get("FieldURI") for xml in ews.sent("FindItem") + ews.sent("GetItem")[:1]
              for node in xml.iterfind(".//t:FieldURI", NS)}
    assert {"calendar:CalendarItemType", "item:ReminderIsSet", "item:ReminderMinutesBeforeStart"} <= fields
    # One GetItem resolves the masters of every occurrence on the page.
    lookup = master_lookup(ews)
    assert [ref.get("OccurrenceId") for ref in lookup.find("m:ItemIds", NS)] == ["OCC", "EXC"]
    assert {node.get("FieldURI") for node in lookup.iterfind(".//t:FieldURI", NS)} == set()


def test_calendar_adds_the_series_rule_on_request(ews, client):
    calendar_view(ews)
    result = run(calendar_tools.GetCalendarTool, client, start_date="2026-03-23", end_date="2026-04-12",
                 include_recurrence=True)
    rows = {row["item_id"]: row for row in result["events"]}
    rule = {"pattern": "weekly", "interval": 1, "weekdays": ["thursday"], "start_date": "2026-03-26",
            "count": 10, "summary": "Every week on Thursday, from 2026-03-26, 10 occurrences"}
    assert rows["OCC"]["recurrence"] == rule and rows["EXC"]["recurrence"] == rule
    assert rows["ONE"]["recurrence"] is None
    lookup = master_lookup(ews)
    assert {node.get("FieldURI") for node in lookup.iterfind(".//t:FieldURI", NS)} == {"calendar:Recurrence"}


def test_calendar_reports_a_master_it_could_not_load(ews, client):
    calendar_view(ews)
    del ews.masters["EXC"]
    result = run(calendar_tools.GetCalendarTool, client, start_date="2026-03-23", end_date="2026-04-12")
    rows = {row["item_id"]: row for row in result["events"]}
    assert rows["OCC"]["series_master_id"] == "MASTER"
    assert rows["EXC"]["series_master_id"] is None
    assert result["warnings"] == ["Could not load the series master of 1 recurring event(s)"]


# ------------------------------------------------------------ series guard
def series(ews, **occurrence):
    ews.add("MASTER", "RecurringMaster", recurrence=WEEKLY_THURSDAY_X10)
    ews.add("OCC", "Occurrence", start="2026-04-02T07:00:00Z", end="2026-04-02T07:30:00Z", **occurrence)
    ews.masters["OCC"] = "MASTER"


def item_change(ews):
    (update,) = ews.sent("UpdateItem")
    change = update.find(".//t:ItemChange", NS)
    fields = [node.get("FieldURI") for node in change.iterfind(".//t:SetItemField/t:FieldURI", NS)]
    return change.find("t:ItemId", NS).get("Id"), fields, change


@pytest.mark.parametrize("tool_cls", [calendar_tools.UpdateAppointmentTool, calendar_tools.DeleteAppointmentTool])
@pytest.mark.parametrize("scope", [None, "occurrence"])
def test_a_series_master_id_needs_scope_series(ews, client, tool_cls, scope):
    series(ews)
    with pytest.raises(ValidationError, match="whole recurring series; pass scope='series'"):
        run(tool_cls, client, item_id="MASTER", subject="Renamed", scope=scope)
    assert ews.sent("UpdateItem") == [] and ews.sent("DeleteItem") == [] and ews.sent("CreateItem") == []


def test_an_occurrence_update_targets_only_that_occurrence(ews, client):
    series(ews)
    result = run(calendar_tools.UpdateAppointmentTool, client, item_id="OCC", subject="Moved")
    target, fields, change = item_change(ews)
    assert target == "OCC" and fields == ["item:Subject"]
    assert text(change, ".//t:Subject") == "Moved"
    # The master was never looked up.
    assert all(ref.tag == f"{{{T}}}ItemId" for xml in ews.sent("GetItem") for ref in xml.find("m:ItemIds", NS))
    assert result["type"] == "occurrence" and "series_master_id" not in result


def test_scope_series_on_an_occurrence_updates_its_master(ews, client):
    series(ews)
    result = run(calendar_tools.UpdateAppointmentTool, client, item_id="OCC", subject="Renamed", scope="series")
    target, fields, _ = item_change(ews)
    assert target == "MASTER" and fields == ["item:Subject"]
    lookup = ews.sent("GetItem")[1]
    assert lookup.find("m:ItemIds/t:RecurringMasterItemId", NS).get("OccurrenceId") == "OCC"
    assert result["type"] == "series_master" and result["series_master_id"] == "MASTER"


def test_scope_series_on_a_master_changes_the_time_of_day(ews, client):
    series(ews)
    run(calendar_tools.UpdateAppointmentTool, client, item_id="MASTER", scope="series",
        start_time="2026-03-26T10:00:00", end_time="2026-03-26T10:45:00")
    target, fields, change = item_change(ews)
    assert target == "MASTER"
    assert set(fields) == {"calendar:Start", "calendar:End", "calendar:StartTimeZone", "calendar:EndTimeZone"}
    assert text(change, ".//t:Start") == "2026-03-26T10:00:00+01:00"
    assert change.find(".//t:StartTimeZone", NS).get("Id") == BERLIN_MS_ID


@pytest.mark.parametrize("item_id", ["MASTER", "OCC"])
def test_scope_series_cannot_move_the_series_to_another_date(ews, client, item_id):
    series(ews)
    with pytest.raises(ValidationError, match="must stay on the series' first date 2026-03-26"):
        run(calendar_tools.UpdateAppointmentTool, client, item_id=item_id, scope="series",
            start_time="2026-04-02T10:00:00")
    assert ews.sent("UpdateItem") == []


@pytest.mark.parametrize("tool_cls", [calendar_tools.UpdateAppointmentTool, calendar_tools.DeleteAppointmentTool])
def test_scope_series_on_a_single_appointment_is_refused(ews, client, tool_cls):
    ews.add("ONE", "Single")
    with pytest.raises(ValidationError, match="needs a recurring event"):
        run(tool_cls, client, item_id="ONE", subject="Renamed", scope="series")
    assert ews.sent("UpdateItem") == [] and ews.sent("DeleteItem") == []


def test_bad_scope_is_refused(ews, client):
    with pytest.raises(ValidationError, match="scope must be 'occurrence' or 'series'"):
        run(calendar_tools.UpdateAppointmentTool, client, item_id="OCC", subject="x", scope="all")
    assert ews.requests == []


@pytest.mark.parametrize("value,fields,is_set", [
    (5, ["item:ReminderIsSet", "item:ReminderMinutesBeforeStart"], True),
    (None, ["item:ReminderIsSet"], False),
], ids=["set", "clear"])
def test_update_sets_changes_and_clears_the_reminder(ews, client, value, fields, is_set):
    ews.add("ONE", "Single", reminder=15)
    result = run(calendar_tools.UpdateAppointmentTool, client, item_id="ONE", reminder_minutes=value)
    target, sent_fields, change = item_change(ews)
    assert target == "ONE" and sent_fields == fields
    assert is_true(text(change, ".//t:ReminderIsSet")) is is_set
    if is_set:
        assert text(change, ".//t:ReminderMinutesBeforeStart") == "5"
    assert result["updated_fields"] == ["reminder_is_set", "reminder_minutes_before_start"][: len(fields)]


@pytest.mark.parametrize("kwargs", [
    {},
    {"scope": "occurrence"},
    {"inline_attachments": []},
    {"inline_attachments": [{"file_name": "agenda.txt"}]},
], ids=["no-fields", "scope-only", "no-attachments", "unusable-attachment"])
def test_an_update_that_changes_nothing_is_refused_before_any_request(ews, client, kwargs):
    # save(update_fields=None) would push every writable field back to Exchange.
    ews.add("ONE", "Single")
    with pytest.raises(ValidationError, match="nothing to update"):
        run(calendar_tools.UpdateAppointmentTool, client, item_id="ONE", **kwargs)
    assert ews.requests == []


@pytest.mark.parametrize("attendees,note", [((), False), (("peer@example.com",), True)], ids=["single", "meeting"])
def test_an_attachment_only_update_never_rewrites_every_field(ews, client, attendees, note):
    ews.add("ONE", "Single", attendees=attendees)
    content = base64.b64encode(b"agenda").decode()
    result = run(calendar_tools.UpdateAppointmentTool, client, item_id="ONE",
                 inline_attachments=[{"file_name": "agenda.txt", "file_content": content}])
    (attach,) = ews.sent("CreateAttachment")
    assert attach.find("m:ParentItemId", NS).get("Id") == "ONE"
    assert text(attach, ".//t:FileAttachment/t:Name") == "agenda.txt"
    assert ews.sent("UpdateItem") == []
    assert result["updated_fields"] == [] and result["attachments_added"] == 1
    assert ("note" in result) is note


def test_deleting_an_occurrence_removes_only_that_occurrence(ews, client):
    series(ews)
    result = run(calendar_tools.DeleteAppointmentTool, client, item_id="OCC")
    (delete,) = ews.sent("DeleteItem")
    assert [ref.get("Id") for ref in delete.find("m:ItemIds", NS)] == ["OCC"]
    assert result["type"] == "occurrence" and result["cancellation_sent"] is False


def test_cancelling_a_meeting_occurrence_references_only_that_occurrence(ews, client):
    series(ews, attendees=["peer@example.com"])
    result = run(calendar_tools.DeleteAppointmentTool, client, item_id="OCC")
    (cancel,) = ews.sent("CreateItem")
    assert cancel.find(".//t:CancelCalendarItem/t:ReferenceItemId", NS).get("Id") == "OCC"
    assert ews.sent("DeleteItem") == [] and result["cancellation_sent"] is True


def test_scope_series_deletes_the_master_of_an_occurrence(ews, client):
    series(ews)
    result = run(calendar_tools.DeleteAppointmentTool, client, item_id="OCC", scope="series")
    (delete,) = ews.sent("DeleteItem")
    assert [ref.get("Id") for ref in delete.find("m:ItemIds", NS)] == ["MASTER"]
    assert result["series_master_id"] == "MASTER" and result["type"] == "series_master"


# ------------------------------------------------------------------ schema
def test_schema_documents_null_reminder_scope_and_recurrence():
    from jsonschema import Draft202012Validator

    from src.tools import graph_calendar_tools

    create = calendar_tools.CreateAppointmentTool(None).get_schema()["inputSchema"]
    Draft202012Validator.check_schema(create)
    validator = Draft202012Validator(create)
    base = {"subject": "Sync", "start_time": "2026-03-10T09:00:00", "end_time": "2026-03-10T10:00:00"}
    assert validator.is_valid({**base, "reminder_minutes": None})
    assert validator.is_valid({**base, "recurrence": {
        "pattern": "monthly", "week_of_month": "second", "weekday": "tuesday", "count": 5}})
    assert not validator.is_valid({**base, "recurrence": {"pattern": "weekly", "byday": "TU"}})
    assert not validator.is_valid({**base, "reminder_minutes": -1})

    update = calendar_tools.UpdateAppointmentTool(None).get_schema()["inputSchema"]["properties"]
    assert update["scope"]["enum"] == ["occurrence", "series"]
    assert update["reminder_minutes"]["type"] == ["integer", "null"]

    # The routed schema adds its CalDAV note to a copy, once per call.
    routed = graph_calendar_tools.CreateAppointmentTool(None)
    notes = [routed.get_schema()["inputSchema"]["properties"]["recurrence"]["description"] for _ in range(2)]
    assert notes[0] == notes[1] and notes[0].count("CalDAV") == 1
    assert "CalDAV" not in create["properties"]["recurrence"]["description"]
