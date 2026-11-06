"""check_availability reads naive free/busy times in the request's zone.

exchangelib asks GetUserAvailability for the zone of the ``start`` datetime
and EWS writes CalendarEvent times in that zone without an offset, so
exchangelib hands them back naive. Reading them as UTC shifted every busy
block by the TIMEZONE offset.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from exchangelib.properties import FreeBusyView
from lxml import etree

from src.tools.calendar_tools import CheckAvailabilityTool

ADDRESS = "user@example.com"
PEER = "peer@example.com"

# A FreeBusyView as EWS returns it: event times carry no offset.
FREE_BUSY_VIEW = b"""\
<m:FreeBusyView xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages"
                xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types">
  <t:FreeBusyViewType>DetailedMerged</t:FreeBusyViewType>
  <t:MergedFreeBusy>00220000</t:MergedFreeBusy>
  <t:CalendarEventArray>
    <t:CalendarEvent>
      <t:StartTime>2026-09-15T10:00:00</t:StartTime>
      <t:EndTime>2026-09-15T11:00:00</t:EndTime>
      <t:BusyType>Busy</t:BusyType>
    </t:CalendarEvent>
  </t:CalendarEventArray>
</m:FreeBusyView>
"""


def check_availability(monkeypatch, tz_name):
    monkeypatch.setenv("TIMEZONE", tz_name)
    requested_starts = []

    def get_free_busy_info(accounts, start, end, merged_free_busy_interval):
        requested_starts.append(start)
        view = FreeBusyView.from_xml(etree.fromstring(FREE_BUSY_VIEW), account=None)
        return iter([view])

    account = SimpleNamespace(
        primary_smtp_address=ADDRESS,
        protocol=SimpleNamespace(get_free_busy_info=get_free_busy_info),
    )
    ews_client = SimpleNamespace(
        config=SimpleNamespace(ews_email=ADDRESS),
        mailbox_registry=None,
        get_account=lambda target=None: account,
    )
    result = asyncio.run(CheckAvailabilityTool(ews_client).execute(
        email_addresses=[PEER],
        start_time="2026-09-15T08:00:00",
        end_time="2026-09-15T12:00:00",
        include_self=False,
    ))
    (start,) = requested_starts
    return start, result["availability"][0]["calendar_events"]


def test_naive_event_times_are_read_in_the_request_zone(monkeypatch):
    start, events = check_availability(monkeypatch, "Europe/Berlin")
    # exchangelib builds the request TimeZone from start.tzinfo.
    assert start.tzinfo.key == "Europe/Berlin"
    assert [(e["start"], e["end"]) for e in events] == [
        ("2026-09-15T10:00:00+02:00", "2026-09-15T11:00:00+02:00"),
    ]


def test_utc_output_is_unchanged(monkeypatch):
    start, events = check_availability(monkeypatch, "UTC")
    assert start.tzinfo.key == "UTC"
    assert events == [{
        "start": "2026-09-15T10:00:00+00:00",
        "end": "2026-09-15T11:00:00+00:00",
        "busy_type": "Busy",
        "details": None,
    }]
