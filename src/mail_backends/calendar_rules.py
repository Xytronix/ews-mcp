"""Provider-neutral calendar rules: recurrence, reminders and series scope.

Exchange, Graph and CalDAV each spell a repeating event differently. The
calendar tools accept one ``recurrence`` object, validate it here once, and
each provider turns the normalized rule into its own wire format. The same
holds for ``reminder_minutes`` (``None`` means no reminder) and for the
``scope`` guard that keeps an update or delete aimed at a series master from
rewriting every occurrence by accident.
"""
from __future__ import annotations

import calendar
from datetime import date
from typing import Any, Dict, List, Optional

from .base import MailBackendError

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WEEK_OF_MONTH = ("first", "second", "third", "fourth", "last")
PATTERNS = ("daily", "weekly", "monthly", "yearly")
SCOPES = ("occurrence", "series")
RECURRENCE_KEYS = (
    "pattern", "interval", "weekdays", "day_of_month", "week_of_month",
    "weekday", "month", "until", "count",
)
DEFAULT_REMINDER_MINUTES = 15
MAX_COUNT = 999
# Exchange's own limits (DailyRecurrence 1-999, Weekly/Monthly 1-99). Graph
# stores events in the same mailbox, so the same bounds apply there.
MAX_INTERVAL = {"daily": 999, "weekly": 99, "monthly": 99, "yearly": 99}

# Item types reported by get_calendar, the same words for every provider.
SINGLE = "single"
OCCURRENCE = "occurrence"
EXCEPTION = "exception"
SERIES_MASTER = "series_master"

_PATTERN_KEYS = {
    "daily": set(),
    "weekly": {"weekdays"},
    "monthly": {"day_of_month", "week_of_month", "weekday"},
    "yearly": {"month", "day_of_month", "week_of_month", "weekday"},
}
_UNITS = {"daily": "day", "weekly": "week", "monthly": "month", "yearly": "year"}


class CalendarRuleError(MailBackendError):
    """Recurrence, reminder or scope input that is invalid or that a provider cannot store.

    A ``MailBackendError`` so a routed backend can raise it directly; the
    calendar tools report it as a ``ValidationError``.
    """


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _day_name(value: Any, field: str) -> str:
    name = value.strip().lower() if isinstance(value, str) else None
    if name not in WEEKDAYS:
        raise CalendarRuleError(f"{field} must be a day name such as 'tuesday', not {value!r}")
    return name


def weeks_of_month(day: date) -> tuple:
    """Every ``week_of_month`` value that describes ``day``.

    The 4th Tuesday of a month with only four Tuesdays is also its last one.
    """
    names = []
    nth = (day.day - 1) // 7
    if nth < 4:
        names.append(WEEK_OF_MONTH[nth])
    if day.day + 7 > calendar.monthrange(day.year, day.month)[1]:
        names.append("last")
    return tuple(names)


def normalize_recurrence(spec: Any, first_date: date) -> Dict[str, Any]:
    """Validate the tool's ``recurrence`` object against the first occurrence.

    ``first_date`` is the local date of ``start_time``. A series must start on
    one of its own occurrences, so every day/week/month field has to agree
    with it; omitted fields default to it. Returns a dict with ``pattern``,
    ``interval``, ``start_date``, the pattern's day fields, ``until`` (a date
    or None) and ``count`` (an int or None).
    """
    if not isinstance(spec, dict):
        raise CalendarRuleError("recurrence must be an object such as {\"pattern\": \"weekly\"}")
    unknown = sorted(set(spec) - set(RECURRENCE_KEYS))
    if unknown:
        raise CalendarRuleError(
            f"recurrence.{unknown[0]} is not supported; use {', '.join(RECURRENCE_KEYS)}"
        )
    pattern = spec.get("pattern")
    if pattern not in PATTERNS:
        raise CalendarRuleError("recurrence.pattern must be one of daily, weekly, monthly, yearly")
    for key in ("weekdays", "day_of_month", "week_of_month", "weekday", "month"):
        if spec.get(key) is not None and key not in _PATTERN_KEYS[pattern]:
            raise CalendarRuleError(f"recurrence.{key} does not apply to a {pattern} pattern")

    interval = spec.get("interval", 1)
    limit = MAX_INTERVAL[pattern]
    if not _whole(interval) or not 1 <= interval <= limit:
        raise CalendarRuleError(f"recurrence.interval must be a whole number from 1 to {limit} for a {pattern} pattern")

    start_day = WEEKDAYS[first_date.weekday()]
    rule: Dict[str, Any] = {"pattern": pattern, "interval": interval, "start_date": first_date}

    if pattern == "weekly":
        weekdays = spec.get("weekdays")
        if weekdays is None:
            weekdays = [start_day]
        if not isinstance(weekdays, list) or not weekdays:
            raise CalendarRuleError("recurrence.weekdays must be a non-empty list of day names")
        names = {_day_name(day, "recurrence.weekdays") for day in weekdays}
        if start_day not in names:
            raise CalendarRuleError(
                f"start_time falls on a {start_day}, which is not in recurrence.weekdays; "
                "start the series on its first occurrence"
            )
        rule["weekdays"] = sorted(names, key=WEEKDAYS.index)

    if pattern in ("monthly", "yearly"):
        if pattern == "yearly":
            month = spec.get("month", first_date.month)
            if not _whole(month) or not 1 <= month <= 12:
                raise CalendarRuleError("recurrence.month must be a month number from 1 to 12")
            if month != first_date.month:
                raise CalendarRuleError(
                    f"start_time is in month {first_date.month}, not recurrence.month {month}; "
                    "start the series on its first occurrence"
                )
            rule["month"] = month
        relative = spec.get("week_of_month") is not None or spec.get("weekday") is not None
        if relative:
            if spec.get("day_of_month") is not None:
                raise CalendarRuleError(
                    "use recurrence.day_of_month, or week_of_month with weekday, not both"
                )
            week = spec.get("week_of_month")
            if week not in WEEK_OF_MONTH:
                raise CalendarRuleError(
                    "recurrence.week_of_month must be first, second, third, fourth or last "
                    "for a pattern such as 'second Tuesday'"
                )
            weekday = _day_name(spec.get("weekday") or start_day, "recurrence.weekday")
            if weekday != start_day or week not in weeks_of_month(first_date):
                raise CalendarRuleError(
                    f"start_time ({first_date.isoformat()}) is not the {week} {weekday} of its month; "
                    "start the series on its first occurrence"
                )
            rule["week_of_month"] = week
            rule["weekday"] = weekday
        else:
            day = spec.get("day_of_month", first_date.day)
            if not _whole(day) or not 1 <= day <= 31:
                raise CalendarRuleError("recurrence.day_of_month must be a whole number from 1 to 31")
            if day != first_date.day:
                raise CalendarRuleError(
                    f"start_time is on day {first_date.day}, not recurrence.day_of_month {day}; "
                    "start the series on its first occurrence"
                )
            rule["day_of_month"] = day

    until, count = spec.get("until"), spec.get("count")
    if until is not None and count is not None:
        raise CalendarRuleError("pass recurrence.until or recurrence.count, not both")
    rule["until"] = rule["count"] = None
    if count is not None:
        if not _whole(count) or not 1 <= count <= MAX_COUNT:
            raise CalendarRuleError(f"recurrence.count must be a whole number from 1 to {MAX_COUNT}")
        rule["count"] = count
    if until is not None:
        try:
            if not isinstance(until, str) or len(until) != 10:
                raise ValueError
            end_date = date.fromisoformat(until)
        except ValueError:
            raise CalendarRuleError(f"recurrence.until must be a date (YYYY-MM-DD), not {until!r}") from None
        if end_date < first_date:
            raise CalendarRuleError(
                f"recurrence.until {until} is before the first occurrence on {first_date.isoformat()}"
            )
        rule["until"] = end_date
    return rule


def _join(words: List[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def summarize_recurrence(rule: Dict[str, Any]) -> str:
    """One readable sentence for a normalized (or read-back) rule."""
    pattern = rule.get("pattern")
    if pattern not in _UNITS:
        return rule.get("description") or "Recurring series with a pattern this server cannot describe"
    interval = rule.get("interval") or 1
    unit = _UNITS[pattern]
    text = f"Every {unit}" if interval == 1 else f"Every {interval} {unit}s"
    month = calendar.month_name[rule["month"]] if rule.get("month") else None
    if pattern == "weekly" and rule.get("weekdays"):
        text += " on " + _join([day.capitalize() for day in rule["weekdays"]])
    elif rule.get("week_of_month"):
        days = " or ".join(day.capitalize() for day in str(rule.get("weekday")).split(" or "))
        text += f" on the {rule['week_of_month']} {days}"
        text += f" of {month}" if month else ""
    elif rule.get("day_of_month"):
        text += f" on {month} {rule['day_of_month']}" if month else f" on day {rule['day_of_month']}"
    if rule.get("start_date"):
        text += f", from {_iso(rule['start_date'])}"
    if rule.get("until"):
        text += f" until {_iso(rule['until'])}"
    elif rule.get("count"):
        text += f", {rule['count']} occurrence" + ("" if rule["count"] == 1 else "s")
    else:
        text += ", no end date"
    return text


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, date) else value


def describe_recurrence(rule: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The ``recurrence`` value tools return: the rule's set fields plus a summary."""
    if rule is None:
        return None
    described = {key: _iso(value) for key, value in rule.items() if value is not None}
    described["summary"] = summarize_recurrence(rule)
    return described


def normalize_reminder(value: Any) -> Optional[int]:
    """``reminder_minutes``: minutes before the start, or ``None`` for no reminder."""
    if value is None:
        return None
    if not _whole(value) or value < 0:
        raise CalendarRuleError(
            "reminder_minutes must be a whole number of minutes (0 or more), or null for no reminder"
        )
    return value


def normalize_scope(value: Any) -> Optional[str]:
    if value is None:
        return None
    if value not in SCOPES:
        raise CalendarRuleError("scope must be 'occurrence' or 'series'")
    return value


def series_target(kind: Optional[str], scope: Optional[str]) -> bool:
    """Apply the series guard; True when the call must act on the occurrence's master.

    An occurrence or exception id acts on that one instance. A series master
    id needs ``scope='series'``, so a small edit cannot rewrite every
    occurrence by accident. ``scope='series'`` on an occurrence id acts on
    its master; on a single appointment it is refused.
    """
    if kind == SERIES_MASTER and scope != "series":
        raise CalendarRuleError(
            "item_id is a whole recurring series; pass scope='series' to change every occurrence, "
            "or an occurrence id from get_calendar to change one"
        )
    if scope == "series" and kind not in (SERIES_MASTER, OCCURRENCE, EXCEPTION):
        raise CalendarRuleError("scope='series' needs a recurring event; this item is a single appointment")
    return scope == "series" and kind in (OCCURRENCE, EXCEPTION)


def require_series_first_date(field: str, value: date, first_date: date) -> None:
    """A series' start/end may move in time of day, never to another date.

    The master's start is its first occurrence; a new date would also have
    to move the recurrence range, which these tools do not do.
    """
    if value != first_date:
        raise CalendarRuleError(
            f"with scope='series', {field} must stay on the series' first date "
            f"{first_date.isoformat()}; moving a series to another date is not supported"
        )
