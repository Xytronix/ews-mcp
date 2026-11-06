"""Graph personal contacts; directory and communication history are separate APIs."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit

from .base import MailBackendError

_CONTACT_SELECT = (
    "id,displayName,givenName,surname,emailAddresses,businessPhones,homePhones,"
    "mobilePhone,companyName,jobTitle,department,officeLocation,categories"
)
_CONTACT_FIELDS = {
    "given_name": "givenName", "surname": "surname", "company": "companyName",
    "job_title": "jobTitle", "department": "department", "categories": "categories",
}
_MAX_CONTACT_PAGES = 20


def _contact_payload(contact: dict[str, Any]) -> dict[str, Any]:
    emails = contact.get("emailAddresses") or []
    name = contact.get("displayName") or " ".join(
        p for p in (contact.get("givenName"), contact.get("surname")) if p
    )
    phones = [
        {"type": kind, "number": number}
        for kind, key in (("BusinessPhone", "businessPhones"), ("HomePhone", "homePhones"))
        for number in contact.get(key) or []
    ]
    if contact.get("mobilePhone"):
        phones.append({"type": "MobilePhone", "number": contact["mobilePhone"]})
    return {
        "item_id": contact.get("id"), "name": name, "display_name": name,
        "given_name": contact.get("givenName") or "", "surname": contact.get("surname") or "",
        "email": emails[0].get("address", "") if emails else "",
        "email_addresses": emails, "phone_numbers": phones,
        "company": contact.get("companyName") or "", "job_title": contact.get("jobTitle") or "",
        "department": contact.get("department") or "", "office": contact.get("officeLocation") or "",
        "categories": contact.get("categories") or [], "sources": ["contacts"],
    }


class GraphContactsMixin:
    """Uses GraphBackend's existing authenticated transport without adding scopes."""

    def _contact_request(self, method: str, path: str, **kwargs):
        try:
            return self._request(method, path, **kwargs)
        except MailBackendError as exc:
            message = str(exc)
            if any(marker in message for marker in (
                "HTTP 403", "HTTP 401", "invalid_scope", "invalid_grant", "consent_required",
            )):
                # The shared transport's default remedy is mail-specific. Preserve
                # the provider error but replace that remedy for the contacts API.
                message = message.split(" The token is valid but lacks permission", 1)[0]
                raise MailBackendError(
                    "re-consent Contacts.ReadWrite; add https://graph.microsoft.com/Contacts.ReadWrite "
                    "to account.settings.graph_scope (keep Mail scopes/offline_access). " + message,
                    details={
                        **exc.details, "required_scope": "Contacts.ReadWrite",
                        "scope_setting": "account.settings.graph_scope",
                        "scope_to_add": "https://graph.microsoft.com/Contacts.ReadWrite",
                        "provider_error": message,
                        "permission_guidance": (
                            "Re-consent Contacts.ReadWrite and update the stored OAuth credentials. "
                            "Add https://graph.microsoft.com/Contacts.ReadWrite to account.settings.graph_scope "
                            "alongside existing Mail scopes and offline_access; re-consent alone does not "
                            "change scopes requested during refresh. Shared mailboxes also require the "
                            "corresponding permission and mailbox grant."
                        ),
                    },
                ) from exc
            raise

    def _contact_path(self, item_id: str | None = None) -> str:
        path = f"{self._resource_prefix()}/contacts"
        if item_id is not None:
            if not isinstance(item_id, str) or not item_id.strip():
                raise MailBackendError("item_id is required")
            path += "/" + quote(item_id, safe="")
        return path

    def create_contact(self, **fields) -> dict[str, Any]:
        name = " ".join(p for p in (fields.get("given_name"), fields.get("surname")) if p)
        body = {remote: fields[local] for local, remote in _CONTACT_FIELDS.items() if local in fields}
        body["displayName"] = name
        body["emailAddresses"] = [{"address": fields["email_address"], "name": name}]
        if fields.get("phone_number"):
            body["businessPhones"] = [fields["phone_number"]]
        contact = self._contact_request("POST", self._contact_path(), json=body)
        if not isinstance(contact, dict) or not contact.get("id"):
            raise MailBackendError("Graph created contact response did not contain an item id")
        return _contact_payload(contact)

    def update_contact(self, item_id: str, **fields) -> dict[str, Any]:
        path = self._contact_path(item_id)
        body = {remote: fields[local] for local, remote in _CONTACT_FIELDS.items() if local in fields}
        merge_fields = {"given_name", "surname", "email_address", "phone_number"}
        if merge_fields.intersection(fields):
            current = self._contact_request("GET", path, params={"$select": _CONTACT_SELECT})
            if "given_name" in fields or "surname" in fields:
                body["displayName"] = " ".join(p for p in (
                    fields.get("given_name", current.get("givenName")),
                    fields.get("surname", current.get("surname")),
                ) if p)
            if "email_address" in fields:
                emails = list(current.get("emailAddresses") or [])
                # The public schema exposes one primary address, not a replacement
                # collection. Keep every secondary address and its display name.
                primary = dict(emails[0]) if emails else {}
                rest = emails[1:]
                if fields["email_address"]:
                    primary["address"] = fields["email_address"]
                    body["emailAddresses"] = [primary, *rest]
                else:
                    body["emailAddresses"] = rest
            if "phone_number" in fields:
                phones = list(current.get("businessPhones") or [])
                body["businessPhones"] = (
                    [fields["phone_number"], *phones[1:]] if fields["phone_number"] else phones[1:]
                )
        if body:
            self._contact_request("PATCH", path, json=body)
        else:
            # A no-op still verifies that the requested contact exists.
            self._contact_request("GET", path, params={"$select": "id"})
        return {"item_id": item_id}

    def delete_contact(self, item_id: str) -> dict[str, Any]:
        self._contact_request("DELETE", self._contact_path(item_id))
        return {"item_id": item_id}

    def find_person(
        self, query: str = "", source: str = "all", max_results: int = 50,
        include_stats: bool = True, time_range_days: int = 365,
    ) -> dict[str, Any]:
        if source not in {"all", "contacts"}:
            explanation = (
                "GAL search is not implemented: Microsoft Graph directory lookup requires a "
                "work/school tenant and separately consented directory permissions; personal "
                "Outlook accounts do not provide a tenant GAL."
                if source == "gal" else
                "Email-history/domain discovery is not implemented for Graph; contact records "
                "are not a substitute for scanning mail or a tenant directory."
            )
            raise MailBackendError(explanation + " Use source='contacts' or 'all' for personal contacts.",
                                   details={"unsupported_sources": [source]})
        if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= 1000:
            raise MailBackendError("max_results must be between 1 and 1000")
        query = query.strip()
        needle = query.casefold()
        path = self._contact_path()
        params = {"$select": _CONTACT_SELECT, "$top": 100}
        results = []
        truncated = False
        pages = 0
        seen = set()
        while path:
            if path in seen:
                raise MailBackendError("Graph returned a repeated contacts paging link")
            seen.add(path)
            page = self._contact_request("GET", path, params=params)
            pages += 1
            contacts = page.get("value") or []
            next_link = page.get("@odata.nextLink")
            for index, contact in enumerate(contacts):
                searchable = [contact.get(key) or "" for key in (
                    "displayName", "givenName", "surname", "companyName", "jobTitle", "department",
                )]
                searchable.extend(entry.get("address") or "" for entry in contact.get("emailAddresses") or [])
                if needle and not any(needle in text.casefold() for text in searchable):
                    continue
                results.append(_contact_payload(contact))
                if len(results) >= max_results:
                    truncated = bool(next_link) or index + 1 < len(contacts)
                    break
            if len(results) >= max_results or not next_link:
                break
            if pages >= _MAX_CONTACT_PAGES:
                truncated = True
                break
            # Never forward the bearer token to a different origin/resource via
            # an untrusted continuation link. Graph returns absolute nextLinks.
            expected = urlsplit(f"{self._base_url()}/v1.0{self._contact_path()}")
            following = urlsplit(next_link)
            if (following.scheme, following.netloc, following.path) != (
                expected.scheme, expected.netloc, expected.path,
            ) or following.fragment:
                raise MailBackendError("Graph returned an unsafe contacts paging link")
            path, params = next_link, None
        limitations = ["Only the default personal contacts folder is searched; other contact folders are not included."]
        unsupported = ["gal", "email_history"] if source == "all" else []
        if unsupported:
            limitations.append("source='all' searches personal contacts only, not GAL or email history.")
        if include_stats:
            limitations.append("Communication statistics are unavailable from contact records; no email history was scanned.")
        if truncated:
            limitations.append("Results were limited by max_results or the 2000-contact scan bound; more matches may exist.")
        result = {
            "query": query, "source": source, "total_results": len(results),
            "unified_results": results, "searched_sources": ["contacts"],
            "unsupported_sources": unsupported, "stats_included": False,
            "limitations": limitations, "truncated": truncated,
            "partial": bool(unsupported) or include_stats or truncated,
        }
        if not query:
            result.update(contacts=results, count=len(results))
        return result
