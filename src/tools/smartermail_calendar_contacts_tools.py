"""SmarterMail calendar/contact capabilities that EWS cannot express.

Recurring events (create/update a series, edit/delete a single occurrence),
extra calendars, internet (ICS URL) subscriptions, calendar ICS import/export,
extra address books, and contacts bulk/vCard. EWS ``create_appointment`` /
``update_appointment`` have no recurrence object and cannot edit one
occurrence of a series; these tools ride the private JSON API the webmail
uses instead. Payloads were read off the webmail bundle and live-verified
against a hosted SmarterMail server.

IDs: calendar ``id`` is the hex guid from ``calendars/sources``; event
``id`` is the integer SmarterMail event id (not an EWS item id);
occurrence discriminator is ``occurrenceId`` (UTC ISO, e.g.
``2026-09-15T08:00:00Z``); address-book ``id`` is ``itemID`` / folderGuid;
contact ``id`` is the hex guid from ``contacts-all``.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .smartermail_base import SmarterMailTool
from ..exceptions import ToolExecutionError, ValidationError
from ..smartermail import SmarterMailError
from ..utils import format_success_response, parse_datetime

# Stored recurrence.type values from the webmail bundle
# (t.recurrenceType = {ONCE:0, DAILY:4, WEEKLY:5, MONTHLY:6, YEARLY:7}).
_RECURRENCE_TYPES = {
    "once": 0,
    "none": 0,
    "daily": 4,
    "weekly": 5,
    "week": 5,
    "monthly": 6,
    "month": 6,
    "yearly": 7,
    "year": 7,
}
_RECURRENCE_NAMES = {0: "once", 4: "daily", 5: "weekly", 6: "monthly", 7: "yearly"}

# Sunday-first, matching moment.js day() / weeklyDays[].
_DOW = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_DOW_ALIASES = {
    "sunday": 0, "sun": 0, "0": 0,
    "monday": 1, "mon": 1, "1": 1,
    "tuesday": 2, "tue": 2, "tues": 2, "2": 2,
    "wednesday": 3, "wed": 3, "3": 3,
    "thursday": 4, "thu": 4, "thur": 4, "thurs": 4, "4": 4,
    "friday": 5, "fri": 5, "5": 5,
    "saturday": 6, "sat": 6, "6": 6,
}

# reminderIndex dropdown from the webmail bundle (id → minutes). 0 = none,
# 1 = a reminder at the event start.
_REMINDER_MINUTES = {
    0: None,
    1: 0, 2: 5, 3: 10, 4: 15, 5: 30,
    6: 60, 7: 120, 8: 180, 9: 240, 10: 300, 11: 360,
    12: 420, 13: 480, 14: 540, 15: 600, 16: 660, 17: 720,
    18: 1440,
    19: 2880, 20: 4320, 21: 5760,
    22: 10080, 23: 20160,
}

_ZERO_GUID = "00000000-0000-0000-0000-000000000000"
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _action(kwargs: Dict[str, Any], allowed: Iterable[str]) -> str:
    action = (kwargs.get("action") or "").strip().lower().replace("-", "_")
    if action not in set(allowed):
        raise ValidationError(
            f"action must be one of {', '.join(allowed)}; got {action!r}"
        )
    return action


def _owner_user(client) -> str:
    """SmarterMail path owner is the local-part (``admin``), not the email."""
    email = (client.email or "").strip()
    return email.split("@")[0] if email else ""


def _as_list(value: Any) -> List[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [v for v in value if v not in (None, "")]
    return [value]


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be an integer, got {value!r}") from exc


def _notify_seg(kwargs: Dict[str, Any]) -> str:
    return "true" if _as_bool(kwargs.get("notify"), False) else "false"


def _hex_color(value: Any, default: str = "#3B82F6") -> str:
    if value in (None, ""):
        return default
    color = str(value).strip()
    if not color.startswith("#"):
        color = "#" + color
    if not _HEX_COLOR.match(color):
        raise ValidationError(f"color must be #RRGGBB, got {value!r}")
    return color.upper()


def _zone(name: Optional[str]) -> ZoneInfo:
    tz = (name or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(tz)
    except Exception as exc:
        raise ValidationError(f"unknown timezone {tz!r}") from exc


def _default_tz(tool: Any) -> str:
    """SMARTERMAIL_TIMEZONE, else TIMEZONE, for a call that names no timezone."""
    config = tool.ews_client.config
    name = getattr(config, "smartermail_timezone", None) or getattr(config, "timezone", None)
    return (name or "UTC").strip() or "UTC"


def _parse_dt(value: Any, name: str, tz_name: Optional[str] = None) -> datetime:
    if value in (None, ""):
        raise ValidationError(f"{name} required")
    if isinstance(value, datetime):
        dt = value
    else:
        dt = parse_datetime(str(value).strip())
        if dt is None:
            raise ValidationError(f"{name} is not a valid ISO-8601 datetime: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zone(tz_name) if tz_name else timezone.utc)
    return dt


def _dt_payload(value: Any, tz_name: str, name: str = "start") -> Dict[str, Any]:
    """Wall-clock in ``tz_name`` plus the zone id — the shape events/save stores."""
    dt = _parse_dt(value, name, tz_name=tz_name).astimezone(_zone(tz_name))
    return {"dt": dt.strftime("%Y-%m-%dT%H:%M:%S.000"), "tz": tz_name}


def _occurrence_id(value: Any) -> str:
    """UTC ISO used as the occurrence discriminator (list ``occurrenceId``)."""
    if value in (None, ""):
        raise ValidationError("occurrence_id required (UTC ISO from the event list)")
    raw = str(value).strip()
    dt = parse_datetime(raw)
    if dt is None:
        return raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dow_index(dt: datetime) -> int:
    return (dt.weekday() + 1) % 7  # Python Mon=0 → Sunday-first


def _weekly_days(value: Any, start: Optional[datetime] = None) -> List[bool]:
    days = [False] * 7
    items = _as_list(value)
    if items and all(isinstance(x, bool) for x in items) and len(items) == 7:
        return [bool(x) for x in items]
    for item in items:
        if isinstance(item, bool):
            continue
        if isinstance(item, int) and 0 <= item <= 6:
            days[item] = True
            continue
        key = str(item).strip().lower()
        if key in _DOW_ALIASES:
            days[_DOW_ALIASES[key]] = True
            continue
        raise ValidationError(f"unknown weekday {item!r} (use sun..sat or 0-6 Sunday-first)")
    if not any(days) and start is not None:
        days[_dow_index(start)] = True
    if not any(days):
        raise ValidationError("weekly_days required for a weekly recurrence (sun..sat)")
    return days


def _recurrence_type(value: Any) -> int:
    if value is None or value == "":
        raise ValidationError(
            "pattern required (daily, weekly, monthly, yearly)"
        )
    if isinstance(value, int):
        if value in _RECURRENCE_NAMES:
            return value
        raise ValidationError(f"unknown recurrence type {value}")
    key = str(value).strip().lower()
    if key in _RECURRENCE_TYPES:
        return _RECURRENCE_TYPES[key]
    raise ValidationError(
        f"pattern must be daily, weekly, monthly or yearly; got {value!r}"
    )


def _reminder_index(kwargs: Dict[str, Any]) -> int:
    if kwargs.get("reminder_index") not in (None, ""):
        idx = _as_int(kwargs.get("reminder_index"), "reminder_index")
        if idx not in _REMINDER_MINUTES:
            raise ValidationError("reminder_index must be 0-23 (0=none, 4=15m, 5=30m, 6=1h)")
        return idx
    minutes = kwargs.get("reminder_minutes")
    if minutes in (None, ""):
        return 0
    if isinstance(minutes, str) and minutes.strip().lower() == "none":
        return 0
    want = _as_int(minutes, "reminder_minutes")
    # 0 means "no reminder" to callers. Snapping it to the nearest table
    # entry would pick index 1 and set a reminder at the event start.
    if want <= 0:
        return 0
    best = 0
    best_delta = 10**9
    for idx, m in _REMINDER_MINUTES.items():
        if m is None:
            continue
        delta = abs(m - want)
        if delta < best_delta:
            best, best_delta = idx, delta
    return best


def _until_date_iso(value: Any, start: datetime) -> str:
    """recurrence.untilDate as UTC ISO.

    A bare date covers that whole day in the event's zone: the webmail stores
    local 23:59:59 of the chosen day. Midnight would end the series before
    that day's occurrence. A full datetime is used as given.
    """
    day: Optional[date] = None
    if isinstance(value, date) and not isinstance(value, datetime):
        day = value
    elif not isinstance(value, datetime):
        try:
            day = date.fromisoformat(str(value).strip())
        except ValueError:
            pass
    if day is not None:
        until_dt = datetime.combine(day, time(23, 59, 59), tzinfo=start.tzinfo)
    else:
        until_dt = _parse_dt(value, "until_date")
    return until_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _attendees(value: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in _as_list(value):
        if isinstance(item, str):
            email = item.strip()
            if not email:
                continue
            out.append({
                "name": email.split("@")[0],
                "email": email,
                "status": 0,
                "conflict": False,
                "nonavail": None,
            })
            continue
        if not isinstance(item, dict):
            raise ValidationError("attendees must be emails or {name, email} objects")
        email = (item.get("email") or item.get("address") or "").strip()
        if not email:
            raise ValidationError("attendee is missing email")
        out.append({
            "name": (item.get("name") or email.split("@")[0]).strip(),
            "email": email,
            "status": int(item.get("status") or 0),
            "conflict": False,
            "nonavail": None,
        })
    return out


def _build_recurrence(kwargs: Dict[str, Any], start: datetime) -> Dict[str, Any]:
    rtype = _recurrence_type(kwargs.get("pattern") or kwargs.get("recurrence_type"))
    interval = _as_int(kwargs.get("interval") or 1, "interval")
    if interval < 1:
        raise ValidationError("interval must be >= 1")
    until_count = kwargs.get("until_count") or kwargs.get("count")
    until_date = kwargs.get("until_date") or kwargs.get("end_date")
    until_count_on = until_count not in (None, "")
    until_date_on = until_date not in (None, "")
    dummy_until = (start + timedelta(days=365)).strftime("%Y-%m-%dT23:59:59Z")
    until_date_iso = dummy_until
    if until_date_on:
        until_date_iso = _until_date_iso(until_date, start)
    weekly = _weekly_days(kwargs.get("weekly_days") or kwargs.get("days_of_week"), start)
    month_day = _as_int(kwargs.get("month_day") or start.day, "month_day")
    period_index = _as_int(kwargs.get("monthly_period_index") or max((start.day - 1) // 7, 0), "monthly_period_index")
    period_day = _as_int(
        kwargs.get("monthly_period_day_index") if kwargs.get("monthly_period_day_index") is not None else _dow_index(start),
        "monthly_period_day_index",
    )
    specific_day = _as_bool(kwargs.get("monthly_specific_day"), True)
    yearly_month = _as_int(
        kwargs.get("yearly_month_index") if kwargs.get("yearly_month_index") is not None else start.month - 1,
        "yearly_month_index",
    )
    return {
        "type": rtype,
        "untilCount": _as_int(until_count, "until_count") if until_count_on else 10,
        "untilCountEnabled": until_count_on,
        "untilDate": until_date_iso,
        "untilDateEnabled": until_date_on and not until_count_on,
        "dailyInterval": interval,
        "weeklyInterval": interval,
        "weeklyDays": weekly,
        "isWeekdaysOnly": _as_bool(kwargs.get("weekdays_only"), False),
        "monthlyInterval": interval,
        "monthlySpecificDay": specific_day,
        "monthDay": month_day,
        "monthlyPeriodIndex": period_index,
        "monthlyPeriodDayIndex": period_day,
        "yearlyInterval": interval,
        "yearlyMonthIndex": yearly_month,
        "yearlyDay": month_day,
        "yearlySpecificDate": _as_bool(kwargs.get("yearly_specific_date"), True),
        "yearlyPeriodIndex": period_index,
        "yearlyPeriodDayIndex": period_day,
    }


def _slim_calendar(cal: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": cal.get("id"),
        "name": cal.get("name"),
        "color": cal.get("color"),
        "folder_id": cal.get("folderId"),
        "owner": cal.get("owner") or cal.get("ownerUsername"),
        "is_primary": bool(cal.get("isPrimary")),
        "is_visible": cal.get("isVisible"),
        "is_internet": bool(cal.get("internetCalendarLink") or cal.get("isWebCalendar")),
        "internet_url": cal.get("internetCalendarLink") or cal.get("subscriptionUrl") or None,
        "internet_interval": cal.get("internetCalendarInterval"),
        "permission": cal.get("permission"),
    }


def _slim_event(ev: Dict[str, Any]) -> Dict[str, Any]:
    start = ev.get("startWithTZ") or ev.get("start")
    end = ev.get("endWithTZ") or ev.get("end")
    return {
        "id": ev.get("id"),
        "uid": ev.get("uid"),
        "subject": ev.get("title") or ev.get("subject"),
        "location": ev.get("location"),
        "start": start,
        "end": end,
        "all_day": ev.get("allDay"),
        "is_recurring": ev.get("isRecurring"),
        "occurrence_id": ev.get("occurrenceId"),
        "calendar_id": ev.get("calId") or ev.get("calendarId"),
        "calendar_name": ev.get("calName"),
        "owner": ev.get("owner"),
        "attendee_count": ev.get("attendeeCount"),
        "reminder_seconds": ev.get("firstReminder"),
    }


def _slim_details(details: Dict[str, Any]) -> Dict[str, Any]:
    rec = details.get("recurrence") or {}
    rtype = rec.get("type")
    attendees = []
    for a in details.get("attendees") or []:
        if isinstance(a, dict):
            attendees.append({
                "name": a.get("name"),
                "email": a.get("email"),
                "status": a.get("status"),
                "is_organizer": a.get("isOrganizer"),
            })
    return {
        "id": details.get("id"),
        "uid": details.get("uid"),
        "calendar_id": details.get("calendarId"),
        "calendar_owner": details.get("calendarOwner"),
        "subject": details.get("subject"),
        "location": details.get("location"),
        "description": details.get("description"),
        "start": details.get("start"),
        "end": details.get("end"),
        "all_day": details.get("allDay"),
        "availability": details.get("availability"),
        "is_private": details.get("isPrivate"),
        "reminder_index": details.get("reminderIndex"),
        "attendees": attendees,
        "recurrence": {
            "type": rtype,
            "pattern": _RECURRENCE_NAMES.get(rtype, rtype),
            "until_count": rec.get("untilCount") if rec.get("untilCountEnabled") else None,
            "until_date": rec.get("untilDate") if rec.get("untilDateEnabled") else None,
            "daily_interval": rec.get("dailyInterval"),
            "weekly_interval": rec.get("weeklyInterval"),
            "weekly_days": rec.get("weeklyDays"),
            "weekdays_only": rec.get("isWeekdaysOnly"),
            "monthly_interval": rec.get("monthlyInterval"),
            "month_day": rec.get("monthDay"),
            "monthly_specific_day": rec.get("monthlySpecificDay"),
            "yearly_interval": rec.get("yearlyInterval"),
        },
    }


def _slim_ab(src: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": src.get("itemID") or src.get("id"),
        "name": src.get("displayName") or src.get("name"),
        "folder_id": src.get("folderId"),
        "owner": src.get("ownerUsername") or src.get("owner"),
        "is_primary": bool(src.get("isPrimary")),
        "is_gal": (src.get("itemID") or src.get("id")) == "gal",
        "access": src.get("access"),
    }


def _slim_contact(ct: Dict[str, Any]) -> Dict[str, Any]:
    emails = ct.get("emailAddressList") or []
    email = ct.get("email") or (emails[0] if emails else None)
    return {
        "id": ct.get("id"),
        "display_as": ct.get("displayAs"),
        "first_name": ct.get("firstName"),
        "last_name": ct.get("lastName"),
        "email": email,
        "emails": emails,
        "source_id": ct.get("sourceId"),
        "source_owner": ct.get("sourceOwner"),
        "source_name": ct.get("sourceName"),
    }


def _write_export(tool: SmarterMailTool, raw: bytes, filename: str) -> Tuple[str, int]:
    data_dir = Path(getattr(tool.ews_client.config, "data_dir", "data") or "data")
    out_dir = data_dir / "smartermail_exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    path.write_bytes(raw)
    return str(path.resolve()), len(raw)


class _CalContactTool(SmarterMailTool):
    """SmarterMailTool that runs the blocking HTTP hop off the event loop."""

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    async def execute(self, **kwargs) -> Dict[str, Any]:
        return await asyncio.to_thread(self._run, kwargs)

    def _run(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        client = self.client(kwargs.get("target_mailbox"))
        return self._dispatch(client, kwargs)

    def _download(self, client, method: str, path: str, **kwargs) -> bytes:
        try:
            return client.download(method, path, **kwargs)
        except SmarterMailError as exc:
            raise ToolExecutionError(str(exc)) from exc

    def _calendars(self, client) -> List[Dict[str, Any]]:
        data = self.call(client, "GET", "/api/v1/calendars/sources")
        return list(data.get("calendars") or [])

    def _resolve_calendar(self, client, kwargs: Dict[str, Any], *, required: bool = False) -> Dict[str, Any]:
        cals = self._calendars(client)
        cal_id = (kwargs.get("calendar_id") or kwargs.get("cal_id") or "").strip()
        name = (kwargs.get("calendar_name") or "").strip()
        if cal_id:
            for cal in cals:
                if cal.get("id") == cal_id:
                    return cal
            raise ValidationError(f"calendar_id {cal_id!r} not found")
        if name:
            matches = [c for c in cals if (c.get("name") or "").lower() == name.lower()]
            if not matches:
                raise ValidationError(f"calendar {name!r} not found")
            return matches[0]
        for cal in cals:
            if cal.get("isPrimary"):
                return cal
        if cals:
            return cals[0]
        if required:
            raise ValidationError("no calendars on this mailbox")
        raise ValidationError("calendar_id required")

    def _address_books(self, client) -> List[Dict[str, Any]]:
        data = self.call(client, "GET", "/api/v1/contacts/sources")
        return list(data.get("sharedLists") or [])

    def _resolve_ab(self, client, kwargs: Dict[str, Any], *, allow_gal: bool = False) -> Dict[str, Any]:
        books = self._address_books(client)
        ab_id = (kwargs.get("address_book_id") or kwargs.get("source_id") or "").strip()
        name = (kwargs.get("address_book_name") or "").strip()
        chosen: Optional[Dict[str, Any]] = None
        if ab_id:
            for src in books:
                if (src.get("itemID") or src.get("id")) == ab_id:
                    chosen = src
                    break
            if chosen is None:
                raise ValidationError(f"address_book_id {ab_id!r} not found")
        elif name:
            matches = [
                s for s in books
                if (s.get("displayName") or s.get("name") or "").lower() == name.lower()
            ]
            if not matches:
                raise ValidationError(f"address book {name!r} not found")
            chosen = matches[0]
        else:
            for src in books:
                if src.get("isPrimary"):
                    chosen = src
                    break
            if chosen is None and books:
                chosen = books[0]
        if chosen is None:
            raise ValidationError("no address books on this mailbox")
        item_id = chosen.get("itemID") or chosen.get("id")
        if item_id == "gal" and not allow_gal:
            raise ValidationError("the Global Address List cannot be modified")
        return chosen

    def _list_events(
        self, client, owner: str, cal_id: str, start: str, end: str,
    ) -> List[Dict[str, Any]]:
        data = self.call(
            client,
            "POST",
            f"/api/v1/calendars/events/{owner}/{cal_id}",
            json={"startDate": start, "endDate": end},
        )
        return list(data.get("events") or [])

    def _get_details(self, client, owner: str, cal_id: str, event_id: int) -> Dict[str, Any]:
        data = self.call(
            client, "GET", f"/api/v1/calendars/events/{owner}/{cal_id}/{event_id}",
        )
        details = data.get("details")
        if not isinstance(details, dict):
            raise ToolExecutionError(f"event {event_id} returned no details")
        return details

    def _delete_series(self, client, owner: str, cal_id: str, event_id: int, notify: bool) -> Any:
        return self.call(
            client,
            "POST",
            "/api/v1/calendars/events/delete-bulk",
            json=[{
                "owner": owner,
                "calendarId": cal_id,
                "eventId": event_id,
                "notify": bool(notify),
            }],
        )

    def _upload(
        self,
        client,
        *,
        context: str,
        context_data: Dict[str, Any],
        filename: str,
        content: bytes,
        content_type: str,
    ) -> Dict[str, Any]:
        """Resumable.js POST /api/upload — calendar-ics and contact-import."""

        def _once(token: str):
            ident = uuid.uuid4().hex
            size = str(len(content))
            return client._client.post(
                "/api/upload",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "context": context,
                    "contextData": json.dumps(context_data),
                },
                data={
                    "resumableChunkNumber": "1",
                    "resumableChunkSize": size,
                    "resumableCurrentChunkSize": size,
                    "resumableTotalSize": size,
                    "resumableType": content_type,
                    "resumableIdentifier": ident,
                    "resumableFilename": filename,
                    "resumableRelativePath": filename,
                    "resumableTotalChunks": "1",
                    "context": context,
                    "contextData": json.dumps(context_data),
                },
                files={"file": (filename, content, content_type)},
            )

        try:
            resp = _once(client._token())
            if resp.status_code in (401, 403):
                with client._lock:
                    client._access_token = None
                    client._authenticate()
                resp = _once(client._token())
        except Exception as exc:
            raise ToolExecutionError(f"SmarterMail /api/upload failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ToolExecutionError(
                f"SmarterMail /api/upload: HTTP {resp.status_code} {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except Exception as exc:
            raise ToolExecutionError(
                f"SmarterMail /api/upload returned non-JSON: {resp.text[:200]}"
            ) from exc
        if isinstance(data, dict) and data.get("success") is False:
            raise ToolExecutionError(
                f"SmarterMail /api/upload failed: {data.get('message') or data}"
            )
        return data if isinstance(data, dict) else {"value": data}

    def _list_contacts(self, client, source: Dict[str, Any], search: Optional[str] = None) -> List[Dict[str, Any]]:
        owner = source.get("ownerUsername") or _owner_user(client)
        item_id = source.get("itemID") or source.get("id")
        name = source.get("displayName") or source.get("name") or "Contacts"
        data = self.call(
            client,
            "POST",
            "/api/v1/contacts/contacts-all",
            json={
                "sources": [{"owner": owner, "id": item_id, "sourceName": name}],
                "searchParams": {
                    "skip": 0,
                    "take": 500,
                    "search": search if search not in (None, "") else None,
                    "sortField": None,
                    "sortDescending": False,
                    "showNonCategorized": True,
                    "getImages": False,
                    "categories": [],
                    "filterFlags": {},
                },
            },
        )
        return list(data.get("results") or [])


# ---------------------------------------------------------------------------
# Recurring events
# ---------------------------------------------------------------------------


class ManageRecurringEventTool(_CalContactTool):
    """Create / get / update / delete / list a recurring series."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_recurring_event",
            "Create, get, update, delete, or list a SmarterMail recurring "
            "calendar series (daily/weekly/monthly/yearly). EWS appointments "
            "cannot express recurrence. event_id is the integer SmarterMail "
            "id from create/list, not an EWS item id. Default calendar is the "
            "mailbox primary. notify=true sends attendee updates.",
            {
                "action": {
                    "type": "string",
                    "enum": ["create", "get", "update", "delete", "list"],
                    "description": "create/get/update/delete a series, or list events in a date range.",
                },
                "calendar_id": {
                    "type": "string",
                    "description": "Calendar guid from manage_calendars list. Default: primary.",
                },
                "event_id": {
                    "type": "integer",
                    "description": "SmarterMail event id (get/update/delete).",
                },
                "subject": {"type": "string", "description": "Event subject (create/update)."},
                "start": {
                    "type": "string",
                    "description": "Series start (ISO-8601). Interpreted in timezone.",
                },
                "end": {
                    "type": "string",
                    "description": "Series end (ISO-8601). Interpreted in timezone.",
                },
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone for start/end. Defaults to SMARTERMAIL_TIMEZONE, else the server TIMEZONE setting.",
                },
                "pattern": {
                    "type": "string",
                    "enum": ["daily", "weekly", "monthly", "yearly"],
                    "description": "Recurrence pattern (create; update if changing).",
                },
                "interval": {
                    "type": "integer",
                    "description": "Repeat every N days/weeks/months/years. Default 1.",
                },
                "weekly_days": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Weekdays for weekly (sun..sat). Default: start's weekday.",
                },
                "until_count": {
                    "type": "integer",
                    "description": "Stop after N occurrences.",
                },
                "until_date": {
                    "type": "string",
                    "description": (
                        "Last day of the series. An ISO date includes that whole day "
                        "in timezone. A full ISO datetime is used as given. Ignored if "
                        "until_count is set."
                    ),
                },
                "location": {"type": "string", "description": "Location."},
                "description": {"type": "string", "description": "HTML or plain-text body."},
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Attendee emails (or {name, email} objects).",
                },
                "reminder_minutes": {
                    "type": "integer",
                    "description": "Reminder offset in minutes (mapped to SmarterMail reminderIndex). 0/omit = none.",
                },
                "all_day": {"type": "boolean", "description": "All-day event."},
                "private": {"type": "boolean", "description": "Mark private."},
                "notify": {
                    "type": "boolean",
                    "description": "Email attendees on create/update/delete. Default false.",
                },
                "start_date": {
                    "type": "string",
                    "description": "List range start (ISO). Default: now-7d.",
                },
                "end_date": {
                    "type": "string",
                    "description": "List range end (ISO). Default: now+90d.",
                },
            },
            required=["action"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs, ("create", "get", "update", "delete", "list"))
        owner = _owner_user(client)
        cal = self._resolve_calendar(client, kwargs)
        cal_id = cal["id"]
        notify = _notify_seg(kwargs)

        if action == "list":
            now = datetime.now(timezone.utc)
            start = kwargs.get("start_date") or (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
            end = kwargs.get("end_date") or (now + timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
            events = [_slim_event(e) for e in self._list_events(client, owner, cal_id, start, end)]
            return format_success_response(
                f"{len(events)} event(s) in {cal.get('name')}",
                action=action,
                calendar_id=cal_id,
                calendar_name=cal.get("name"),
                events=events,
            )

        if action == "create":
            self.require(kwargs, "subject", "start", "end", "pattern")
            tz_name = (kwargs.get("timezone") or "").strip() or _default_tz(self)
            start_dt = _parse_dt(kwargs.get("start"), "start", tz_name=tz_name).astimezone(_zone(tz_name))
            payload = {
                "id": 0,
                "subject": str(kwargs["subject"]),
                "description": kwargs.get("description") or "",
                "isHtml": True,
                "isPrivate": _as_bool(kwargs.get("private"), False),
                "location": kwargs.get("location") or "",
                "availability": 0 if _as_bool(kwargs.get("all_day"), False) else 2,
                "allDay": _as_bool(kwargs.get("all_day"), False),
                "emailNotification": client.email,
                "emailNotificationEnabled": False,
                "reminderIndex": _reminder_index(kwargs),
                "organizerName": "",
                "organizerEmail": "",
                "attendees": _attendees(kwargs.get("attendees")),
                "categories": [],
                "recurrence": _build_recurrence(kwargs, start_dt),
                "resourceId": "",
                "permission": 8,
                "isTentative": False,
                "attachedFiles": [],
                "calendarId": cal_id,
                "calendarOwner": owner,
                "start": _dt_payload(kwargs.get("start"), tz_name, "start"),
                "end": _dt_payload(kwargs.get("end"), tz_name, "end"),
                "meetingUrl": None,
            }
            data = self.call(
                client,
                "POST",
                f"/api/v1/calendars/events/save/{owner}/{cal_id}/null/{notify}",
                json=payload,
            )
            created = (data.get("events") or [None])[0] or {}
            event_id = created.get("id")
            details = self._get_details(client, owner, cal_id, event_id) if event_id else {}
            return format_success_response(
                f"created recurring event {event_id}",
                action=action,
                event_id=event_id,
                uid=created.get("uid") or details.get("uid"),
                calendar_id=cal_id,
                recurrence=_slim_details(details).get("recurrence") if details else None,
                event=_slim_event(created) if created else None,
            )

        if kwargs.get("event_id") in (None, ""):
            raise ValidationError("event_id required")
        event_id = _as_int(kwargs.get("event_id"), "event_id")

        if action == "get":
            details = self._get_details(client, owner, cal_id, event_id)
            return format_success_response(
                f"event {event_id}",
                action=action,
                event=_slim_details(details),
            )

        if action == "delete":
            data = self._delete_series(
                client, owner, cal_id, event_id, _as_bool(kwargs.get("notify"), False),
            )
            return format_success_response(
                f"deleted series {event_id}",
                action=action,
                event_id=event_id,
                calendar_id=cal_id,
                api=data,
            )

        # update: fetch, overlay, save with the series id in the path
        details = self._get_details(client, owner, cal_id, event_id)
        tz_name = (kwargs.get("timezone") or "").strip()
        if not tz_name:
            start_obj = details.get("start") or {}
            tz_name = start_obj.get("tz") or _default_tz(self)
        if kwargs.get("subject") not in (None, ""):
            details["subject"] = str(kwargs["subject"])
        if kwargs.get("location") is not None:
            details["location"] = str(kwargs.get("location") or "")
        if kwargs.get("description") is not None:
            details["description"] = str(kwargs.get("description") or "")
            details["isHtml"] = True
        if kwargs.get("private") is not None:
            details["isPrivate"] = _as_bool(kwargs.get("private"), False)
        if kwargs.get("all_day") is not None:
            details["allDay"] = _as_bool(kwargs.get("all_day"), False)
            details["availability"] = 0 if details["allDay"] else 2
        if kwargs.get("attendees") is not None:
            details["attendees"] = _attendees(kwargs.get("attendees"))
        if kwargs.get("reminder_minutes") not in (None, "") or kwargs.get("reminder_index") not in (None, ""):
            details["reminderIndex"] = _reminder_index(kwargs)
        if kwargs.get("start") not in (None, ""):
            details["start"] = _dt_payload(kwargs.get("start"), tz_name, "start")
        if kwargs.get("end") not in (None, ""):
            details["end"] = _dt_payload(kwargs.get("end"), tz_name, "end")
        if kwargs.get("pattern") or kwargs.get("recurrence_type") or kwargs.get("interval") \
                or kwargs.get("weekly_days") or kwargs.get("until_count") or kwargs.get("until_date"):
            start_src = kwargs.get("start") or (details.get("start") or {}).get("dt")
            start_dt = _parse_dt(start_src, "start", tz_name=tz_name).astimezone(_zone(tz_name))
            rec_kwargs = dict(kwargs)
            if not rec_kwargs.get("pattern") and not rec_kwargs.get("recurrence_type"):
                rec_kwargs["pattern"] = _RECURRENCE_NAMES.get(
                    (details.get("recurrence") or {}).get("type"), "weekly",
                )
            details["recurrence"] = _build_recurrence(rec_kwargs, start_dt)
        details["calendarId"] = cal_id
        details["calendarOwner"] = owner
        # details.id from GET is 0 — the path carries the real series id.
        data = self.call(
            client,
            "POST",
            f"/api/v1/calendars/events/save/{owner}/{cal_id}/{event_id}/{notify}",
            json=details,
        )
        fresh = self._get_details(client, owner, cal_id, event_id)
        return format_success_response(
            f"updated series {event_id}",
            action=action,
            event_id=event_id,
            event=_slim_details(fresh),
            api_keys=list(data.keys()) if isinstance(data, dict) else None,
        )


class UpdateEventOccurrenceTool(_CalContactTool):
    """Edit or delete a single occurrence of a recurring series."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "update_event_occurrence",
            "Get, edit, or delete ONE occurrence of a SmarterMail recurring "
            "series. The rest of the series is unchanged. occurrence_id is the "
            "UTC ISO from manage_recurring_event list (e.g. 2026-09-15T08:00:00Z). "
            "event_id is the integer series id.",
            {
                "action": {
                    "type": "string",
                    "enum": ["get", "update", "delete"],
                    "description": "get/update the occurrence, or delete just that instance.",
                },
                "calendar_id": {
                    "type": "string",
                    "description": "Calendar guid. Default: primary.",
                },
                "event_id": {
                    "type": "integer",
                    "description": "SmarterMail series event id.",
                },
                "occurrence_id": {
                    "type": "string",
                    "description": "UTC ISO occurrence discriminator from the event list.",
                },
                "subject": {"type": "string", "description": "New subject for this occurrence."},
                "location": {"type": "string", "description": "New location for this occurrence."},
                "description": {"type": "string", "description": "New body for this occurrence."},
                "start": {"type": "string", "description": "New start (ISO) for this occurrence."},
                "end": {"type": "string", "description": "New end (ISO) for this occurrence."},
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone for start/end. Defaults to SMARTERMAIL_TIMEZONE, else the server TIMEZONE setting.",
                },
                "notify": {
                    "type": "boolean",
                    "description": "Email attendees. Default false.",
                },
            },
            required=["action", "event_id", "occurrence_id"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs, ("get", "update", "delete"))
        owner = _owner_user(client)
        cal = self._resolve_calendar(client, kwargs)
        cal_id = cal["id"]
        event_id = _as_int(kwargs.get("event_id"), "event_id")
        occ = _occurrence_id(kwargs.get("occurrence_id"))
        notify = _notify_seg(kwargs)
        fetch_path = f"/api/v1/calendars/events/occurrence/{owner}/{cal_id}/{event_id}"

        if action == "delete":
            # 3-arg delete 404s; the live route is .../delete/{owner}/{cal}/{id}/{notify}
            # with {instanceStart} selecting the occurrence.
            data = self.call(
                client,
                "POST",
                f"/api/v1/calendars/events/delete/{owner}/{cal_id}/{event_id}/{notify}",
                json={"instanceStart": occ},
            )
            return format_success_response(
                f"deleted occurrence {occ} of series {event_id}",
                action=action,
                event_id=event_id,
                occurrence_id=occ,
                calendar_id=cal_id,
                api=data,
            )

        data = self.call(client, "POST", fetch_path, json={"occurrenceId": occ})
        details = data.get("details")
        if not isinstance(details, dict):
            raise ToolExecutionError(f"occurrence {occ} of {event_id} returned no details")
        if action == "get":
            return format_success_response(
                f"occurrence {occ} of series {event_id}",
                action=action,
                event_id=event_id,
                occurrence_id=occ,
                event=_slim_details(details),
            )

        tz_name = (kwargs.get("timezone") or "").strip()
        if not tz_name:
            tz_name = (details.get("start") or {}).get("tz") or _default_tz(self)
        if kwargs.get("subject") not in (None, ""):
            details["subject"] = str(kwargs["subject"])
        if kwargs.get("location") is not None:
            details["location"] = str(kwargs.get("location") or "")
        if kwargs.get("description") is not None:
            details["description"] = str(kwargs.get("description") or "")
            details["isHtml"] = True
        if kwargs.get("start") not in (None, ""):
            details["start"] = _dt_payload(kwargs.get("start"), tz_name, "start")
        if kwargs.get("end") not in (None, ""):
            details["end"] = _dt_payload(kwargs.get("end"), tz_name, "end")
        # details.id from the occurrence fetch is 0; save needs the series id
        # and recurrenceId so the server writes an exception, not a new series.
        details["id"] = event_id
        details["recurrenceId"] = occ
        details["calendarId"] = cal_id
        details["calendarOwner"] = owner
        saved = self.call(
            client,
            "POST",
            f"/api/v1/calendars/events/save/{owner}/{cal_id}/{event_id}/{notify}",
            json=details,
        )
        fresh = self.call(client, "POST", fetch_path, json={"occurrenceId": occ}).get("details") or details
        return format_success_response(
            f"updated occurrence {occ} of series {event_id}",
            action=action,
            event_id=event_id,
            occurrence_id=occ,
            event=_slim_details(fresh),
            api_keys=list(saved.keys()) if isinstance(saved, dict) else None,
        )


# ---------------------------------------------------------------------------
# Calendars
# ---------------------------------------------------------------------------


class ManageCalendarsTool(_CalContactTool):
    """List / create / rename / recolour / delete calendars."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_calendars",
            "List, create, rename, recolour, or delete SmarterMail calendars "
            "(beyond the primary). calendar_id is the hex guid from list.",
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "create", "rename", "color", "delete"],
                    "description": "list all calendars; create/rename/color/delete one.",
                },
                "calendar_id": {
                    "type": "string",
                    "description": "Calendar guid (rename/color/delete).",
                },
                "name": {
                    "type": "string",
                    "description": "Calendar name (create/rename).",
                },
                "color": {
                    "type": "string",
                    "description": "Hex colour #RRGGBB (create/color).",
                },
            },
            required=["action"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs, ("list", "create", "rename", "color", "delete"))
        if action == "list":
            cals = [_slim_calendar(c) for c in self._calendars(client)]
            return format_success_response(
                f"{len(cals)} calendar(s)",
                action=action,
                calendars=cals,
            )
        if action == "create":
            self.require(kwargs, "name")
            color = _hex_color(kwargs.get("color"))
            data = self.call(
                client,
                "POST",
                "/api/v1/calendars/calendar-put",
                json={"setting": {"friendlyName": str(kwargs["name"]), "calendarViewColor": color}},
            )
            cal_id = data.get("result")
            return format_success_response(
                f"created calendar {kwargs['name']}",
                action=action,
                calendar_id=cal_id,
                name=str(kwargs["name"]),
                color=color,
            )

        cal = self._resolve_calendar(client, kwargs, required=True)
        cal_id = cal["id"]
        if action == "delete":
            if cal.get("isPrimary"):
                raise ValidationError("refusing to delete the primary calendar")
            data = self.call(client, "POST", f"/api/v1/calendars/calendar-delete/{cal_id}")
            return format_success_response(
                f"deleted calendar {cal.get('name')}",
                action=action,
                calendar_id=cal_id,
                api=data,
            )
        setting: Dict[str, Any] = {
            "id": cal_id,
            "isPrimary": bool(cal.get("isPrimary")),
            "calendarViewColor": cal.get("color") or "#3B82F6",
        }
        if action == "rename":
            self.require(kwargs, "name")
            setting["friendlyName"] = str(kwargs["name"])
        if action == "color":
            setting["calendarViewColor"] = _hex_color(kwargs.get("color"))
        if action == "rename" and kwargs.get("color") not in (None, ""):
            setting["calendarViewColor"] = _hex_color(kwargs.get("color"))
        data = self.call(client, "POST", "/api/v1/calendars/calendar", json={"setting": setting})
        fresh = next((c for c in self._calendars(client) if c.get("id") == cal_id), None)
        verb = {"rename": "renamed", "color": "recolored"}.get(action, action)
        return format_success_response(
            f"{verb} calendar {cal_id}",
            action=action,
            calendar=_slim_calendar(fresh) if fresh else {"id": cal_id},
            api=data,
        )


class ManageInternetCalendarsTool(_CalContactTool):
    """Subscribe to an ICS URL (internet calendar)."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_internet_calendars",
            "List, test, subscribe, update, or unsubscribe SmarterMail internet "
            "calendars (ICS URL). subscribe POSTs calendars/internet-calendars-put.",
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "test", "subscribe", "update", "unsubscribe"],
                    "description": "list internet calendars; test a URL; subscribe/update/unsubscribe.",
                },
                "calendar_id": {
                    "type": "string",
                    "description": "Internet-calendar guid (update/unsubscribe).",
                },
                "url": {
                    "type": "string",
                    "description": "ICS URL (test/subscribe/update).",
                },
                "name": {
                    "type": "string",
                    "description": "Display name (subscribe/update).",
                },
                "interval": {
                    "type": "integer",
                    "description": "Refresh interval in minutes. Default 60.",
                },
            },
            required=["action"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs, ("list", "test", "subscribe", "update", "unsubscribe"))
        if action == "list":
            cals = [
                _slim_calendar(c) for c in self._calendars(client)
                if c.get("internetCalendarLink") or c.get("isWebCalendar")
            ]
            return format_success_response(
                f"{len(cals)} internet calendar(s)",
                action=action,
                calendars=cals,
            )
        if action == "test":
            self.require(kwargs, "url")
            data = self.call(
                client,
                "POST",
                "/api/v1/calendars/internet-calendars-test",
                json=str(kwargs["url"]).strip(),
            )
            return format_success_response(
                "ICS URL is reachable",
                action=action,
                url=str(kwargs["url"]).strip(),
                api=data,
            )
        if action == "subscribe":
            self.require(kwargs, "url", "name")
            interval = _as_int(kwargs.get("interval") or 60, "interval")
            data = self.call(
                client,
                "POST",
                "/api/v1/calendars/internet-calendars-put",
                json={
                    "id": _ZERO_GUID,
                    "name": str(kwargs["name"]),
                    "url": str(kwargs["url"]).strip(),
                    "interval": interval,
                    "intervalOverride": False,
                },
            )
            return format_success_response(
                f"subscribed {kwargs['name']}",
                action=action,
                calendar_id=data.get("result"),
                name=str(kwargs["name"]),
                url=str(kwargs["url"]).strip(),
                interval=interval,
            )
        cal = self._resolve_calendar(client, kwargs, required=True)
        cal_id = cal["id"]
        if action == "unsubscribe":
            data = self.call(client, "POST", f"/api/v1/calendars/calendar-delete/{cal_id}")
            return format_success_response(
                f"unsubscribed {cal.get('name')}",
                action=action,
                calendar_id=cal_id,
                api=data,
            )
        url = str(kwargs.get("url") or cal.get("internetCalendarLink") or "").strip()
        if not url:
            raise ValidationError("url required")
        name = str(kwargs.get("name") or cal.get("name") or "Internet calendar")
        interval = _as_int(
            kwargs.get("interval") if kwargs.get("interval") not in (None, "") else (cal.get("internetCalendarInterval") or 60),
            "interval",
        )
        data = self.call(
            client,
            "POST",
            "/api/v1/calendars/internet-calendars-put",
            json={
                "id": cal_id,
                "name": name,
                "url": url,
                "interval": interval,
            },
        )
        return format_success_response(
            f"updated internet calendar {name}",
            action=action,
            calendar_id=cal_id,
            name=name,
            url=url,
            interval=interval,
            api=data,
        )


class ImportExportCalendarTool(_CalContactTool):
    """Import an ICS file or export events as ICS bytes to data_dir."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "import_export_calendar",
            "Import an ICS file into a SmarterMail calendar, or export events "
            "as ICS (written under data_dir/smartermail_exports). Export takes "
            "event_ids and/or a date range on calendar_id (default primary).",
            {
                "action": {
                    "type": "string",
                    "enum": ["import", "export"],
                    "description": "import an ICS, or export events as ICS.",
                },
                "calendar_id": {
                    "type": "string",
                    "description": "Target/source calendar guid. Default: primary.",
                },
                "ics_path": {
                    "type": "string",
                    "description": "Path to an .ics file (import).",
                },
                "ics_content": {
                    "type": "string",
                    "description": "Raw ICS text (import; used if ics_path omitted).",
                },
                "event_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "SmarterMail event ids to export. Default: all in the date range.",
                },
                "start_date": {
                    "type": "string",
                    "description": "Export range start (ISO). Default: now-7d.",
                },
                "end_date": {
                    "type": "string",
                    "description": "Export range end (ISO). Default: now+90d.",
                },
            },
            required=["action"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs, ("import", "export"))
        owner = _owner_user(client)
        cal = self._resolve_calendar(client, kwargs)
        cal_id = cal["id"]

        if action == "import":
            content, filename = self._read_ics(kwargs)
            uploaded = self._upload(
                client,
                context="calendar-ics",
                context_data={"calId": cal_id, "owner": owner},
                filename=filename,
                content=content,
                content_type="text/calendar",
            )
            import_key = uploaded.get("importKey")
            if not import_key:
                raise ToolExecutionError(
                    f"calendar ICS upload returned no importKey: {uploaded}"
                )
            data = self.call(
                client,
                "POST",
                f"/api/v1/calendars/calendar-start-import/{owner}/{cal_id}",
                json={"importKey": import_key},
            )
            return format_success_response(
                f"imported ICS into {cal.get('name')}",
                action=action,
                calendar_id=cal_id,
                import_key=import_key,
                event_count=uploaded.get("eventCount"),
                api=data,
            )

        now = datetime.now(timezone.utc)
        start = kwargs.get("start_date") or (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = kwargs.get("end_date") or (now + timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ids = [_as_int(x, "event_id") for x in _as_list(kwargs.get("event_ids"))]
        if not ids:
            events = self._list_events(client, owner, cal_id, start, end)
            ids = sorted({e.get("id") for e in events if e.get("id") is not None})
        if not ids:
            raise ToolExecutionError("no events to export in that range")
        payload = [f"{owner}|{cal_id}|{eid}" for eid in ids]
        raw = self._download(
            client, "POST", "/api/v1/calendars/export/download", json=payload,
        )
        if not raw:
            raise ToolExecutionError("calendars/export/download returned an empty body")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"calendar-{cal_id[:8]}-{stamp}.ics"
        path, nbytes = _write_export(self, raw, filename)
        return format_success_response(
            f"exported {len(ids)} event(s) ({nbytes} bytes)",
            action=action,
            path=path,
            bytes=nbytes,
            filename=filename,
            event_ids=ids,
            calendar_id=cal_id,
        )

    @staticmethod
    def _read_ics(kwargs: Dict[str, Any]) -> Tuple[bytes, str]:
        path = kwargs.get("ics_path")
        if path not in (None, ""):
            p = Path(str(path))
            if not p.is_file():
                raise ValidationError(f"ics_path not found: {p}")
            return p.read_bytes(), p.name
        content = kwargs.get("ics_content")
        if content in (None, ""):
            raise ValidationError("ics_path or ics_content required")
        if isinstance(content, bytes):
            return content, "import.ics"
        return str(content).encode("utf-8"), "import.ics"


# ---------------------------------------------------------------------------
# Address books + contacts bulk
# ---------------------------------------------------------------------------


class ManageAddressBooksTool(_CalContactTool):
    """List / create / rename / delete contact folders (address books)."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_address_books",
            "List, create, rename, or delete SmarterMail address books "
            "(contact folders). id is the folderGuid / itemID from list. "
            "The Global Address List cannot be modified.",
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "create", "rename", "delete"],
                    "description": "list address books; create/rename/delete one.",
                },
                "address_book_id": {
                    "type": "string",
                    "description": "Address-book guid (rename/delete).",
                },
                "name": {
                    "type": "string",
                    "description": "Address-book name (create/rename).",
                },
            },
            required=["action"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs, ("list", "create", "rename", "delete"))
        if action == "list":
            books = [_slim_ab(s) for s in self._address_books(client)]
            return format_success_response(
                f"{len(books)} address book(s)",
                action=action,
                address_books=books,
            )
        if action == "create":
            self.require(kwargs, "name")
            data = self.call(
                client,
                "POST",
                "/api/v1/contacts/address-book/add",
                json={"folder": str(kwargs["name"])},
            )
            return format_success_response(
                f"created address book {kwargs['name']}",
                action=action,
                address_book_id=data.get("folderGuid"),
                folder_id=data.get("folderId"),
                name=str(kwargs["name"]),
            )
        src = self._resolve_ab(client, kwargs)
        uid = src.get("itemID") or src.get("id")
        if action == "rename":
            self.require(kwargs, "name")
            data = self.call(
                client,
                "POST",
                "/api/v1/contacts/address-book/edit",
                json={"folder": str(kwargs["name"]), "uid": uid},
            )
            return format_success_response(
                f"renamed address book to {kwargs['name']}",
                action=action,
                address_book_id=uid,
                name=str(kwargs["name"]),
                api=data,
            )
        if src.get("isPrimary"):
            raise ValidationError("refusing to delete the primary address book")
        data = self.call(
            client,
            "POST",
            "/api/v1/contacts/address-book/delete",
            json={"uid": uid},
        )
        return format_success_response(
            f"deleted address book {src.get('displayName')}",
            action=action,
            address_book_id=uid,
            api=data,
        )


class ManageContactsBulkTool(_CalContactTool):
    """List / get / export / import / bulk-delete contacts. Not single-contact CRUD."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_contacts_bulk",
            "List, fetch, vCard-export, vCard-import, or bulk-delete SmarterMail "
            "contacts. Does not replace create_contact/update_contact/delete_contact "
            "(those are EWS single-contact CRUD). contact_ids are hex guids from list. "
            "Export writes under data_dir/smartermail_exports.",
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "get", "export", "import", "delete"],
                    "description": "list/get contacts; export vCard; import vCard; bulk-delete.",
                },
                "address_book_id": {
                    "type": "string",
                    "description": "Address-book guid. Default: primary Contacts.",
                },
                "contact_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Contact guids (get/export/delete). Export defaults to all in the book.",
                },
                "search": {
                    "type": "string",
                    "description": "Optional list filter string.",
                },
                "vcard_path": {
                    "type": "string",
                    "description": "Path to a .vcf file (import).",
                },
                "vcard_content": {
                    "type": "string",
                    "description": "Raw vCard text (import; used if vcard_path omitted).",
                },
            },
            required=["action"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs, ("list", "get", "export", "import", "delete"))
        src = self._resolve_ab(client, kwargs, allow_gal=(action in ("list", "get", "export")))
        item_id = src.get("itemID") or src.get("id")
        folder_id = src.get("folderId")
        owner_user = src.get("ownerUsername") or _owner_user(client)
        ids = [str(x).strip() for x in _as_list(kwargs.get("contact_ids")) if str(x).strip()]

        if action == "list":
            rows = [_slim_contact(c) for c in self._list_contacts(client, src, kwargs.get("search"))]
            return format_success_response(
                f"{len(rows)} contact(s) in {src.get('displayName')}",
                action=action,
                address_book_id=item_id,
                contacts=rows,
            )

        if action == "get":
            if not ids:
                raise ValidationError("contact_ids required")
            data = self.call(
                client,
                "POST",
                "/api/v1/contacts/fetch-many",
                json=[
                    {"id": cid, "sourceId": item_id, "sourceOwner": owner_user}
                    for cid in ids
                ],
            )
            contacts = [_slim_contact(c) for c in (data.get("contacts") or []) if c]
            return format_success_response(
                f"{len(contacts)} contact(s)",
                action=action,
                contacts=contacts,
            )

        if action == "delete":
            if not ids:
                raise ValidationError("contact_ids required")
            payload = [
                {"id": cid, "sourceId": item_id, "sourceOwner": owner_user}
                for cid in ids
            ]
            data = self.call(client, "POST", "/api/v1/contacts/delete-bulk", json=payload)
            return format_success_response(
                f"deleted {len(ids)} contact(s)",
                action=action,
                contact_ids=ids,
                address_book_id=item_id,
                api=data,
            )

        if action == "export":
            if not ids:
                ids = [c.get("id") for c in self._list_contacts(client, src) if c.get("id")]
            if not ids:
                raise ToolExecutionError("no contacts to export")
            # sourceOwner in the export body is the email (live export used admin).
            export_owner = owner_user
            payload = {
                "contacts": [
                    {
                        "id": cid,
                        "hasAttachments": False,
                        "sourceOwner": export_owner,
                        "sourceId": item_id,
                    }
                    for cid in ids
                ]
            }
            raw = self._download(
                client, "POST", "/api/v1/contacts/export/vcard/download", json=payload,
            )
            if not raw:
                raise ToolExecutionError("contacts/export/vcard/download returned an empty body")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            ext = ".zip" if raw[:2] == b"PK" else ".vcf"
            filename = f"contacts-{len(ids)}-{stamp}{ext}"
            path, nbytes = _write_export(self, raw, filename)
            return format_success_response(
                f"exported {len(ids)} contact(s) ({nbytes} bytes)",
                action=action,
                path=path,
                bytes=nbytes,
                filename=filename,
                contact_ids=ids,
                address_book_id=item_id,
            )

        # import
        content, filename = self._read_vcf(kwargs)
        if folder_id in (None, "", -1):
            raise ValidationError("cannot import into this address book (no folderId)")
        uploaded = self._upload(
            client,
            context="contact-import",
            context_data={"source": owner_user},
            filename=filename,
            content=content,
            content_type="text/vcard",
        )
        parsed = uploaded.get("contacts") or []
        if not parsed:
            raise ToolExecutionError(
                f"contact vCard upload returned no contacts: {list(uploaded.keys())}"
            )
        data = self.call(
            client,
            "POST",
            f"/api/v1/contacts/contacts-put/{owner_user}/{folder_id}",
            json={"skipSignalR": True, "contacts": parsed},
        )
        created = [_slim_contact(c) for c in (data.get("contacts") or []) if c]
        return format_success_response(
            f"imported {len(created)} contact(s) into {src.get('displayName')}",
            action=action,
            address_book_id=item_id,
            contacts=created,
        )

    @staticmethod
    def _read_vcf(kwargs: Dict[str, Any]) -> Tuple[bytes, str]:
        path = kwargs.get("vcard_path")
        if path not in (None, ""):
            p = Path(str(path))
            if not p.is_file():
                raise ValidationError(f"vcard_path not found: {p}")
            return p.read_bytes(), p.name
        content = kwargs.get("vcard_content")
        if content in (None, ""):
            raise ValidationError("vcard_path or vcard_content required")
        if isinstance(content, bytes):
            return content, "import.vcf"
        return str(content).encode("utf-8"), "import.vcf"


SMARTERMAIL_TOOLS = [
    ManageRecurringEventTool,
    UpdateEventOccurrenceTool,
    ManageCalendarsTool,
    ManageInternetCalendarsTool,
    ImportExportCalendarTool,
    ManageAddressBooksTool,
    ManageContactsBulkTool,
]
