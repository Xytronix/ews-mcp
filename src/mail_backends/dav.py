"""CalDAV/CardDAV support shared by IMAP-backed mailboxes.

IMAP only covers mail. iCloud exposes calendar and contacts through separate
DAV services that can reuse the same app-specific password. This module keeps
those multi-step DAV flows behind the same MailBackend methods used by Graph.
"""
from __future__ import annotations
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional
from urllib.parse import quote, urljoin, urlparse
from xml.etree import ElementTree as ET
import uuid

import httpx
from icalendar import Calendar, Event
import vobject

from .base import MailBackendError

_DAV = "DAV:"
_CALDAV = "urn:ietf:params:xml:ns:caldav"
_CARDDAV = "urn:ietf:params:xml:ns:carddav"
_DEFAULT_CALDAV = "https://caldav.icloud.com/"
_DEFAULT_CARDDAV = "https://contacts.icloud.com/"

ET.register_namespace("d", _DAV)
ET.register_namespace("c", _CALDAV)
ET.register_namespace("card", _CARDDAV)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _children_named(node: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in node.iter() if _local(child.tag) == name]


def _first_text(node: ET.Element, name: str) -> Optional[str]:
    for child in _children_named(node, name):
        if child.text:
            return child.text.strip()
    return None

def _nested_href_text(node: ET.Element, name: str) -> Optional[str]:
    for child in _children_named(node, name):
        href = _first_text(child, "href")
        if href:
            return href
    return None


def _responses(root: ET.Element) -> Iterable[ET.Element]:
    return (node for node in root.iter() if _local(node.tag) == "response")


def _response_href(response: ET.Element) -> str:
    href = _first_text(response, "href")
    if not href:
        raise MailBackendError("DAV response did not include a resource href")
    return urlparse(href).path or href


def _has_resource_type(response: ET.Element, resource: str) -> bool:
    for node in _children_named(response, "resourcetype"):
        if any(_local(child.tag) == resource for child in node):
            return True
    return False


def _parse_xml(response: httpx.Response) -> ET.Element:
    try:
        return ET.fromstring(response.text.encode("utf-8"))
    except ET.ParseError as exc:
        raise MailBackendError(f"DAV server returned malformed XML: {exc}") from exc


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise MailBackendError("datetime value must be ISO-8601 text")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ical_time(value: Any, all_day: bool = False) -> date | datetime:
    dt = _parse_datetime(value)
    return dt.date() if all_day else dt.astimezone(timezone.utc)


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "dt"):
        return _iso(value.dt)
    return str(value)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


class DAVMixin:
    """Calendar/contact operations for providers that expose DAV beside IMAP."""
    _dav_http: Optional[httpx.Client] = None
    _dav_collections: dict[str, str]

    @property
    def _dav_username(self) -> str:
        return str(self.account.get("imap_username") or self.email)

    def _dav_base(self, kind: str) -> Optional[str]:
        explicit = self.account.get(f"{kind}dav_url")
        if explicit:
            return str(explicit)
        if self.account.provider in {"icloud", "apple"}:
            return _DEFAULT_CALDAV if kind == "cal" else _DEFAULT_CARDDAV
        return None

    def has_caldav(self) -> bool:
        return bool(self._dav_base("cal"))

    def has_carddav(self) -> bool:
        return bool(self._dav_base("card"))

    def _client(self) -> httpx.Client:
        if self._dav_http is None:
            password = self.account.password
            if not password:
                raise MailBackendError(f"{self.email}: no DAV password configured")
            self._dav_http = httpx.Client(
                auth=(self._dav_username, password), timeout=60, headers={"User-Agent": "ews-mcp-dav/1"},
            )
        return self._dav_http

    def _dav_request(self, method: str, url: str, **kwargs) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        if method in {"PROPFIND", "REPORT"}:
            headers.setdefault("Content-Type", "application/xml; charset=utf-8")
        try:
            response = self._client().request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise MailBackendError(f"DAV {method} failed for {self.email}: {exc}") from exc
        if response.status_code >= 400:
            raise MailBackendError(f"DAV {method} {urlparse(url).path or url} returned HTTP {response.status_code}: {response.text[:300]}")
        return response

    def _propfind(self, url: str, body: str, depth: str = "0") -> ET.Element:
        return _parse_xml(self._dav_request("PROPFIND", url, content=body, headers={"Depth": depth}))

    def _collection_href(self, kind: str) -> str:
        if not hasattr(self, "_dav_collections"):
            self._dav_collections = {}
        if kind in self._dav_collections:
            return self._dav_collections[kind]
        base = self._dav_base(kind)
        if not base:
            proto = "CalDAV" if kind == "cal" else "CardDAV"
            raise MailBackendError(f"{proto} is not configured for {self.email}")
        base = base.rstrip("/") + "/"
        principal_xml = """<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>"""
        try:
            principal_root = self._propfind(base, principal_xml)
            principal_href = _nested_href_text(principal_root, "current-user-principal")
        except (StopIteration, MailBackendError):
            self._dav_collections[kind] = urlparse(base).path or "/"
            return self._dav_collections[kind]

        if not principal_href:
            raise MailBackendError("DAV discovery did not return current-user-principal")
        home_prop = "calendar-home-set" if kind == "cal" else "addressbook-home-set"
        home_xml = (
            '<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
            'xmlns:card="urn:ietf:params:xml:ns:carddav"><d:prop>'
            + ("<c:calendar-home-set/>" if kind == "cal" else "<card:addressbook-home-set/>")
            + "</d:prop></d:propfind>"
        )
        home_root = self._propfind(urljoin(base, principal_href), home_xml)
        home_href = _nested_href_text(home_root, home_prop)
        if not home_href:
            raise MailBackendError(f"DAV discovery did not return {home_prop}")
        resource = "calendar" if kind == "cal" else "addressbook"
        collection_xml = """<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:displayname/></d:prop></d:propfind>"""
        home_url = urljoin(base, home_href)
        root = self._propfind(home_url, collection_xml, depth="1")
        for response in _responses(root):
            if _has_resource_type(response, resource):
                href = _response_href(response)
                self._dav_collections[kind] = href if href.endswith("/") else href + "/"
                return self._dav_collections[kind]
        href = urlparse(home_url).path or home_href
        self._dav_collections[kind] = href if href.endswith("/") else href + "/"
        return self._dav_collections[kind]

    def _dav_url(self, kind: str, href: str) -> str:
        base_value = self._dav_base(kind)
        if not base_value:
            proto = "CalDAV" if kind == "cal" else "CardDAV"
            raise MailBackendError(f"{proto} is not configured for {self.email}")
        base = base_value.rstrip("/") + "/"
        return urljoin(base, href)

    # ------------------------------------------------------------------
    # CalDAV
    # ------------------------------------------------------------------
    def _event_component(self, body: str) -> Event:
        calendar = Calendar.from_ical(body)
        for component in calendar.walk("VEVENT"):
            return component
        raise MailBackendError("CalDAV resource did not contain a VEVENT")

    def _event_payload(self, href: str, etag: Optional[str], body: str) -> dict[str, Any]:
        event = self._event_component(body)
        attendees = []
        for raw in _as_list(event.get("ATTENDEE")):
            text = str(raw)
            attendees.append(text[7:] if text.upper().startswith("MAILTO:") else text)
        return {
            "item_id": href,
            "uid": str(event.get("UID") or ""),
            "etag": etag,
            "subject": str(event.get("SUMMARY") or ""),
            "start": _iso(event.get("DTSTART")),
            "end": _iso(event.get("DTEND")),
            "location": str(event.get("LOCATION") or ""),
            "organizer": str(event.get("ORGANIZER") or ""),
            "is_all_day": isinstance(getattr(event.get("DTSTART"), "dt", None), date) and not isinstance(getattr(event.get("DTSTART"), "dt", None), datetime),
            "attendees": attendees,
        }

    def _calendar_body(self, *, uid: str, subject: str, start_time: Any, end_time: Any, fields: dict[str, Any]) -> bytes:
        all_day = bool(fields.get("is_all_day"))
        calendar = Calendar()
        calendar.add("prodid", "-//Email MCP//DAV//EN")
        calendar.add("version", "2.0")
        event = Event()
        event.add("uid", uid)
        event.add("dtstamp", datetime.now(timezone.utc))
        event.add("summary", subject)
        event.add("dtstart", _ical_time(start_time, all_day))
        event.add("dtend", _ical_time(end_time, all_day))
        if fields.get("location") is not None:
            event.add("location", fields.get("location") or "")
        if fields.get("body") is not None:
            event.add("description", fields.get("body") or "")
        for category in fields.get("categories") or []:
            event.add("categories", category)
        for attendee in fields.get("attendees") or []:
            event.add("attendee", "MAILTO:" + str(attendee))
        calendar.add_component(event)
        return calendar.to_ical()

    def create_appointment(self, *, subject, start_time, end_time, **fields) -> dict[str, Any]:
        if not isinstance(subject, str) or not subject:
            raise MailBackendError("subject is required")
        uid = str(uuid.uuid4())
        collection = self._collection_href("cal")
        href = collection + quote(uid, safe="") + ".ics"
        body = self._calendar_body(uid=uid, subject=subject, start_time=start_time, end_time=end_time, fields=fields)
        response = self._dav_request("PUT", self._dav_url("cal", href), content=body, headers={"If-None-Match": "*", "Content-Type": "text/calendar; charset=utf-8"})
        return {"item_id": href, "uid": uid, "etag": response.headers.get("ETag"), "subject": subject,
                "start_time": _parse_datetime(start_time).isoformat(), "end_time": _parse_datetime(end_time).isoformat()}

    def get_calendar(self, *, start_date=None, end_date=None, days_ahead=7, max_results=50) -> dict[str, Any]:
        start = _parse_datetime(start_date) if start_date else datetime.now(timezone.utc)
        end = _parse_datetime(end_date) if end_date else start.replace(day=start.day)  # replaced below for clarity
        if end_date is None:
            from datetime import timedelta
            end = start + timedelta(days=min(max(1, days_ahead), 90))
        collection = self._collection_href("cal")
        body = f'''<?xml version="1.0"?><c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><d:getetag/><c:calendar-data/></d:prop><c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"><c:time-range start="{start.strftime('%Y%m%dT%H%M%SZ')}" end="{end.strftime('%Y%m%dT%H%M%SZ')}"/></c:comp-filter></c:comp-filter></c:filter></c:calendar-query>'''
        root = _parse_xml(self._dav_request("REPORT", self._dav_url("cal", collection), content=body, headers={"Depth": "1"}))
        events = []
        for response in _responses(root):
            href = _response_href(response)
            data = _first_text(response, "calendar-data")
            if not data:
                continue
            events.append(self._event_payload(href, _first_text(response, "getetag"), data))
            if len(events) >= max_results:
                break
        events.sort(key=lambda row: row.get("start") or "")
        return {"events": events, "start_date": start.isoformat(), "end_date": end.isoformat()}

    def _get_event_resource(self, href: str) -> tuple[str, Optional[str]]:
        response = self._dav_request("GET", self._dav_url("cal", href), headers={"Accept": "text/calendar"})
        return response.text, response.headers.get("ETag")

    def update_appointment(self, item_id, **fields) -> dict[str, Any]:
        body, etag = self._get_event_resource(item_id)
        event = self._event_component(body)
        uid = str(event.get("UID") or uuid.uuid4())
        subject = fields.get("subject", str(event.get("SUMMARY") or ""))
        start = fields.get("start_time", _iso(event.get("DTSTART")))
        end = fields.get("end_time", _iso(event.get("DTEND")))
        merged = {
            "is_all_day": fields.get("is_all_day", isinstance(getattr(event.get("DTSTART"), "dt", None), date) and not isinstance(getattr(event.get("DTSTART"), "dt", None), datetime)),
            "location": fields.get("location", str(event.get("LOCATION") or "")),
            "body": fields.get("body", str(event.get("DESCRIPTION") or "")),
        }
        if "categories" in fields:
            merged["categories"] = fields["categories"]
        response = self._dav_request("PUT", self._dav_url("cal", item_id), content=self._calendar_body(uid=uid, subject=subject, start_time=start, end_time=end, fields=merged), headers={"If-Match": etag or "*", "Content-Type": "text/calendar; charset=utf-8"})
        changed = [key for key in ("subject", "start_time", "end_time", "location", "body", "categories") if key in fields]
        return {"item_id": item_id, "uid": uid, "etag": response.headers.get("ETag"), "updated_fields": changed}

    def delete_appointment(self, item_id, *, send_cancellation=True) -> dict[str, Any]:
        self._dav_request("DELETE", self._dav_url("cal", item_id))
        return {"item_id": item_id, "cancellation_sent": False,
                "note": "CalDAV delete removed the calendar resource; meeting cancellation delivery is provider-specific and was not sent."}

    def respond_to_meeting(self, item_id, *, response, message=""):
        raise self._unsupported("respond to meeting invitations", "CalDAV attendee scheduling is not implemented")

    def check_availability(self, **kwargs):
        raise self._unsupported("check cross-attendee availability", "CalDAV has no Graph getSchedule equivalent in this backend")

    def find_meeting_times(self, **kwargs):
        raise self._unsupported("find meeting times", "CalDAV free/busy aggregation is not implemented")

    # ------------------------------------------------------------------
    # CardDAV
    # ------------------------------------------------------------------
    def _contact_href(self, uid: str) -> str:
        return self._collection_href("card") + quote(uid, safe="") + ".vcf"

    def _contact_body(self, *, uid: str, fields: dict[str, Any]) -> str:
        given = fields.get("given_name") or ""
        surname = fields.get("surname") or ""
        full_name = fields.get("full_name") or " ".join(p for p in (given, surname) if p) or fields.get("email_address") or uid
        card = vobject.vCard()
        card.add("uid").value = uid
        card.add("fn").value = full_name
        card.add("n").value = vobject.vcard.Name(family=surname, given=given)
        if fields.get("email_address"):
            email = card.add("email")
            email.value = fields["email_address"]
            email.type_param = "INTERNET"
        if fields.get("phone_number"):
            phone = card.add("tel")
            phone.value = fields["phone_number"]
            phone.type_param = "WORK"
        if fields.get("company"):
            card.add("org").value = [fields["company"]]
        if fields.get("job_title"):
            card.add("title").value = fields["job_title"]
        if fields.get("department"):
            card.add("x-department").value = fields["department"]
        return card.serialize()

    def _contact_payload(self, href: str, etag: Optional[str], body: str) -> dict[str, Any]:
        card = vobject.readOne(body)
        uid = getattr(getattr(card, "uid", None), "value", "")
        name = getattr(getattr(card, "fn", None), "value", "")
        n = getattr(getattr(card, "n", None), "value", None)
        emails = [{"address": item.value, "name": name} for item in getattr(card, "email_list", [])]
        phones = [{"type": ",".join(getattr(item, "type_paramlist", []) or []), "number": item.value} for item in getattr(card, "tel_list", [])]
        org = getattr(getattr(card, "org", None), "value", []) or []
        return {
            "item_id": href, "uid": uid, "etag": etag, "name": name, "display_name": name,
            "given_name": getattr(n, "given", "") if n else "", "surname": getattr(n, "family", "") if n else "",
            "email": emails[0]["address"] if emails else "", "email_addresses": emails, "phone_numbers": phones,
            "company": org[0] if org else "", "job_title": getattr(getattr(card, "title", None), "value", ""),
            "department": getattr(getattr(card, "x_department", None), "value", ""), "office": "",
            "categories": [], "sources": ["contacts"],
        }

    def create_contact(self, **fields) -> dict[str, Any]:
        uid = str(uuid.uuid4())
        href = self._contact_href(uid)
        response = self._dav_request("PUT", self._dav_url("card", href), content=self._contact_body(uid=uid, fields=fields).encode(), headers={"If-None-Match": "*", "Content-Type": "text/vcard; charset=utf-8"})
        body, etag = self._get_contact_resource(href)
        return self._contact_payload(href, response.headers.get("ETag") or etag, body)

    def _get_contact_resource(self, href: str) -> tuple[str, Optional[str]]:
        response = self._dav_request("GET", self._dav_url("card", href), headers={"Accept": "text/vcard"})
        return response.text, response.headers.get("ETag")

    def update_contact(self, item_id: str, **fields) -> dict[str, Any]:
        body, etag = self._get_contact_resource(item_id)
        current = self._contact_payload(item_id, etag, body)
        merged = {
            "given_name": current.get("given_name"), "surname": current.get("surname"),
            "email_address": current.get("email"), "phone_number": (current.get("phone_numbers") or [{}])[0].get("number"),
            "company": current.get("company"), "job_title": current.get("job_title"), "department": current.get("department"),
        }
        merged.update({k: v for k, v in fields.items() if v is not None})
        response = self._dav_request("PUT", self._dav_url("card", item_id), content=self._contact_body(uid=current.get("uid") or str(uuid.uuid4()), fields=merged).encode(), headers={"If-Match": etag or "*", "Content-Type": "text/vcard; charset=utf-8"})
        return {"item_id": item_id, "uid": current.get("uid"), "etag": response.headers.get("ETag"), "updated_fields": list(fields)}

    def delete_contact(self, item_id: str) -> dict[str, Any]:
        self._dav_request("DELETE", self._dav_url("card", item_id))
        return {"item_id": item_id}

    def find_person(self, query: str = "", source: str = "all", max_results: int = 50, include_stats: bool = True, time_range_days: int = 365) -> dict[str, Any]:
        if source not in {"all", "contacts"}:
            raise MailBackendError("CardDAV only searches personal contacts. Use source='contacts' or source='all'.")
        collection = self._collection_href("card")
        body = """<?xml version="1.0"?><card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav"><d:prop><d:getetag/><card:address-data/></d:prop></card:addressbook-query>"""
        root = _parse_xml(self._dav_request("REPORT", self._dav_url("card", collection), content=body, headers={"Depth": "1"}))
        needle = query.casefold().strip()
        results = []
        for response in _responses(root):
            href = _response_href(response)
            data = _first_text(response, "address-data")
            if not data:
                continue
            payload = self._contact_payload(href, _first_text(response, "getetag"), data)
            haystack = " ".join(str(payload.get(k) or "") for k in ("name", "given_name", "surname", "email", "company", "job_title"))
            if needle and needle not in haystack.casefold():
                continue
            results.append(payload)
            if len(results) >= max_results:
                break
        limitations = ["CardDAV searches personal address-book contacts only; GAL, email history, communication statistics and analytics are unavailable."]
        return {"query": query, "source": source, "total_results": len(results), "unified_results": results,
                "searched_sources": ["contacts"], "unsupported_sources": ["gal", "email_history"] if source == "all" else [],
                "stats_included": False, "limitations": limitations, "truncated": False,
                "partial": source == "all" or include_stats, "contacts": results if not query else None, "count": len(results) if not query else None}
