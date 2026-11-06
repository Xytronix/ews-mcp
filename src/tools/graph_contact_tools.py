"""Existing contact names routed to Graph, with unchanged Exchange fallbacks."""
from __future__ import annotations

import re

from ..mail_backends.base import MailBackendError
from ..models import CreateContactRequest
from .contact_intelligence_tools import (
    AnalyzeContactsTool as _ExchangeAnalyzeContactsTool,
    FindPersonTool as _ExchangeFindPersonTool,
)
from .contact_tools import (
    CreateContactTool as _ExchangeCreateContactTool,
    DeleteContactTool as _ExchangeDeleteContactTool,
    UpdateContactTool as _ExchangeUpdateContactTool,
)
from .mailbox_tools import RoutedMailTool


def _require_contacts(account):
    if account.kind not in {"graph", "imap"}:
        raise MailBackendError("Contacts require Exchange, Graph, or a CardDAV-capable IMAP mailbox")


def _require_graph(account):
    if account.kind != "graph":
        raise MailBackendError(
            "analyze_contacts is unavailable for CardDAV/iCloud: contact records do not contain "
            "communication history or network statistics. Use find_person with source='contacts'.",
            details={"unsupported_operation": "analyze_contacts"},
        )


def _item_id(kwargs):
    value = kwargs.get("item_id")
    if not isinstance(value, str) or not value.strip():
        raise MailBackendError("item_id is required")
    return value


class CreateContactTool(RoutedMailTool, _ExchangeCreateContactTool):
    async def execute_routed(self, account, backend, **kwargs):
        _require_contacts(account)
        full_name = kwargs.get("full_name")
        if full_name and not kwargs.get("given_name") and not kwargs.get("surname"):
            parts = re.split(r"\s+", str(full_name).strip(), maxsplit=1)
            kwargs["given_name"] = parts[0]
            kwargs["surname"] = parts[1] if len(parts) > 1 else ""
        request = self.validate_input(CreateContactRequest, **kwargs)
        fields = request.model_dump(exclude_none=True)
        if "categories" in kwargs:
            fields["categories"] = kwargs["categories"]
        result = await self.call(backend.create_contact, **fields)
        return self.routed_response("Contact created successfully", account, result)


class UpdateContactTool(RoutedMailTool, _ExchangeUpdateContactTool):
    async def execute_routed(self, account, backend, **kwargs):
        _require_contacts(account)
        fields = {key: kwargs[key] for key in (
            "given_name", "surname", "email_address", "phone_number", "company", "job_title", "categories",
        ) if key in kwargs}
        result = await self.call(backend.update_contact, _item_id(kwargs), **fields)
        return self.routed_response("Contact updated successfully", account, result)


class DeleteContactTool(RoutedMailTool, _ExchangeDeleteContactTool):
    async def execute_routed(self, account, backend, **kwargs):
        _require_contacts(account)
        result = await self.call(backend.delete_contact, _item_id(kwargs))
        return self.routed_response("Contact deleted successfully", account, result)


class FindPersonTool(RoutedMailTool, _ExchangeFindPersonTool):
    def get_schema(self):
        schema = super().get_schema()
        schema["description"] += (
            " Graph searches the default personal contacts folder only (including source=all); "
            "GAL, email history, domain discovery, and communication statistics are unavailable. "
            "IMAP contacts require CardDAV."
        )
        schema["inputSchema"]["properties"]["query"]["description"] += (
            " For Graph, an empty query also lists contacts with source=all."
        )
        return schema

    async def execute_routed(self, account, backend, **kwargs):
        _require_contacts(account)
        result = await self.call(
            backend.find_person, query=kwargs.get("query", ""),
            source=kwargs.get("source", "all"), max_results=kwargs.get("max_results", 50),
            include_stats=kwargs.get("include_stats", True),
            time_range_days=kwargs.get("time_range_days", 365),
        )
        return self.routed_response(f"Found {result['total_results']} contacts", account, result)


class AnalyzeContactsTool(RoutedMailTool, _ExchangeAnalyzeContactsTool):
    def get_schema(self):
        schema = super().get_schema()
        schema["description"] += (
            " Exchange only: Graph mail-based analytics are not implemented; "
            "contacts records do not contain communication history or network statistics."
        )
        return schema

    async def execute_routed(self, account, backend, **kwargs):
        _require_graph(account)
        raise MailBackendError(
            "analyze_contacts is unavailable for Graph: the existing analysis scans Exchange "
            "inbox/sent mail, and no Graph mail-based analysis is implemented. The contacts "
            "API has no communication counts, timelines, VIP/dormant classification, or "
            "network statistics. Use find_person with source='contacts' for contact lookup.",
            details={"unsupported_operation": "analyze_contacts"},
        )
