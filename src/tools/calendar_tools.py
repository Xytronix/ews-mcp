"""Calendar operation tools for EWS MCP Server."""

import logging
from typing import Any, Dict, List, Optional, Tuple
from datetime import date, datetime, timedelta
from exchangelib import CalendarItem, EWSDate, Mailbox, Attendee
from exchangelib.fields import MONTHS, WEEK_NUMBERS, WEEKDAYS as EWS_WEEKDAYS
from exchangelib.items import SEND_TO_ALL_AND_SAVE_COPY, SEND_TO_NONE
from exchangelib.items.calendar_item import EXCEPTION as EWS_EXCEPTION
from exchangelib.items.calendar_item import OCCURRENCE as EWS_OCCURRENCE
from exchangelib.items.calendar_item import RECURRING_MASTER, SINGLE as EWS_SINGLE
from exchangelib.properties import RecurringMasterItemId
from exchangelib.recurrence import (
    AbsoluteMonthlyPattern,
    AbsoluteYearlyPattern,
    DailyPattern,
    EndDatePattern,
    NumberedPattern,
    Recurrence,
    RelativeMonthlyPattern,
    RelativeYearlyPattern,
    WeeklyPattern,
)

from .base import BaseTool
from ..mail_backends.calendar_rules import (
    EXCEPTION,
    OCCURRENCE,
    PATTERNS,
    SERIES_MASTER,
    SINGLE,
    WEEK_OF_MONTH,
    WEEKDAYS,
    CalendarRuleError,
    describe_recurrence,
    normalize_recurrence,
    normalize_reminder,
    normalize_scope,
    require_series_first_date,
    series_target,
)
from ..models import CreateAppointmentRequest, MeetingResponse
from ..exceptions import ToolExecutionError, ValidationError
from ..utils import format_success_response, safe_get, parse_datetime_tz_aware, make_tz_aware, format_datetime, ews_id_to_str, attach_inline_files, find_message_for_account, INLINE_ATTACHMENTS_SCHEMA, get_timezone as get_configured_timezone


# ---------------------------------------------------------------- series
# Input schemas shared by the Exchange tools and their Graph/CalDAV routes.
RECURRENCE_SCHEMA = {
    "type": "object",
    "description": (
        "Repeat the appointment. start_time/end_time are the first occurrence; the "
        "series keeps that wall-clock time across DST changes in the configured "
        "TIMEZONE. Day fields default to start_time and must agree with it. "
        "Without until or count the series has no end."
    ),
    "properties": {
        "pattern": {"type": "string", "enum": list(PATTERNS)},
        "interval": {
            "type": "integer", "minimum": 1, "default": 1,
            "description": (
                "Every N days (up to 999), weeks or months (up to 99). Exchange "
                "yearly patterns only support 1; use monthly with 12*N instead."
            ),
        },
        "weekdays": {
            "type": "array", "items": {"type": "string", "enum": list(WEEKDAYS)},
            "description": "weekly: days of the week, including start_time's day. Default: start_time's day.",
        },
        "day_of_month": {
            "type": "integer", "minimum": 1, "maximum": 31,
            "description": "monthly/yearly: day of the month. Default: start_time's day.",
        },
        "week_of_month": {
            "type": "string", "enum": list(WEEK_OF_MONTH),
            "description": "monthly/yearly: with weekday, a relative day such as the second Tuesday.",
        },
        "weekday": {
            "type": "string", "enum": list(WEEKDAYS),
            "description": "monthly/yearly: the day for week_of_month. Default: start_time's day.",
        },
        "month": {
            "type": "integer", "minimum": 1, "maximum": 12,
            "description": "yearly: month number. Default: start_time's month.",
        },
        "until": {"type": "string", "description": "Last possible date of the series, YYYY-MM-DD."},
        "count": {"type": "integer", "minimum": 1, "maximum": 999, "description": "Number of occurrences."},
    },
    "required": ["pattern"],
    "additionalProperties": False,
}

SCOPE_SCHEMA = {
    "type": "string",
    "enum": ["occurrence", "series"],
    "description": (
        "For recurring events. An occurrence id from get_calendar acts on that "
        "occurrence only. A series master id is refused unless scope='series'; "
        "scope='series' with an occurrence id acts on its whole series."
    ),
}

_EWS_ITEM_TYPES = {
    EWS_SINGLE: SINGLE,
    EWS_OCCURRENCE: OCCURRENCE,
    EWS_EXCEPTION: EXCEPTION,
    RECURRING_MASTER: SERIES_MASTER,
}

# update_appointment inputs that change a field of the item.
_UPDATE_FIELDS = ("subject", "start_time", "end_time", "location", "body", "categories", "reminder_minutes")


def _item_kind(item) -> Optional[str]:
    """get_calendar's type for an exchangelib CalendarItem (None if EWS sent none)."""
    return _EWS_ITEM_TYPES.get(safe_get(item, "type", None))


def _reminder_minutes(item) -> Optional[int]:
    if not safe_get(item, "reminder_is_set", False):
        return None
    return safe_get(item, "reminder_minutes_before_start", None)


def build_ews_recurrence(rule: Dict[str, Any]) -> Recurrence:
    """exchangelib Recurrence for a rule from ``normalize_recurrence``.

    exchangelib numbers weekdays, week numbers and months from 1, in the
    same order as the rule's names (Monday..Sunday, first..last).
    """
    pattern, interval = rule["pattern"], rule["interval"]
    weekday = WEEKDAYS.index(rule["weekday"]) + 1 if rule.get("weekday") else None
    week = WEEK_OF_MONTH.index(rule["week_of_month"]) + 1 if rule.get("week_of_month") else None
    if pattern == "daily":
        ews_pattern = DailyPattern(interval=interval)
    elif pattern == "weekly":
        ews_pattern = WeeklyPattern(
            interval=interval,
            weekdays=[WEEKDAYS.index(day) + 1 for day in rule["weekdays"]],
            first_day_of_week=1,
        )
    elif pattern == "monthly" and week:
        ews_pattern = RelativeMonthlyPattern(interval=interval, weekday=weekday, week_number=week)
    elif pattern == "monthly":
        ews_pattern = AbsoluteMonthlyPattern(interval=interval, day_of_month=rule["day_of_month"])
    elif interval != 1:
        # EWS yearly patterns carry no interval at all.
        raise CalendarRuleError(
            "Exchange yearly recurrence has no interval; for every N years use "
            "pattern 'monthly' with interval 12*N"
        )
    elif week:
        ews_pattern = RelativeYearlyPattern(weekday=weekday, week_number=week, month=rule["month"])
    else:
        ews_pattern = AbsoluteYearlyPattern(day_of_month=rule["day_of_month"], month=rule["month"])
    # EWSDate.from_date() rejects date subclasses such as EWSDate itself.
    start = EWSDate(*rule["start_date"].timetuple()[:3])
    if rule.get("until"):
        return Recurrence(pattern=ews_pattern, start=start, end=EWSDate(*rule["until"].timetuple()[:3]))
    if rule.get("count"):
        return Recurrence(pattern=ews_pattern, start=start, number=rule["count"])
    return Recurrence(pattern=ews_pattern, start=start)


def _ews_name(value, names) -> str:
    """exchangelib returns enum values as 1-based numbers or as names."""
    return (names[value - 1] if isinstance(value, int) else str(value)).lower()


def _as_date(value) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def rule_from_ews(recurrence) -> Optional[Dict[str, Any]]:
    """Read an exchangelib Recurrence back into the tools' rule shape."""
    if recurrence is None:
        return None
    pattern = recurrence.pattern
    if isinstance(pattern, DailyPattern):
        rule: Dict[str, Any] = {"pattern": "daily", "interval": pattern.interval}
    elif isinstance(pattern, WeeklyPattern):
        days = pattern.weekdays if isinstance(pattern.weekdays, (list, tuple)) else [pattern.weekdays]
        rule = {"pattern": "weekly", "interval": pattern.interval,
                "weekdays": [_ews_name(day, EWS_WEEKDAYS) for day in days]}
    elif isinstance(pattern, AbsoluteMonthlyPattern):
        rule = {"pattern": "monthly", "interval": pattern.interval, "day_of_month": pattern.day_of_month}
    elif isinstance(pattern, RelativeMonthlyPattern):
        rule = {"pattern": "monthly", "interval": pattern.interval,
                "week_of_month": _ews_name(pattern.week_number, WEEK_NUMBERS),
                "weekday": _ews_name(pattern.weekday, EWS_WEEKDAYS)}
    elif isinstance(pattern, (AbsoluteYearlyPattern, RelativeYearlyPattern)):
        month = pattern.month if isinstance(pattern.month, int) else MONTHS.index(pattern.month) + 1
        rule = {"pattern": "yearly", "interval": 1, "month": month}
        if isinstance(pattern, AbsoluteYearlyPattern):
            rule["day_of_month"] = pattern.day_of_month
        else:
            rule["week_of_month"] = _ews_name(pattern.week_number, WEEK_NUMBERS)
            rule["weekday"] = _ews_name(pattern.weekday, EWS_WEEKDAYS)
    else:
        rule = {"pattern": "other", "description": str(pattern)}
    boundary = recurrence.boundary
    rule["start_date"] = _as_date(getattr(boundary, "start", None))
    if isinstance(boundary, EndDatePattern):
        rule["until"] = _as_date(boundary.end)
    elif isinstance(boundary, NumberedPattern):
        rule["count"] = boundary.number
    return rule


def fetch_series_master(account, occurrence):
    """The recurring master of an occurrence or exception, with all fields."""
    master_id = RecurringMasterItemId(id=occurrence.id, changekey=occurrence.changekey)
    master = next(iter(account.fetch(ids=[master_id], folder=account.calendar)), None)
    if master is None or isinstance(master, Exception):
        raise ToolExecutionError(f"Could not load the series of this occurrence: {master}")
    return master


def load_series_masters(account, occurrences, with_rule: bool) -> Tuple[List[Any], Optional[str]]:
    """The recurring masters of ``occurrences``, aligned with them, in one GetItem.

    A CalendarView expands every series into occurrences and never returns
    the master, and an occurrence id does not reveal the master's id. EWS
    resolves it through RecurringMasterItemId. Fetching only the id (plus
    the rule when asked) keeps this one small batch per get_calendar call.
    A failure is reported as a warning instead of failing the listing.
    """
    if not occurrences:
        return [], None
    ids = [RecurringMasterItemId(id=item.id, changekey=item.changekey) for item in occurrences]
    try:
        fetched = list(account.fetch(
            ids=ids, folder=account.calendar, only_fields=["recurrence"] if with_rule else [],
        ))
    except Exception as exc:
        return [None] * len(occurrences), f"Could not load the series masters of recurring events: {exc}"
    masters = [None if isinstance(master, Exception) else master for master in fetched]
    masters += [None] * (len(occurrences) - len(masters))
    failed = sum(master is None for master in masters)
    warning = f"Could not load the series master of {failed} recurring event(s)" if failed else None
    return masters, warning


def _series_first_date(master) -> Optional[date]:
    """Local date of the series' first occurrence (the master's own start)."""
    start = safe_get(master, "start", None)
    if isinstance(start, datetime):
        return start.astimezone(get_configured_timezone()).date()
    if isinstance(start, date):
        return start
    return _as_date(getattr(getattr(safe_get(master, "recurrence", None), "boundary", None), "start", None))


def _series_guard(item, scope: Optional[str]) -> bool:
    """series_target() for an exchangelib item, as a ValidationError with a hint."""
    try:
        return series_target(_item_kind(item), scope)
    except CalendarRuleError as exc:
        raise ValidationError(
            str(exc),
            hint=(
                "get_calendar lists occurrences with their own item_id and the "
                "series_master_id of their series."
            ),
        ) from exc


def build_free_busy_accounts(email_addresses):
    """Build exchangelib free/busy account tuples."""
    return [(email, "Required", False) for email in email_addresses]


def _extract_merged(busy_info: Any) -> Optional[str]:
    """Return the merged free/busy string for a FreeBusyView.

    exchangelib's ``FreeBusyView`` exposes the merged string under ``.merged``.
    Some earlier code (and callers) used the attribute name
    ``merged_free_busy`` which is never populated by exchangelib; reading it
    directly returns ``None`` and makes every slot look unavailable. We
    accept both attributes here so behaviour stays correct if exchangelib
    adds or renames an attribute upstream.
    """
    if busy_info is None:
        return None
    merged = safe_get(busy_info, "merged", None)
    if merged:
        return merged
    # Fall back to the legacy/alternate name some external code may expose.
    merged_alt = safe_get(busy_info, "merged_free_busy", None)
    return merged_alt or None


def get_timezone():
    """Compatibility shim retained for older tests."""
    return None


def format_free_busy_datetime(dt, request_tz):
    """Format free/busy datetimes in the configured local timezone.

    exchangelib hands back CalendarEvent times naive. EWS writes them in the
    time zone of the GetUserAvailability request, which exchangelib takes from
    the start datetime's tzinfo, so callers pass that zone as ``request_tz``.
    """
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=request_tz)
    return dt.astimezone(get_configured_timezone()).isoformat()


FREE_BUSY_LABELS = {
    "0": "Free",
    "1": "Tentative",
    "2": "Busy",
    "3": "OutOfOffice",
    "4": "NoData",
}


def build_slot_summaries(start_time, interval_minutes, merged_free_busy):
    """Expand merged free/busy codes into explicit local-time slots."""
    slots = []
    current = start_time
    for code in merged_free_busy or "":
        slot_end = current + timedelta(minutes=interval_minutes)
        label = FREE_BUSY_LABELS.get(code, "Unknown")
        slots.append({
            "start": current.isoformat(),
            "end": slot_end.isoformat(),
            "code": code,
            "label": label,
            "is_blocking": code in {"2", "3", "4"},
        })
        current = slot_end
    return slots


def parse_display_datetime(dt_str):
    """Parse the raw user-supplied datetime while preserving its explicit offset."""
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


def summarize_availability(merged_free_busy, calendar_events):
    """Return a compact summary that is easier for LLMs and clients to interpret."""
    codes = set(merged_free_busy or "")
    busy_types = {str(safe_get(event, "busy_type", "")) for event in calendar_events or []}

    if "3" in codes:
        primary = "out_of_office"
    elif "2" in codes:
        primary = "busy"
    elif "4" in codes:
        primary = "no_data"
    elif "WorkingElsewhere" in busy_types:
        primary = "working_elsewhere"
    elif "1" in codes:
        primary = "tentative"
    else:
        primary = "free"

    return {
        "primary_status": primary,
        "is_fully_free": codes <= {"0"} if merged_free_busy is not None else False,
        "has_busy": "2" in codes,
        "has_tentative": "1" in codes,
        "has_out_of_office": "3" in codes,
        "has_no_data": "4" in codes,
        "has_working_elsewhere": "WorkingElsewhere" in busy_types,
    }


class CreateAppointmentTool(BaseTool):
    """Tool for creating calendar appointments."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "create_appointment",
            "description": (
                "Create a calendar appointment or meeting with attendees; with "
                "recurrence, a recurring series whose item_id is the series master."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "Appointment subject"
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Start time (ISO 8601 format)"
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End time (ISO 8601 format)"
                    },
                    "location": {
                        "type": "string",
                        "description": "Meeting location (optional)"
                    },
                    "body": {
                        "type": "string",
                        "description": "Appointment body (optional)"
                    },
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Attendee email addresses (optional)"
                    },
                    "is_all_day": {
                        "type": "boolean",
                        "description": "All day event (optional)",
                        "default": False
                    },
                    "reminder_minutes": {
                        "type": ["integer", "null"],
                        "minimum": 0,
                        "description": (
                            "Reminder this many minutes before the start "
                            "(0 = at the start). Default 15. null = no reminder."
                        ),
                        "default": 15
                    },
                    # A copy: routed tools append provider notes to it.
                    "recurrence": {**RECURRENCE_SCHEMA},
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Outlook categories to attach to the appointment "
                            "(Issue #114). Strings; matched case-insensitively "
                            "against the user's category list."
                        )
                    },
                    **INLINE_ATTACHMENTS_SCHEMA,
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["subject", "start_time", "end_time"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Create calendar appointment."""
        # Parse datetime strings as timezone-aware
        kwargs["start_time"] = parse_datetime_tz_aware(kwargs["start_time"])
        kwargs["end_time"] = parse_datetime_tz_aware(kwargs["end_time"])

        # Validate input
        request = self.validate_input(CreateAppointmentRequest, **kwargs)

        # start_time is an EWSDateTime in the configured TIMEZONE, which
        # exchangelib also writes as the item's StartTimeZone/EndTimeZone. A
        # series therefore repeats at that zone's wall-clock time across DST
        # changes even when the caller passed a fixed offset such as +01:00.
        try:
            reminder = normalize_reminder(request.reminder_minutes)
            rule = None
            ews_recurrence = None
            if kwargs.get("recurrence") is not None:
                rule = normalize_recurrence(kwargs["recurrence"], request.start_time.date())
                ews_recurrence = build_ews_recurrence(rule)
        except CalendarRuleError as exc:
            raise ValidationError(str(exc)) from exc

        try:
            target_mailbox = kwargs.get("target_mailbox")
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Create calendar item
            item = CalendarItem(
                account=account,
                folder=account.calendar,
                subject=request.subject,
                start=request.start_time,
                end=request.end_time,
                is_all_day=request.is_all_day
            )

            # Set optional fields
            if request.location:
                item.location = request.location

            if request.body:
                item.body = request.body

            # reminder_minutes=null asks for no reminder; omitted keeps 15.
            item.reminder_is_set = reminder is not None
            if reminder is not None:
                item.reminder_minutes_before_start = reminder

            if ews_recurrence is not None:
                item.recurrence = ews_recurrence

            # Add attendees
            if request.attendees:
                item.required_attendees = [
                    Attendee(mailbox=Mailbox(email_address=email))
                    for email in request.attendees
                ]

            # Issue #114 — Outlook categories on the appointment.
            cats = kwargs.get("categories")
            if cats:
                item.categories = list(cats)

            # Add inline (base64) attachments if provided
            inline_count = attach_inline_files(item, kwargs.get("inline_attachments", []))
            if inline_count > 0:
                self.logger.info(f"Added {inline_count} inline (base64) attachment(s) to appointment")

            # An item with attendees is a meeting: exchangelib's save() default
            # is SEND_TO_NONE, which stores the attendee list and invites
            # nobody. Send the request (and keep a copy in Sent, matching the
            # cancellation that delete_appointment already files there).
            item.save(
                send_meeting_invitations=(
                    SEND_TO_ALL_AND_SAVE_COPY if request.attendees else SEND_TO_NONE
                )
            )

            self.logger.info(f"Created appointment: {request.subject}")

            return format_success_response(
                "Appointment created successfully",
                item_id=ews_id_to_str(item.id) if hasattr(item, "id") else None,
                subject=request.subject,
                start_time=request.start_time.isoformat(),
                end_time=request.end_time.isoformat(),
                type=SERIES_MASTER if rule else SINGLE,
                reminder_minutes=reminder,
                recurrence=describe_recurrence(rule),
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to create appointment: {e}")
            raise ToolExecutionError(f"Failed to create appointment: {e}")


class GetCalendarTool(BaseTool):
    """Tool for retrieving calendar events."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "get_calendar",
            "description": (
                "Retrieve calendar events for a date range. Each event reports its "
                "type (single, occurrence or exception; series are expanded into "
                "occurrences), series_master_id for occurrences and exceptions, and "
                "reminder_minutes (null when no reminder is set)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "Start date (ISO 8601 format, optional, defaults to today)"
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date (ISO 8601 format, optional, overrides days_ahead)"
                    },
                    "days_ahead": {
                        "type": "integer",
                        "description": "Number of days ahead to retrieve (default: 7, max: 90)",
                        "default": 7,
                        "minimum": 1,
                        "maximum": 90
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of events to retrieve",
                        "default": 50,
                        "maximum": 1000
                    },
                    "include_recurrence": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Add recurrence (pattern, interval, days, end and a "
                            "readable summary) to occurrences and exceptions, read "
                            "from their series master; null for single events."
                        )
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                }
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Get calendar events."""
        try:
            target_mailbox = kwargs.get("target_mailbox")
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Parse dates as timezone-aware
            start_date = kwargs.get("start_date")
            if start_date:
                start_date = parse_datetime_tz_aware(start_date)
            else:
                start_date = make_tz_aware(datetime.now())

            end_date_raw = kwargs.get("end_date")
            if end_date_raw:
                end_date = parse_datetime_tz_aware(end_date_raw)
                # If the caller supplied a date-only string (no 'T'), treat it
                # as "the whole of that day" by extending through the next
                # midnight. Previously we did this whenever the parsed time
                # happened to be exactly 00:00:00, which over-collected events
                # whenever someone deliberately requested a midnight boundary.
                if end_date is not None and isinstance(end_date_raw, str) and "T" not in end_date_raw:
                    end_date = end_date + timedelta(days=1)
            else:
                # Use days_ahead parameter (default: 7, max: 90)
                days_ahead = kwargs.get("days_ahead", 7)
                # Enforce maximum of 90 days
                days_ahead = min(max(1, days_ahead), 90)
                end_date = start_date + timedelta(days=days_ahead)

            max_results = kwargs.get("max_results", 50)
            include_recurrence = bool(kwargs.get("include_recurrence", False))

            # Query calendar - use only() to fetch specific fields and avoid timezone parsing warnings
            items = account.calendar.view(
                start=start_date,
                end=end_date
            ).only(
                'id', 'subject', 'start', 'end', 'location',
                'organizer', 'is_all_day', 'required_attendees',
                'type', 'reminder_is_set', 'reminder_minutes_before_start'
            ).order_by('start')
            items = list(items[:max_results])

            recurring = [item for item in items if _item_kind(item) in (OCCURRENCE, EXCEPTION)]
            masters, warning = load_series_masters(account, recurring, include_recurrence)
            pending_masters = iter(masters)

            # Format events
            events = []
            for item in items:
                # Get organizer email safely
                organizer = safe_get(item, "organizer", None)
                organizer_email = ""
                if organizer and hasattr(organizer, "email_address"):
                    organizer_email = organizer.email_address or ""

                # Get attendees safely - filter out None values
                required_attendees = safe_get(item, "required_attendees", []) or []
                attendee_emails = [
                    att.mailbox.email_address
                    for att in required_attendees
                    if att and hasattr(att, "mailbox") and att.mailbox and hasattr(att.mailbox, "email_address") and att.mailbox.email_address
                ]

                kind = _item_kind(item)
                master = next(pending_masters) if kind in (OCCURRENCE, EXCEPTION) else None
                event_data = {
                    "item_id": ews_id_to_str(safe_get(item, "id", None)) or "unknown",
                    "subject": safe_get(item, "subject", "") or "",
                    "start": safe_get(item, "start", datetime.now()).isoformat(),
                    "end": safe_get(item, "end", datetime.now()).isoformat(),
                    "location": safe_get(item, "location", "") or "",
                    "organizer": organizer_email,
                    "is_all_day": safe_get(item, "is_all_day", False),
                    "attendees": attendee_emails,
                    "type": kind,
                    "series_master_id": ews_id_to_str(safe_get(master, "id", None)) if master is not None else None,
                    "reminder_minutes": _reminder_minutes(item),
                }
                if include_recurrence:
                    event_data["recurrence"] = describe_recurrence(
                        rule_from_ews(safe_get(master, "recurrence", None)) if master is not None else None
                    )
                events.append(event_data)

            self.logger.info(f"Retrieved {len(events)} calendar events")

            extra = {"warnings": [warning]} if warning else {}
            return format_success_response(
                f"Retrieved {len(events)} events",
                events=events,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                mailbox=mailbox,
                **extra
            )

        except Exception as e:
            self.logger.error(f"Failed to get calendar: {e}")
            raise ToolExecutionError(f"Failed to get calendar: {e}")


class UpdateAppointmentTool(BaseTool):
    """Tool for updating calendar appointments."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "update_appointment",
            "description": (
                "Update an existing calendar appointment. An occurrence id changes "
                "that occurrence only; a series master id needs scope='series', which "
                "changes the whole series and overwrites those fields on occurrences "
                "changed individually before."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "Appointment item ID"
                    },
                    "subject": {
                        "type": "string",
                        "description": "New subject (optional)"
                    },
                    "start_time": {
                        "type": "string",
                        "description": "New start time (ISO 8601 format, optional)"
                    },
                    "end_time": {
                        "type": "string",
                        "description": "New end time (ISO 8601 format, optional)"
                    },
                    "location": {
                        "type": "string",
                        "description": "New location (optional)"
                    },
                    "body": {
                        "type": "string",
                        "description": "New body (optional)"
                    },
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Replace the appointment's Outlook categories with "
                            "this list (Issue #114). Pass an empty list to clear."
                        )
                    },
                    "reminder_minutes": {
                        "type": ["integer", "null"],
                        "minimum": 0,
                        "description": (
                            "Set or change the reminder, in minutes before the start "
                            "(0 = at the start). null removes the reminder; omit to keep it."
                        )
                    },
                    "scope": {
                        **SCOPE_SCHEMA,
                        "description": SCOPE_SCHEMA["description"] + (
                            " With 'series', start_time/end_time may change the time of "
                            "day but must stay on the series' first date."
                        ),
                    },
                    **INLINE_ATTACHMENTS_SCHEMA,
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["item_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Update appointment."""
        item_id = kwargs.get("item_id")

        try:
            scope = normalize_scope(kwargs.get("scope"))
            reminder = normalize_reminder(kwargs["reminder_minutes"]) if "reminder_minutes" in kwargs else None
        except CalendarRuleError as exc:
            raise ValidationError(str(exc)) from exc
        # save(update_fields=None) would push every writable field back to the
        # server, so a call that changes nothing is refused before any request.
        # attach_inline_files() skips entries without a name or content.
        attachments = [
            att for att in kwargs.get("inline_attachments") or []
            if isinstance(att, dict) and att.get("file_name") and att.get("file_content")
        ]
        if not attachments and not any(key in kwargs for key in _UPDATE_FIELDS):
            raise ValidationError(
                "nothing to update: pass subject, start_time, end_time, location, body, "
                "categories, reminder_minutes or inline_attachments"
            )
        new_times = {}
        for key in ("start_time", "end_time"):
            if key in kwargs:
                new_times[key] = parse_datetime_tz_aware(kwargs[key])
                if new_times[key] is None:
                    raise ValidationError(f"{key} must be an ISO 8601 datetime")

        try:
            target_mailbox = kwargs.get("target_mailbox")
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Get the appointment
            item = account.calendar.get(id=item_id)

            # An occurrence id updates that occurrence only (EWS turns it
            # into an exception). A master id rewrites every occurrence, so
            # it needs the explicit scope; scope='series' on an occurrence id
            # resolves its master.
            series_master_id = None
            if _series_guard(item, scope):
                item = fetch_series_master(account, item)
                series_master_id = ews_id_to_str(item.id)
            kind = _item_kind(item)
            if kind == SERIES_MASTER and new_times:
                first_date = _series_first_date(item)
                if first_date is None:
                    raise ValidationError(
                        "Exchange returned no start for this series, so start_time/end_time "
                        "cannot be checked against its first date"
                    )
                try:
                    for key, value in new_times.items():
                        require_series_first_date(key, value.date(), first_date)
                except CalendarRuleError as exc:
                    raise ValidationError(str(exc)) from exc

            # Track the fields actually touched: a bare save() makes
            # exchangelib update every writable CalendarItem field, which
            # fails on recurring occurrences (read-only uid/recurrence) and
            # pushes unset values back to the server.
            changed_fields = []

            if "subject" in kwargs:
                item.subject = kwargs["subject"]
                changed_fields.append("subject")

            if "start_time" in new_times:
                item.start = new_times["start_time"]
                changed_fields.append("start")

            if "end_time" in new_times:
                item.end = new_times["end_time"]
                changed_fields.append("end")

            if "location" in kwargs:
                item.location = kwargs["location"]
                changed_fields.append("location")

            if "body" in kwargs:
                item.body = kwargs["body"]
                changed_fields.append("body")

            # Issue #114 — replace categories with the supplied list.
            if "categories" in kwargs:
                item.categories = list(kwargs["categories"] or [])
                changed_fields.append("categories")

            if "reminder_minutes" in kwargs:
                item.reminder_is_set = reminder is not None
                changed_fields.append("reminder_is_set")
                if reminder is not None:
                    item.reminder_minutes_before_start = reminder
                    changed_fields.append("reminder_minutes_before_start")

            # Add inline (base64) attachments if provided. On a saved item
            # exchangelib creates them on the server right away.
            inline_count = attach_inline_files(item, kwargs.get("inline_attachments", []))
            if inline_count > 0:
                self.logger.info(f"Added {inline_count} inline (base64) attachment(s) to appointment")

            # Same default trap as create: without send_meeting_invitations,
            # attendees never see the new time/location.
            has_attendees = bool(
                (safe_get(item, "required_attendees", []) or [])
                or (safe_get(item, "optional_attendees", []) or [])
            )
            extra = {"series_master_id": series_master_id} if series_master_id else {}
            if changed_fields:
                item.save(
                    update_fields=changed_fields,
                    send_meeting_invitations=(
                        SEND_TO_ALL_AND_SAVE_COPY if has_attendees else SEND_TO_NONE
                    ),
                )
            elif has_attendees:
                # Only attachments were added. EWS sends a meeting update only
                # with a field change, and there is none to make.
                extra["note"] = (
                    "Attachments were added; no meeting update was sent because no "
                    "other field changed."
                )

            self.logger.info(f"Updated appointment {item_id}")

            return format_success_response(
                "Appointment updated successfully",
                item_id=item_id,
                type=kind,
                updated_fields=changed_fields,
                attachments_added=inline_count,
                mailbox=mailbox,
                **extra
            )

        except ValidationError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to update appointment: {e}")
            raise ToolExecutionError(f"Failed to update appointment: {e}")


class DeleteAppointmentTool(BaseTool):
    """Tool for deleting calendar appointments."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "delete_appointment",
            "description": (
                "Delete a calendar appointment. An occurrence id deletes that "
                "occurrence only; a series master id needs scope='series'."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "Appointment item ID to delete"
                    },
                    "send_cancellation": {
                        "type": "boolean",
                        "description": "Send cancellation to attendees (for meetings)",
                        "default": True
                    },
                    "scope": {
                        **SCOPE_SCHEMA,
                        "description": SCOPE_SCHEMA["description"] + (
                            " With 'series', the whole series is deleted (or cancelled)."
                        ),
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["item_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Delete appointment."""
        item_id = kwargs.get("item_id")
        send_cancellation = kwargs.get("send_cancellation", True)
        try:
            scope = normalize_scope(kwargs.get("scope"))
        except CalendarRuleError as exc:
            raise ValidationError(str(exc)) from exc

        try:
            target_mailbox = kwargs.get("target_mailbox")
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Get and delete the appointment
            item = account.calendar.get(id=item_id)

            # Deleting or cancelling an occurrence id affects that occurrence
            # only; a master id would remove the whole series, so it needs
            # the explicit scope.
            series_master_id = None
            if _series_guard(item, scope):
                item = fetch_series_master(account, item)
                series_master_id = ews_id_to_str(item.id)
            kind = _item_kind(item)

            # Cancel when anyone was invited — optional attendees are
            # attendees too, and only cancel() notifies them.
            has_attendees = bool(
                (safe_get(item, "required_attendees", []) or [])
                or (safe_get(item, "optional_attendees", []) or [])
            )

            cancelled = bool(send_cancellation and has_attendees)
            if cancelled:
                item.cancel()
            else:
                item.delete()

            self.logger.info(f"Deleted appointment {item_id}")

            extra = {"series_master_id": series_master_id} if series_master_id else {}
            return format_success_response(
                "Appointment deleted successfully",
                item_id=item_id,
                type=kind,
                cancellation_sent=cancelled,
                mailbox=mailbox,
                **extra
            )

        except ValidationError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to delete appointment: {e}")
            raise ToolExecutionError(f"Failed to delete appointment: {e}")


class RespondToMeetingTool(BaseTool):
    """Tool for responding to meeting invitations."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "respond_to_meeting",
            "description": "Respond to a meeting invitation (accept, tentative, or decline).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "Meeting invitation item ID"
                    },
                    "response": {
                        "type": "string",
                        "enum": ["Accept", "Tentative", "Decline"],
                        "description": "Response type"
                    },
                    "message": {
                        "type": "string",
                        "description": "Optional response message"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["item_id", "response"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Respond to meeting invitation."""
        item_id = kwargs.get("item_id")
        response = kwargs.get("response")
        message = kwargs.get("message", "")

        try:
            target_mailbox = kwargs.get("target_mailbox")
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # An invitation that is still in the Inbox is a MeetingRequest,
            # not a CalendarItem: account.calendar.get() asks EWS for
            # CalendarItem-only properties and fails with
            # ErrorInvalidPropertyRequest. Try the calendar first (the id a
            # caller took from get_calendar), then fall back to the mail
            # folders, where MeetingRequest exposes the same
            # accept/decline API.
            try:
                item = account.calendar.get(id=item_id)
            except Exception as calendar_exc:
                self.logger.info(
                    f"{item_id} is not a calendar item ({calendar_exc}); "
                    "looking for a meeting request in the mail folders"
                )
                item = find_message_for_account(account, item_id)

            # Send response
            if response == "Accept":
                item.accept(body=message)
                action = "accepted"
            elif response == "Tentative":
                item.tentatively_accept(body=message)
                action = "tentatively accepted"
            elif response == "Decline":
                item.decline(body=message)
                action = "declined"
            else:
                raise ToolExecutionError(f"Invalid response: {response}")

            self.logger.info(f"Meeting {item_id} {action}")

            return format_success_response(
                f"Meeting {action}",
                item_id=item_id,
                response=response,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to respond to meeting: {e}")
            raise ToolExecutionError(f"Failed to respond to meeting: {e}")


class CheckAvailabilityTool(BaseTool):
    """Tool for checking free/busy availability."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "check_availability",
            "description": "Check free/busy availability for users in a time range.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "email_addresses": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of email addresses to check"
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Start time (ISO 8601 format)"
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End time (ISO 8601 format)"
                    },
                    "interval_minutes": {
                        "type": "integer",
                        "description": "Time slot granularity in minutes",
                        "default": 30,
                        "minimum": 15,
                        "maximum": 1440
                    },
                    "include_self": {
                        "type": "boolean",
                        "description": "Include the current mailbox/user in the availability check, similar to Outlook Scheduling Assistant",
                        "default": True
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["email_addresses", "start_time", "end_time"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Check availability for users."""
        email_addresses = kwargs.get("email_addresses", [])
        start_time_str = kwargs.get("start_time")
        end_time_str = kwargs.get("end_time")
        interval_minutes = kwargs.get("interval_minutes", 30)
        include_self = kwargs.get("include_self", True)

        # Caller errors — raise ValidationError so the SSE adapter maps
        # these to HTTP 400. Previously a missing/invalid param fell
        # through to ToolExecutionError which surfaces as 500 (Bug #2).
        if not email_addresses:
            raise ValidationError("email_addresses is required and cannot be empty")

        if not start_time_str or not end_time_str:
            raise ValidationError("start_time and end_time are required")

        # Parse datetimes up front and with explicit error handling so
        # bad ISO strings don't leak through as opaque 500s.
        try:
            start_time = parse_datetime_tz_aware(start_time_str)
            end_time = parse_datetime_tz_aware(end_time_str)
            display_start_time = parse_display_datetime(start_time_str)
        except ValueError as exc:
            raise ValidationError(
                f"Invalid datetime format: {exc}. Use ISO 8601 "
                f"(e.g. '2026-04-18T08:00:00+00:00')."
            ) from exc

        if not start_time or not end_time:
            raise ValidationError(
                "Invalid datetime format. Use ISO 8601 "
                "(e.g. '2026-04-18T08:00:00+00:00')."
            )

        if end_time <= start_time:
            raise ValidationError("end_time must be after start_time")

        try:
            target_mailbox = kwargs.get("target_mailbox")
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)
            normalized_emails = []
            seen_emails = set()
            for email in email_addresses:
                if not email or not isinstance(email, str):
                    continue
                normalized = email.strip().lower()
                if not normalized or normalized in seen_emails:
                    continue
                seen_emails.add(normalized)
                normalized_emails.append(email)

            current_mailbox = (
                target_mailbox
                if target_mailbox
                else safe_get(account, "primary_smtp_address", None)
            )
            # Guard: ``primary_smtp_address`` can be None on freshly-
            # constructed accounts or when exchangelib hasn't completed
            # autodiscover. ``.lower()`` on None would raise AttributeError
            # and surface as an opaque 500.
            if include_self and isinstance(current_mailbox, str) and current_mailbox:
                current_mailbox_normalized = current_mailbox.lower()
                if current_mailbox_normalized not in seen_emails:
                    seen_emails.add(current_mailbox_normalized)
                    normalized_emails.insert(0, current_mailbox)

            free_busy_accounts = build_free_busy_accounts(normalized_emails)
            availability_data = list(account.protocol.get_free_busy_info(
                accounts=free_busy_accounts,
                start=start_time,
                end=end_time,
                merged_free_busy_interval=interval_minutes
            ))

            # exchangelib FreeBusyView exposes view_type / merged / calendar_events.
            # Older attribute names such as free_busy_view_type or merged_free_busy
            # are not populated, which would incorrectly make busy users appear free.
            availability_results = []
            for email_address, busy_info in zip(normalized_emails, availability_data):
                result = {
                    "email": email_address,
                    "view_type": str(safe_get(busy_info, "view_type", None) or "DetailedMerged"),
                    "calendar_events": []
                }

                # Add calendar event information if available
                calendar_events = safe_get(busy_info, "calendar_events", None) or []
                if calendar_events:
                    for event in calendar_events:
                        result["calendar_events"].append({
                            "start": format_free_busy_datetime(event.start, start_time.tzinfo) if hasattr(event, 'start') else None,
                            "end": format_free_busy_datetime(event.end, start_time.tzinfo) if hasattr(event, 'end') else None,
                            "busy_type": str(event.busy_type) if hasattr(event, 'busy_type') else "Busy",
                            "details": safe_get(event, 'details')
                        })

                # Add merged free/busy string if available
                merged_free_busy = safe_get(busy_info, "merged", None)
                if merged_free_busy is not None:
                    result["merged_free_busy"] = merged_free_busy
                    result["free_busy_legend"] = FREE_BUSY_LABELS
                    result["slot_summaries"] = build_slot_summaries(
                        start_time=display_start_time,
                        interval_minutes=interval_minutes,
                        merged_free_busy=merged_free_busy,
                    )
                    result["blocking_slots"] = [
                        slot for slot in result["slot_summaries"] if slot["is_blocking"]
                    ]
                    result["non_free_slots"] = [
                        slot for slot in result["slot_summaries"] if slot["code"] != "0"
                    ]

                result["availability_summary"] = summarize_availability(
                    merged_free_busy=merged_free_busy,
                    calendar_events=result["calendar_events"],
                )

                availability_results.append(result)

            self.logger.info(f"Retrieved availability for {len(normalized_emails)} users")

            # Extract tz offset suffix defensively — a naive datetime's
            # isoformat() has no ``+00:00`` tail, so the old ``[-6:]``
            # slice returned garbage. Fall back to empty string.
            response_tz = ""
            try:
                iso = display_start_time.isoformat()
                if "+" in iso[10:] or iso.endswith(("Z",)):
                    response_tz = iso[-6:] if iso.endswith(("+00:00", "-00:00")) or "+" in iso[-6:] or "-" in iso[-6:] else ""
            except Exception:
                response_tz = ""

            return format_success_response(
                f"Availability retrieved for {len(normalized_emails)} user(s)",
                availability=availability_results,
                checked_email_addresses=normalized_emails,
                include_self=include_self,
                response_timezone=response_tz,
                time_range={
                    "start": start_time_str,
                    "end": end_time_str,
                    "interval_minutes": interval_minutes
                },
                mailbox=mailbox
            )

        except (ValidationError, ToolExecutionError):
            raise
        except Exception as e:
            # Log the full exception with traceback so operators can
            # diagnose the real upstream cause. Re-raise as
            # ToolExecutionError (HTTP 500) — this branch is for genuine
            # server-side failures; caller errors are handled above.
            self.logger.exception(
                f"check_availability failed: {type(e).__name__}: {e}"
            )
            raise ToolExecutionError(f"Failed to check availability: {e}")


class FindMeetingTimesTool(BaseTool):
    """Tool for finding meeting times using deterministic availability scoring."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "find_meeting_times",
            "description": "Find optimal meeting times based on attendee availability.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "attendees": {
                        "type": "array",
                        "description": "List of attendee email addresses",
                        "items": {"type": "string"}
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": "Meeting duration in minutes",
                        "default": 60,
                        "minimum": 15,
                        "maximum": 480
                    },
                    "date_range_start": {
                        "type": "string",
                        "description": "Start of date range to search (ISO 8601 format)"
                    },
                    "date_range_end": {
                        "type": "string",
                        "description": "End of date range to search (ISO 8601 format)"
                    },
                    "max_suggestions": {
                        "type": "integer",
                        "description": "Maximum number of time suggestions",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 20
                    },
                    "preferences": {
                        "type": "object",
                        "description": "Scheduling preferences",
                        "properties": {
                            "prefer_morning": {"type": "boolean", "default": False},
                            "prefer_afternoon": {"type": "boolean", "default": False},
                            "avoid_back_to_back": {"type": "boolean", "default": True},
                            "working_hours_only": {"type": "boolean", "default": True},
                            "min_break_minutes": {"type": "integer", "default": 15},
                            "earliest_hour": {"type": "integer", "default": 9, "minimum": 0, "maximum": 23},
                            "latest_hour": {"type": "integer", "default": 17, "minimum": 0, "maximum": 23}
                        }
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["attendees", "date_range_start", "date_range_end"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Find optimal meeting times."""
        attendees = kwargs.get("attendees", [])
        duration_minutes = kwargs.get("duration_minutes", 60)
        date_range_start_str = kwargs.get("date_range_start")
        date_range_end_str = kwargs.get("date_range_end")
        max_suggestions = kwargs.get("max_suggestions", 5)
        preferences = kwargs.get("preferences", {})

        if duration_minutes < 15 or duration_minutes > 480:
            raise ToolExecutionError("duration_minutes must be between 15 and 480")

        if not attendees:
            raise ToolExecutionError("attendees list is required")

        if not date_range_start_str or not date_range_end_str:
            raise ToolExecutionError("date_range_start and date_range_end are required")

        try:
            target_mailbox = kwargs.get("target_mailbox")
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Parse dates
            start_date = parse_datetime_tz_aware(date_range_start_str)
            end_date = parse_datetime_tz_aware(date_range_end_str)

            if not start_date or not end_date:
                raise ToolExecutionError("Invalid date format. Use ISO 8601 format.")

            if end_date <= start_date:
                raise ToolExecutionError("date_range_end must be after date_range_start")

            # Extract preferences
            prefer_morning = preferences.get("prefer_morning", False)
            prefer_afternoon = preferences.get("prefer_afternoon", False)
            avoid_back_to_back = preferences.get("avoid_back_to_back", True)
            working_hours_only = preferences.get("working_hours_only", True)
            min_break_minutes = preferences.get("min_break_minutes", 15)
            earliest_hour = preferences.get("earliest_hour", 9)
            latest_hour = preferences.get("latest_hour", 17)

            free_busy_accounts = build_free_busy_accounts(attendees)
            availability_data = list(account.protocol.get_free_busy_info(
                accounts=free_busy_accounts,
                start=start_date,
                end=end_date,
                merged_free_busy_interval=15  # 15-minute intervals
            ))

            # Diagnostic context useful when debugging bad suggestion output.
            # Keep it compact so we don't flood logs at INFO.
            if self.logger.isEnabledFor(logging.DEBUG):
                sample_lens = [
                    len(_extract_merged(info) or "") for info in availability_data[:3]
                ]
                self.logger.debug(
                    "find_meeting_times: start=%s end=%s tz=%s attendees=%d "
                    "availability_data=%d merged_lens_sample=%s",
                    start_date, end_date, getattr(start_date, "tzinfo", None),
                    len(attendees), len(availability_data), sample_lens,
                )

            # Analyze availability and find open slots
            suggestions = []
            current_time = start_date

            while current_time < end_date and len(suggestions) < max_suggestions:
                # Skip to next day boundary if we're past working hours
                if working_hours_only:
                    if current_time.hour < earliest_hour:
                        current_time = current_time.replace(hour=earliest_hour, minute=0, second=0)
                    elif current_time.hour >= latest_hour:
                        current_time = (current_time + timedelta(days=1)).replace(hour=earliest_hour, minute=0, second=0)
                        continue

                # Check if this time slot works for all attendees
                slot_end = current_time + timedelta(minutes=duration_minutes)

                # Make sure we don't go past end_date or working hours
                if slot_end > end_date:
                    break

                if working_hours_only and slot_end.hour > latest_hour:
                    current_time = (current_time + timedelta(days=1)).replace(hour=earliest_hour, minute=0, second=0)
                    continue

                # Check if all attendees are available for the full slot.
                # Treat missing data (slot past end of merged_free_busy) or
                # attendees without a merged free/busy response as BUSY so we
                # never surface a slot we couldn't actually verify.
                all_available = True
                buffer_available = True
                for busy_info in availability_data:
                    # exchangelib's FreeBusyView exposes `.merged`; older code
                    # in this file (and some external references) used the
                    # string `merged_free_busy`. Accept both so we don't
                    # silently fail when the attribute shape changes.
                    merged = _extract_merged(busy_info)
                    if not merged:
                        all_available = False
                        break

                    minutes_from_start = int((current_time - start_date).total_seconds() / 60)
                    interval_index = minutes_from_start // 15
                    intervals_needed = (duration_minutes + 14) // 15  # Round up

                    # Slot must fit entirely within the returned data.
                    if interval_index + intervals_needed > len(merged):
                        all_available = False
                        break

                    for i in range(intervals_needed):
                        status = merged[interval_index + i]
                        # 0=Free, 1=Tentative, 2=Busy, 3=OOF, 4=NoData
                        if status in ('2', '3', '4'):
                            all_available = False
                            break

                    if not all_available:
                        break

                    # Buffer check (only used for scoring, not for filtering).
                    if avoid_back_to_back:
                        buffer_intervals = max(1, min_break_minutes // 15)
                        pre_start = max(0, interval_index - buffer_intervals)
                        post_end = min(len(merged), interval_index + intervals_needed + buffer_intervals)
                        for i in range(pre_start, post_end):
                            if merged[i] in ('2', '3'):
                                buffer_available = False
                                break

                if all_available:
                    score = 100

                    if prefer_morning and current_time.hour < 12:
                        score += 20
                    if prefer_afternoon and current_time.hour >= 13:
                        score += 20

                    if current_time.hour < 9 or current_time.hour > 16:
                        score -= 10

                    if avoid_back_to_back and buffer_available:
                        score += 10

                    suggestions.append({
                        "start_time": format_datetime(current_time),
                        "end_time": format_datetime(slot_end),
                        "duration_minutes": duration_minutes,
                        "score": score,
                        "day_of_week": current_time.strftime("%A"),
                        "time_of_day": "Morning" if current_time.hour < 12 else "Afternoon" if current_time.hour < 17 else "Evening"
                    })

                    # Advance past the accepted slot (plus buffer) so we don't
                    # emit N overlapping suggestions at 15-minute offsets of
                    # the same hour.
                    advance = timedelta(
                        minutes=duration_minutes + (min_break_minutes if avoid_back_to_back else 0)
                    )
                    current_time = current_time + advance
                else:
                    # Keep scanning in 15-minute steps.
                    current_time += timedelta(minutes=15)

            # Sort suggestions by score (highest first)
            suggestions.sort(key=lambda x: x['score'], reverse=True)

            # Limit to max_suggestions
            suggestions = suggestions[:max_suggestions]

            self.logger.info(f"Found {len(suggestions)} meeting time suggestions for {len(attendees)} attendees")

            return format_success_response(
                f"Found {len(suggestions)} optimal meeting times",
                suggestions=suggestions,
                attendees=attendees,
                duration_minutes=duration_minutes,
                date_range={
                    "start": date_range_start_str,
                    "end": date_range_end_str
                },
                preferences=preferences,
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to find meeting times: {e}")
            raise ToolExecutionError(f"Failed to find meeting times: {e}")
