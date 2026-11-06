"""Provider-unified calendar names with unchanged Exchange fallback."""
from __future__ import annotations

from datetime import timezone

from ..exceptions import ValidationError
from ..mail_backends.base import MailBackendError
from ..mail_backends.calendar_rules import CalendarRuleError
from . import calendar_tools as exchange
from .mailbox_tools import RoutedMailTool
from ..mail_backends.graph_calendar import _datetime


def _require_calendar(account):
    if account.kind not in {"graph", "imap", "jmap"}:
        raise MailBackendError("Calendar operations require Exchange, Graph, or a CalDAV-capable IMAP/JMAP mailbox")


def _require_graph(account, operation):
    if account.kind != "graph":
        raise MailBackendError(f"{operation} is unavailable for CalDAV/iCloud calendars; this backend supports event CRUD only")


def _reject_attachments(kwargs):
    if kwargs.get("inline_attachments"):
        raise MailBackendError("Calendar inline_attachments are Exchange-only; Graph calendar attachment upload is not supported by this tool")


async def _call(tool, func, **kwargs):
    """Run a calendar backend call; bad recurrence/reminder/scope input is a ValidationError."""
    try:
        return await tool.call(func, **kwargs)
    except CalendarRuleError as exc:
        raise ValidationError(str(exc)) from exc


def _note(schema, name, text):
    prop = schema["inputSchema"]["properties"][name]
    prop["description"] = prop.get("description", "") + text
    return schema


class CreateAppointmentTool(RoutedMailTool, exchange.CreateAppointmentTool):
    def get_schema(self):
        return _note(super().get_schema(), "recurrence", " CalDAV/iCloud mailboxes reject recurrence.")

    async def execute_routed(self, account, backend, **kwargs):
        _require_calendar(account)
        _reject_attachments(kwargs)
        result = await _call(self, backend.create_appointment, **{k: v for k, v in kwargs.items() if k != "inline_attachments"})
        return self.routed_response("Appointment created successfully", account, result)


class GetCalendarTool(RoutedMailTool, exchange.GetCalendarTool):
    async def execute_routed(self, account, backend, **kwargs):
        _require_calendar(account)
        result = await _call(self, backend.get_calendar, **kwargs)
        return self.routed_response(f"Retrieved {len(result['events'])} events", account, result)


class UpdateAppointmentTool(RoutedMailTool, exchange.UpdateAppointmentTool):
    def get_schema(self):
        return _note(super().get_schema(), "scope", (
            " CalDAV/iCloud: a recurring event is one resource for the whole series, so its "
            "item_id needs scope='series', and its start/end cannot be changed."
        ))

    async def execute_routed(self, account, backend, **kwargs):
        _require_calendar(account)
        _reject_attachments(kwargs)
        result = await _call(self, backend.update_appointment, **{k: v for k, v in kwargs.items() if k != "inline_attachments"})
        return self.routed_response("Appointment updated successfully", account, result)


class DeleteAppointmentTool(RoutedMailTool, exchange.DeleteAppointmentTool):
    def get_schema(self):
        schema = super().get_schema()
        schema["inputSchema"]["properties"]["send_cancellation"]["description"] += (
            ". Graph cannot suppress cancellation for organizer meetings; false is rejected in that case. "
            "Deleting an attendee's copy does not cancel the meeting."
        )
        return _note(schema, "scope", (
            " CalDAV/iCloud: a recurring event is one resource for the whole series, so its "
            "item_id needs scope='series'."
        ))

    async def execute_routed(self, account, backend, **kwargs):
        _require_calendar(account)
        result = await _call(self, backend.delete_appointment, **kwargs)
        return self.routed_response("Appointment deleted successfully", account, result)


class RespondToMeetingTool(RoutedMailTool, exchange.RespondToMeetingTool):
    async def execute_routed(self, account, backend, **kwargs):
        _require_calendar(account)
        result = await self.call(backend.respond_to_meeting, **kwargs)
        action = {"Accept": "accepted", "Tentative": "tentatively accepted", "Decline": "declined"}[result["response"]]
        return self.routed_response(f"Meeting {action}", account, result)


class CheckAvailabilityTool(RoutedMailTool, exchange.CheckAvailabilityTool):
    def get_schema(self):
        schema = super().get_schema()
        schema["description"] += " Graph requires a work/school Microsoft 365 mailbox; personal accounts do not support getSchedule."
        return schema

    async def execute_routed(self, account, backend, **kwargs):
        _require_graph(account, "check_availability")
        result = await self.call(backend.check_availability, **kwargs)
        start = _datetime(kwargs["start_time"])
        end = _datetime(kwargs["end_time"])
        for row in result["availability"]:
            slots = exchange.build_slot_summaries(start.astimezone(timezone.utc), kwargs.get("interval_minutes", 30), row["merged_free_busy"])
            for slot in slots:
                slot["start"] = _datetime(slot["start"]).astimezone(start.tzinfo).isoformat()
                slot["end"] = min(_datetime(slot["end"]), end.astimezone(timezone.utc)).astimezone(start.tzinfo).isoformat()
            summary = exchange.summarize_availability(row["merged_free_busy"], row["calendar_events"])
            # The EWS helper inspects object attributes, not Graph's dict rows.
            summary["has_working_elsewhere"] = any(
                event["busy_type"] == "WorkingElsewhere" for event in row["calendar_events"]
            )
            if summary["has_working_elsewhere"] and summary["primary_status"] in {"free", "tentative"}:
                summary["primary_status"] = "working_elsewhere"
            row.update(free_busy_legend=exchange.FREE_BUSY_LABELS, slot_summaries=slots,
                blocking_slots=[s for s in slots if s["is_blocking"]],
                non_free_slots=[s for s in slots if s["code"] != "0"],
                availability_summary=summary)
        return self.routed_response(f"Availability retrieved for {len(result['availability'])} user(s)", account, result)


class FindMeetingTimesTool(RoutedMailTool, exchange.FindMeetingTimesTool):
    def get_schema(self):
        schema = super().get_schema()
        schema["description"] += " Uses deterministic free/busy scoring, not AI. Graph requires work/school getSchedule; personal accounts are unsupported."
        return schema

    async def execute_routed(self, account, backend, **kwargs):
        _require_graph(account, "find_meeting_times")
        result = await self.call(backend.find_meeting_times, **kwargs)
        return self.routed_response(f"Found {len(result['suggestions'])} optimal meeting times", account, result)
