"""Native Graph calendar operations; no inferred attendee availability."""
from __future__ import annotations

import base64
import json
import math
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .base import MailBackendError


def _zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # Graph also returns Windows timezone IDs on existing events.
        from exchangelib import EWSTimeZone
        try:
            return EWSTimeZone.from_ms_id(name)
        except Exception as exc:
            raise MailBackendError(f"Unsupported calendar timezone: {name}") from exc


def _datetime(value):
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError) as exc:
        raise MailBackendError("Invalid calendar datetime; use ISO 8601 with an explicit offset") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zone(os.environ.get("TIMEZONE", os.environ.get("TZ", "UTC"))))
    return dt


def _graph_datetime(value):
    if not value or not value.get("dateTime"):
        raise MailBackendError("Graph returned no event dateTime")
    raw = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
    return raw if raw.tzinfo else raw.replace(tzinfo=_zone(value.get("timeZone") or "UTC"))


def _wire_time(dt):
    return {"dateTime": dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat(), "timeZone": "UTC"}


def _elapsed(start, end):
    """Elapsed time, including DST transitions when tzinfo objects are equal."""
    return end.astimezone(timezone.utc) - start.astimezone(timezone.utc)


def _advance(dt, minutes):
    return (dt.astimezone(timezone.utc) + timedelta(minutes=minutes)).astimezone(dt.tzinfo)


def _bounds(start, end):
    start, end = _datetime(start), _datetime(end)
    if _elapsed(start, end) <= timedelta(0):
        raise MailBackendError("end_time must be after start_time")
    return start, end


def _emails(values):
    result = []
    for value in values or []:
        if not isinstance(value, str) or not value.strip() or "@" not in value:
            raise MailBackendError("Attendees must be nonempty email addresses")
        email = value.strip().lower()
        if email not in result:
            result.append(email)
    if not result:
        raise MailBackendError("At least one attendee email address is required")
    return result


class GraphCalendarMixin:
    def _calendar_request(self, method, path, **kwargs):
        try:
            return self._request(method, path, **kwargs)
        except MailBackendError as exc:
            text = str(exc)
            if any(marker in text for marker in ("HTTP 401", "HTTP 403", "invalid_scope", "consent_required", "invalid_grant")):
                text = text.split(" The token is valid but lacks permission")[0]
                raise MailBackendError(
                    "re-consent Calendars.ReadWrite; add https://graph.microsoft.com/Calendars.ReadWrite "
                    "to account.settings.graph_scope (keep Mail scopes/offline_access). " + text,
                    details={
                        **exc.details, "required_scope": "Calendars.ReadWrite",
                        "scope_setting": "account.settings.graph_scope",
                        "scope_to_add": "https://graph.microsoft.com/Calendars.ReadWrite",
                        "provider_error": text,
                        "permission_guidance": (
                            "Re-consent Calendars.ReadWrite and update the stored OAuth credentials. "
                            "Add https://graph.microsoft.com/Calendars.ReadWrite to account.settings.graph_scope "
                            "alongside existing Mail scopes and offline_access; re-consent alone does not "
                            "change scopes requested during refresh. Shared calendars also require the "
                            "corresponding permission and mailbox grant."
                        ),
                    },
                ) from exc
            raise

    def _event_path(self, item_id):
        if not isinstance(item_id, str) or not item_id:
            raise MailBackendError("item_id is required")
        return f"{self._resource_prefix()}/events/{quote(item_id, safe='')}"

    def _event_times(self, start, end, all_day=False, zone_name=None):
        start, end = _bounds(start, end)
        if not all_day:
            return _wire_time(start), _wire_time(end)
        # All-day end is exclusive and both boundaries must be local midnight.
        zone_name = zone_name or os.environ.get("TIMEZONE", os.environ.get("TZ", "UTC"))
        zone = _zone(zone_name)
        start, end = start.astimezone(zone), end.astimezone(zone)
        if any((dt.hour, dt.minute, dt.second, dt.microsecond) != (0, 0, 0, 0) for dt in (start, end)):
            raise MailBackendError("All-day start/end must be midnight in configured TIMEZONE, with an exclusive end date")
        return tuple({"dateTime": dt.replace(tzinfo=None).isoformat(), "timeZone": zone_name} for dt in (start, end))

    def create_appointment(self, *, subject, start_time, end_time, **fields):
        if not isinstance(subject, str) or not subject:
            raise MailBackendError("subject is required")
        start, end = self._event_times(start_time, end_time, fields.get("is_all_day", False))
        payload = {"subject": subject, "start": start, "end": end, "isAllDay": fields.get("is_all_day", False)}
        payload.update(self._calendar_fields(fields))
        reminder = fields.get("reminder_minutes", 15)
        if reminder is not None:
            if not isinstance(reminder, int) or reminder < 0:
                raise MailBackendError("reminder_minutes must be a nonnegative integer")
            payload.update(isReminderOn=True, reminderMinutesBeforeStart=reminder)
        if fields.get("attendees"):
            payload["attendees"] = [{"emailAddress": {"address": email}, "type": "required"} for email in _emails(fields["attendees"])]
        result = self._calendar_request("POST", self._resource_prefix() + "/events", json=payload)
        return {"item_id": result["id"], "subject": subject,
                "start_time": _graph_datetime(start).isoformat(), "end_time": _graph_datetime(end).isoformat()}

    @staticmethod
    def _calendar_fields(fields):
        payload = {}
        if "subject" in fields:
            payload["subject"] = fields["subject"]
        if "location" in fields:
            payload["location"] = {"displayName": fields["location"] or ""}
        if "body" in fields:
            payload["body"] = {"contentType": "text", "content": fields["body"] or ""}
        if "categories" in fields:
            payload["categories"] = list(fields["categories"] or [])
        return payload

    def get_calendar(self, *, start_date=None, end_date=None, days_ahead=7, max_results=50):
        start = _datetime(start_date) if start_date else datetime.now(timezone.utc)
        end = _datetime(end_date) if end_date else start + timedelta(days=min(max(1, days_ahead), 90))
        if isinstance(end_date, str) and len(end_date) == 10:
            end += timedelta(days=1)
        _bounds(start, end)
        if not isinstance(max_results, int) or not 1 <= max_results <= 1000:
            raise MailBackendError("max_results must be between 1 and 1000")
        path = self._resource_prefix() + "/calendarView"
        params = {"startDateTime": start.isoformat(), "endDateTime": end.isoformat(), "$orderby": "start/dateTime",
                  "$top": min(max_results, 1000), "$select": "id,subject,start,end,location,organizer,isAllDay,attendees"}
        events, visited = [], set()
        while path and len(events) < max_results:
            if path in visited:
                raise MailBackendError("Graph returned a repeated calendar pagination link")
            visited.add(path)
            page = self._calendar_request("GET", path, params=params)
            for item in page.get("value", []):
                events.append({"item_id": item["id"], "subject": item.get("subject") or "",
                    "start": _graph_datetime(item["start"]).isoformat(), "end": _graph_datetime(item["end"]).isoformat(),
                    "location": (item.get("location") or {}).get("displayName") or "",
                    "organizer": ((item.get("organizer") or {}).get("emailAddress") or {}).get("address") or "",
                    "is_all_day": item.get("isAllDay", False),
                    "attendees": [a["emailAddress"]["address"] for a in item.get("attendees") or [] if (a.get("emailAddress") or {}).get("address")]})
                if len(events) == max_results:
                    break
            path = page.get("@odata.nextLink")
            if path and (urlparse(path).scheme, urlparse(path).netloc) != (urlparse(self._base_url()).scheme, urlparse(self._base_url()).netloc):
                raise MailBackendError("Graph returned an untrusted calendar pagination link")
            params = None
        return {"events": events, "start_date": start.isoformat(), "end_date": end.isoformat()}

    def update_appointment(self, item_id, **fields):
        path = self._event_path(item_id)
        payload = self._calendar_fields(fields)
        if "start_time" in fields or "end_time" in fields:
            zone_name = os.environ.get("TIMEZONE", os.environ.get("TZ", "UTC"))
            _zone(zone_name)
            current = self._calendar_request("GET", path, params={"$select": "start,end,isAllDay"},
                request_headers={"Prefer": f'outlook.timezone="{zone_name}"'})
            start, end = self._event_times(fields.get("start_time", _graph_datetime(current["start"])),
                fields.get("end_time", _graph_datetime(current["end"])), current.get("isAllDay", False),
                zone_name)
            if "start_time" in fields:
                payload["start"] = start
            if "end_time" in fields:
                payload["end"] = end
        if payload:
            self._calendar_request("PATCH", path, json=payload)
        return {"item_id": item_id, "updated_fields": list(payload)}

    def delete_appointment(self, item_id, *, send_cancellation=True):
        path = self._event_path(item_id)
        current = self._calendar_request("GET", path, params={"$select": "isOrganizer,attendees"})
        meeting = bool(current.get("attendees"))
        organizer = current.get("isOrganizer")
        if meeting and organizer is None:
            raise MailBackendError("Graph did not identify the organizer; refusing to guess cancellation behavior")
        if meeting and organizer:
            if not send_cancellation:
                raise MailBackendError("Graph cannot suppress cancellation when deleting an organizer's meeting; use send_cancellation=true")
            self._calendar_request("POST", path + "/cancel", json={"comment": ""})
            return {"item_id": item_id, "cancellation_sent": True}
        self._calendar_request("DELETE", path)
        return {"item_id": item_id, "cancellation_sent": False}

    def respond_to_meeting(self, item_id, *, response, message=""):
        action = {"Accept": "accept", "Tentative": "tentativelyAccept", "Decline": "decline"}.get(response)
        if not action:
            raise MailBackendError("response must be Accept, Tentative, or Decline")
        path = self._event_path(item_id)
        event_id = item_id
        try:
            self._calendar_request("POST", path + "/" + action, json={"comment": message, "sendResponse": True})
        except MailBackendError as exc:
            if "HTTP 404" not in str(exc):
                raise
            # Inbox eventMessageRequest IDs differ from the associated event ID.
            event = self._calendar_request("GET", f"{self._resource_prefix()}/messages/{quote(item_id, safe='')}/event")
            event_id = event["id"]
            self._calendar_request("POST", self._event_path(event_id) + "/" + action,
                                   json={"comment": message, "sendResponse": True})
        return {"item_id": item_id, "event_id": event_id, "response": response}

    def _personal_calendar_account(self):
        tenant = str(self._account.get("tenant_id") or "").lower()
        consumer = "9188040d-6c67-4c5b-b112-36a304b66dad"
        if tenant in ("consumers", consumer):
            return True
        # Only inspect the already validated token's tenant hint, never log it.
        try:
            payload = (self._access_token or "").split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            return claims.get("tid") == consumer
        except (ValueError, IndexError, TypeError):
            return False

    def check_availability(self, *, email_addresses, start_time, end_time, interval_minutes=30, include_self=True):
        start, end = _bounds(start_time, end_time)
        if not isinstance(interval_minutes, int) or not 15 <= interval_minutes <= 1440:
            raise MailBackendError("interval_minutes must be between 15 and 1440")
        emails = _emails(email_addresses)
        if include_self and self.email.lower() not in emails:
            emails.insert(0, self.email.lower())
        if len(emails) > 20 or _elapsed(start, end) >= timedelta(days=62):
            raise MailBackendError("Graph getSchedule supports at most 20 schedules and a range shorter than 62 days")
        if self._personal_calendar_account():
            raise MailBackendError("Graph getSchedule is unsupported for personal Microsoft accounts; cross-attendee availability cannot be determined")
        try:
            result = self._calendar_request("POST", self._resource_prefix() + "/calendar/getSchedule", json={
                "schedules": emails, "startTime": _wire_time(start), "endTime": _wire_time(end),
                "availabilityViewInterval": interval_minutes})
        except MailBackendError as exc:
            raise MailBackendError(str(exc) + " getSchedule requires a work/school Microsoft 365 mailbox; personal accounts are unsupported.", details=exc.details) from exc
        count = math.ceil(_elapsed(start, end).total_seconds() / (60 * interval_minutes))
        by_email = {row.get("scheduleId", "").lower(): row for row in result.get("value", [])}
        rows = []
        for email in emails:
            raw = by_email.get(email, {})
            error = raw.get("error")
            view = raw.get("availabilityView") or ""
            merged = "".join(c if c in "0123" else "4" for c in view[:count]).ljust(count, "4")
            # Microsoft documents workingElsewhere as 0 in availabilityView;
            # 4 is reserved here for missing/unknown data, as in EWS responses.
            if error:
                merged = "4" * count
            events = []
            for item in raw.get("scheduleItems") or []:
                events.append({"start": _graph_datetime(item["start"]).astimezone(start.tzinfo).isoformat(),
                    "end": _graph_datetime(item["end"]).astimezone(start.tzinfo).isoformat(),
                    "busy_type": {"free": "Free", "tentative": "Tentative", "busy": "Busy", "oof": "OutOfOffice",
                                  "workingElsewhere": "WorkingElsewhere"}.get(item.get("status"), "NoData"),
                    "details": {k: item[k] for k in ("subject", "location", "isPrivate") if k in item}})
            row = {"email": email, "view_type": "DetailedMerged", "merged_free_busy": merged, "calendar_events": events}
            if error or not raw:
                row["error"] = error.get("message", str(error)) if isinstance(error, dict) else str(error or "No schedule returned")
            elif "4" in merged:
                row["error"] = "Incomplete or unknown availability data"
            rows.append(row)
        return {"availability": rows, "checked_email_addresses": emails, "include_self": include_self,
                "response_timezone": start.isoformat()[-6:],
                "time_range": {"start": start_time, "end": end_time, "interval_minutes": interval_minutes}}

    def find_meeting_times(self, *, attendees, date_range_start, date_range_end, duration_minutes=60,
                           max_suggestions=5, preferences=None):
        start, end = _bounds(date_range_start, date_range_end)
        if not isinstance(duration_minutes, int) or not 15 <= duration_minutes <= 480:
            raise MailBackendError("duration_minutes must be between 15 and 480")
        if not isinstance(max_suggestions, int) or not 1 <= max_suggestions <= 20:
            raise MailBackendError("max_suggestions must be between 1 and 20")
        prefs = preferences or {}
        earliest, latest = prefs.get("earliest_hour", 9), prefs.get("latest_hour", 17)
        gap = prefs.get("min_break_minutes", 15)
        if not all(isinstance(h, int) and 0 <= h <= 23 for h in (earliest, latest)) or earliest >= latest:
            raise MailBackendError("working hours require 0 <= earliest_hour < latest_hour <= 23")
        if not isinstance(gap, int) or gap < 0:
            raise MailBackendError("min_break_minutes must be nonnegative")
        data = self.check_availability(email_addresses=attendees, start_time=date_range_start,
            end_time=date_range_end, interval_minutes=15, include_self=False)
        views = [r["merged_free_busy"] for r in data["availability"]]
        current, suggestions = start, []
        avoid = prefs.get("avoid_back_to_back", True)
        working = prefs.get("working_hours_only", True)
        while _elapsed(_advance(current, duration_minutes), end) >= timedelta(0) and len(suggestions) < max_suggestions:
            opening = current.replace(hour=earliest, minute=0, second=0, microsecond=0)
            closing = current.replace(hour=latest, minute=0, second=0, microsecond=0)
            if working and _elapsed(current, opening) > timedelta(0):
                current = opening
            slot_end = _advance(current, duration_minutes)
            if working and _elapsed(closing, slot_end) > timedelta(0):
                current = opening + timedelta(days=1)
                continue
            if _elapsed(end, slot_end) > timedelta(0):
                break
            first = math.floor(_elapsed(start, current).total_seconds() / 900)
            last = math.ceil(_elapsed(start, slot_end).total_seconds() / 900)
            available = bool(views) and all(last <= len(view) and all(c in "01" for c in view[first:last]) for view in views)
            if available:
                buffer_first = math.floor((_elapsed(start, current).total_seconds() - gap * 60) / 900)
                buffer_last = math.ceil((_elapsed(start, slot_end).total_seconds() + gap * 60) / 900)
                buffer_free = all(buffer_first >= 0 and buffer_last <= len(view) and
                                  all(c in "01" for c in view[buffer_first:buffer_last]) for view in views)
                score = 100 + (20 if prefs.get("prefer_morning") and current.hour < 12 else 0)
                score += 20 if prefs.get("prefer_afternoon") and current.hour >= 13 else 0
                score -= 10 if current.hour < 9 or current.hour > 16 else 0
                score += 10 if avoid and buffer_free else 0
                suggestions.append({"start_time": current.isoformat(), "end_time": slot_end.isoformat(),
                    "duration_minutes": duration_minutes, "score": score, "day_of_week": current.strftime("%A"),
                    "time_of_day": "Morning" if current.hour < 12 else "Afternoon" if current.hour < 17 else "Evening"})
                current = _advance(slot_end, gap if avoid else 0)
            else:
                current = _advance(current, 15)
        suggestions.sort(key=lambda row: row["score"], reverse=True)
        return {"suggestions": suggestions, "attendees": data["checked_email_addresses"], "duration_minutes": duration_minutes,
                "date_range": {"start": date_range_start, "end": date_range_end}, "preferences": prefs,
                "availability_errors": [{"email": row["email"], "error": row["error"]}
                                        for row in data["availability"] if "error" in row]}
