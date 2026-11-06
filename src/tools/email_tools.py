"""Email operation tools for EWS MCP Server."""

import heapq
import itertools
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from exchangelib import Message, Mailbox, FileAttachment, ItemAttachment, HTMLBody, Body, Folder, ExtendedProperty
from exchangelib.errors import ErrorFolderNotFound, ErrorTimeoutExpired
from exchangelib.queryset import Q

# Define Flag as ExtendedProperty for setting email flag status
# See: https://github.com/ecederstrand/exchangelib/issues/85
# Flag values: None = not flagged, 1 = completed, 2 = flagged
class FlagStatus(ExtendedProperty):
    property_tag = 0x1090
    property_type = 'Integer'


# Register the flag property on Message class. ``outbound_tracking_id`` is
# registered in ``src.utils`` alongside the tracking header setting.
Message.register('flag_status_value', FlagStatus)

# Mapping from string flag_status to integer values
FLAG_STATUS_MAP = {
    'NotFlagged': None,
    'Flagged': 2,
    'Complete': 1,
}
_FLAG_STATUS_BY_VALUE = {value: name for name, value in FLAG_STATUS_MAP.items() if value}
import re

from .base import BaseTool
from ..models import SendEmailRequest, EmailSearchRequest, EmailDetails
from ..exceptions import ToolExecutionError, ValidationError
from ..utils import (
    iso_or_none, item_text_body, sender_matches, is_full_address,
    format_success_response, safe_get, truncate_text, parse_datetime_tz_aware,
    find_message_across_folders, find_message_for_account, ews_id_to_str,
    attach_inline_files, INLINE_ATTACHMENTS_SCHEMA,
    escape_html, format_body_for_html, sanitize_html,
    project_fields, ensure_snippet, strip_body_by_default, LIST_DEFAULT_FIELDS,
    ews_call_log, new_outbound_tracking_id,
)
from .folder_tools import find_folder_by_id, get_standard_folder_map
from .attachment_tools import build_embedded_message
from .outgoing_attachments import (
    FILES_SCHEMA, VERIFY_FILES_SCHEMA, attach_outgoing_files, dry_run_file_report,
    dry_run_schema, load_outgoing_files, validate_outgoing_files,
)
from ..attachment_sources import REMOTE_AGENT_HINT, REMOTE_AGENT_POINTER
from .smartermail_base import smartermail_client

# v4.0 — bidirectional body format schema fragments (read + write)
from ..body_format import (
    DEFAULT_READ_FORMAT,
    READ_FORMAT_SCHEMA,
    WRITE_FORMAT_SCHEMA as BODY_FORMAT_SCHEMA,  # local alias used by email tools
    render_body,
    compose_body,
    trim_quoted as _trim_quoted_body,
)


# ---------------------------------------------------------------------------
# Search pagination + field-projection helpers (Issue 2)
# ---------------------------------------------------------------------------
#
# Prior behaviour iterated ``folder.filter(...)[:max_results]`` with a bare
# ``except Exception`` around the for-loop. exchangelib's auto-pagination
# can raise mid-stream (throttling, transient EWS errors) and the old
# handler silently logged + continued, so the response looked successful
# but carried only the items delivered before the failure — hence the
# reported "wider window returns FEWER results than a subset" symptom.
#
# These helpers make the pagination explicit, bounded, and classifiable:
#   * ``_db_fields_for`` maps the public ``fields=[...]`` projection to
#     exchangelib DB field names so ``.only(*db_fields)`` can shrink the
#     EWS payload rather than just trimming it client-side.
#   * ``_query_total`` calls ``query.count()`` once so the response can
#     expose ``total_available`` (EWS ``TotalItemsInView``).
#   * ``_paginate_query`` materialises the result in explicit chunks,
#     narrowly classifies exchangelib errors, and returns a
#     ``FolderQueryOutcome`` so the caller can carry the diagnosis into
#     the response + structured log.
# ---------------------------------------------------------------------------


# Public-field -> tuple of exchangelib DB fields. Unknown public names
# map to () — in that case we skip .only() rather than crash.
_DB_FIELDS_BY_PUBLIC: Dict[str, tuple] = {
    "id": ("id",),
    "message_id": ("id",),
    "subject": ("subject",),
    "from": ("sender",),
    "sender": ("sender",),
    "to": ("to_recipients",),
    "cc": ("cc_recipients",),
    "bcc": ("bcc_recipients",),
    "received_time": ("datetime_received",),
    "received": ("datetime_received",),
    "datetime_received": ("datetime_received",),
    "sent_time": ("datetime_sent",),
    "sent": ("datetime_sent",),
    "datetime_sent": ("datetime_sent",),
    "is_read": ("is_read",),
    "has_attachments": ("has_attachments",),
    "importance": ("importance",),
    "categories": ("categories",),
    # Snippet/preview/body all derive from text_body + body; include both
    # so exchangelib fetches the HTML body too when callers ask for body.
    "snippet": ("text_body", "body"),
    "preview": ("text_body", "body"),
    "body_preview": ("text_body", "body"),
    "body": ("text_body", "body"),
    "body_html": ("body",),
    "conversation_id": ("conversation_id",),
    "thread_id": ("conversation_id",),
    "flag_status": ("flag_status_value",),
}


def _db_fields_for(public_fields: Optional[List[str]]) -> tuple:
    """Map public field names to the exchangelib DB-field tuple.

    Returns an empty tuple when the public list is empty, None, or
    contains any name we don't recognise — in that case the caller
    should skip ``.only()`` to avoid stripping a field exchangelib
    would otherwise have fetched opportunistically.
    """
    if not public_fields:
        return ()
    out: list = []
    seen: set = set()
    for name in public_fields:
        mapped = _DB_FIELDS_BY_PUBLIC.get(str(name).lower())
        if not mapped:
            # Unknown public field (e.g. "folder" which is synthesized) —
            # skip. Callers get the current permissive behaviour.
            continue
        for db_name in mapped:
            if db_name not in seen:
                seen.add(db_name)
                out.append(db_name)
    # Always include id — the tool uses it for the message_id field.
    if "id" not in seen:
        out.insert(0, "id")
    return tuple(out)


# Exchangelib error classifications. We prefer exchangelib.errors.*
# when present; fall back to a str(exception) heuristic to stay robust
# if the library renames a class.
_TRANSIENT_EWS_ERROR_NAMES = frozenset({
    "ErrorServerBusy", "ErrorTimeoutExpired", "ErrorConnectionFailed",
    "ErrorInternalServerError", "ErrorInternalServerTransientError",
    "ErrorMailboxStoreUnavailable", "ErrorMessageSizeExceeded",
    "TransportError", "RateLimitError",
})
_AUTH_EWS_ERROR_NAMES = frozenset({
    "ErrorAccessDenied", "ErrorAccessDeniedForSendingEmail",
    "UnauthorizedError", "Unauthorized",
})
_CONNECTION_EWS_ERROR_NAMES = frozenset({
    "ConnectionError", "RemoteDisconnected", "SSLError",
    "MaxRetryError", "ChunkedEncodingError",
})


def _classify_ews_error(exc: BaseException) -> str:
    """Return a short tag describing an exception: ``TIMEOUT``,
    ``THROTTLED``, ``AUTH_EXPIRED``, ``CONNECTION``, or ``UNKNOWN``.

    Used to build error_code fields in tool responses and to decide
    whether a mid-iteration exception should abort the query with an
    explicit error vs. log and continue.
    """
    name = type(exc).__name__
    msg = str(exc)
    low = msg.lower()
    if (name in ("ErrorServerBusy", "RateLimitError") or "throttl" in low
            or "ServerBusy" in msg or "429" in msg or "too many requests" in low
            or "rate limit" in low):
        return "THROTTLED"
    if name in _TRANSIENT_EWS_ERROR_NAMES or "timed out" in msg.lower():
        return "TIMEOUT"
    if name in _AUTH_EWS_ERROR_NAMES or "401" in msg or "Unauthorized" in msg:
        return "AUTH_EXPIRED"
    if name in _CONNECTION_EWS_ERROR_NAMES or "Connection aborted" in msg:
        return "CONNECTION"
    return "UNKNOWN"


# --- Throttling (EWS ErrorServerBusy / HTTP 429) ---------------------------
# Hosted SmarterMail servers can enforce roughly 25 EWS requests/minute per
# mailbox. Searches are serialized process-wide (multi-mailbox fan-out from
# one agent no longer bursts), and THROTTLED failures — raised or reported
# via meta.error_code — are retried with exponential backoff.
_SEARCH_LOCK: Optional[Any] = None
THROTTLE_MAX_RETRIES = int(os.environ.get("EWS_THROTTLE_MAX_RETRIES", "4"))
THROTTLE_BASE_DELAY = float(os.environ.get("EWS_THROTTLE_BASE_DELAY", "3"))
THROTTLE_MAX_DELAY = float(os.environ.get("EWS_THROTTLE_MAX_DELAY", "30"))


def _search_lock():
    import asyncio
    global _SEARCH_LOCK
    if _SEARCH_LOCK is None:
        _SEARCH_LOCK = asyncio.Lock()
    return _SEARCH_LOCK


def _result_throttled(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    meta = result.get("meta") or {}
    if meta.get("error_code") == "THROTTLED":
        return True
    return any(e.get("error_code") == "THROTTLED"
               for e in (meta.get("per_folder_errors") or []) if isinstance(e, dict))


def _exc_throttled(exc: BaseException) -> bool:
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if _classify_ews_error(exc) == "THROTTLED":
            return True
        exc = exc.__cause__ or exc.__context__
    return False


async def run_throttled(factory, logger: Optional[logging.Logger] = None,
                        *, sleep=None, max_retries: Optional[int] = None):
    """Run ``await factory()`` under the search lock, retrying throttling."""
    import asyncio
    sleep = sleep or asyncio.sleep
    retries = THROTTLE_MAX_RETRIES if max_retries is None else max_retries
    async with _search_lock():
        attempt = 0
        while True:
            try:
                result = await factory()
                throttled = _result_throttled(result)
                err = None
            except Exception as exc:  # noqa: BLE001
                if not _exc_throttled(exc):
                    raise
                throttled, err, result = True, exc, None
            if not throttled or attempt >= retries:
                if err is not None:
                    raise err
                return result
            delay = min(THROTTLE_MAX_DELAY, THROTTLE_BASE_DELAY * (2 ** attempt))
            attempt += 1
            if logger:
                logger.warning("EWS throttled; retry %d/%d in %.0fs", attempt, retries, delay)
            await sleep(delay)


@dataclass
class FolderQueryOutcome:
    """Result of ``_paginate_query`` for a single folder.

    ``items`` is what we actually collected (possibly partial if an
    error fired mid-iteration). ``error_code`` / ``error_message`` are
    populated on failure and passed into the response so the caller can
    tell a full-but-empty result from a truncated one.
    """
    items: List[Any] = field(default_factory=list)
    total_available: Optional[int] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


def _query_total(query: Any, logger: logging.Logger, folder_label: str) -> Optional[int]:
    """Return ``query.count()`` or None when the call fails.

    Not every exchangelib queryset supports ``.count()`` cheaply —
    treating a failure as "unknown" is strictly better than refusing
    to ship items.
    """
    count_method = getattr(query, "count", None)
    if count_method is None:
        return None
    try:
        return int(count_method())
    except Exception as exc:
        logger.debug(
            "query.count() failed for %s: %s: %s",
            folder_label, type(exc).__name__, exc,
        )
        return None


def _paginate_query(
    query: Any,
    *,
    max_results: int,
    offset: int,
    chunk_size: int,
    logger: logging.Logger,
    folder_label: str,
) -> FolderQueryOutcome:
    """Materialise the query in explicit chunks, capturing partial-failure.

    * Walks ``query[o:o+chunk_size]`` slices in a for-loop, building up
      to ``max_results`` items.
    * Unwraps any mid-iteration exception into a classified error_code
      on the outcome — prior code swallowed these and returned partial
      results as "success".
    * Also calls ``_query_total`` once up front so the response can
      advertise ``total_available``.
    """
    outcome = FolderQueryOutcome()
    outcome.total_available = _query_total(query, logger, folder_label)
    target_offset = max(0, int(offset))
    remaining = max(0, int(max_results))
    cursor = target_offset
    chunk_size = max(1, min(chunk_size, 250))
    while remaining > 0:
        want = min(chunk_size, remaining)
        try:
            batch = list(query[cursor:cursor + want])
        except Exception as exc:
            code = _classify_ews_error(exc)
            # Log full type at WARNING so ops can catch the real cause
            # (the Issue 2 diagnostic the bug report wanted to run live).
            logger.warning(
                "paginate %s offset=%d chunk=%d raised %s: %s",
                folder_label, cursor, want, type(exc).__name__, exc,
            )
            outcome.error_code = code
            outcome.error_message = f"{type(exc).__name__}: {exc}"
            return outcome
        if not batch:
            break
        outcome.items.extend(batch)
        cursor += len(batch)
        remaining -= len(batch)
        # Short batch => queryset exhausted (fewer items than requested).
        if len(batch) < want:
            break
    return outcome


def _subject_search_matcher(subject_contains: Optional[str] = None):
    """Build a local verifier for EWS subject restrictions.

    SmarterMail accepts EWS ``Contains`` restrictions but can ignore them and
    return the entire folder. Keeping the server restriction is useful on real
    Exchange; this verifier guarantees the public result still matches it.
    """
    needle = (subject_contains or "").casefold()
    if not needle:
        return None

    def matches(email: Any) -> bool:
        subject = str(safe_get(email, "subject", "") or "").casefold()
        return needle in subject

    return matches


def _with_subject_search_field(
    only_fields: tuple,
    subject_contains: Optional[str] = None,
) -> tuple:
    """Include the subject needed to verify an EWS restriction locally."""
    fields = list(only_fields)
    if subject_contains and "subject" not in fields:
        fields.append("subject")
    return tuple(fields)


def _paginate_query_matching(
    query: Any,
    *,
    matcher: Any,
    max_results: int,
    offset: int,
    chunk_size: int,
    logger: logging.Logger,
    folder_label: str,
) -> FolderQueryOutcome:
    """Scan a possibly-unrestricted EWS result and keep verified matches."""
    outcome = FolderQueryOutcome()
    target_offset = max(0, int(offset))
    limit = max(0, int(max_results))
    chunk_size = max(1, min(chunk_size, 250))
    cursor = 0
    matched = 0

    while True:
        try:
            batch = list(query[cursor:cursor + chunk_size])
        except Exception as exc:
            code = _classify_ews_error(exc)
            logger.warning(
                "paginate matching %s offset=%d chunk=%d raised %s: %s",
                folder_label, cursor, chunk_size, type(exc).__name__, exc,
            )
            outcome.error_code = code
            outcome.error_message = f"{type(exc).__name__}: {exc}"
            outcome.total_available = None
            return outcome
        if not batch:
            break
        for email in batch:
            if not matcher(email):
                continue
            if matched >= target_offset and len(outcome.items) < limit:
                outcome.items.append(email)
            matched += 1
        cursor += len(batch)
        if len(batch) < chunk_size:
            break

    outcome.total_available = matched
    return outcome


def _with_fields(only_fields: tuple, *needed: str) -> tuple:
    """Add DB fields the tool itself reads to a ``.only()`` projection.

    An empty projection already fetches every field, so it stays empty.
    """
    if not only_fields:
        return only_fields
    extra = tuple(f for f in dict.fromkeys(needed) if f not in only_fields)
    return tuple(only_fields) + extra


# Where full_text mode can look for the query text.
_FULL_TEXT_AREAS = ("subject", "body", "attachments")


def _full_text_areas(search_in: Any) -> List[str]:
    """Validate ``search_in``; an empty or unknown area would search nothing."""
    if isinstance(search_in, str):
        search_in = [search_in]
    areas = [str(area).strip().lower() for area in (search_in or [])]
    unknown = [area for area in areas if area not in _FULL_TEXT_AREAS]
    if unknown:
        raise ValidationError(
            f"search_in: unknown area {unknown[0]!r}; use any of "
            f"{', '.join(_FULL_TEXT_AREAS)}"
        )
    if not areas:
        raise ValidationError(
            f"search_in must name at least one of {', '.join(_FULL_TEXT_AREAS)}"
        )
    return list(dict.fromkeys(areas))


def _full_text_matcher(query_text: str, areas: List[str]):
    """Verify a full_text hit locally, one field at a time.

    The text must occur inside a single selected field: joining subject and
    body let a phrase that straddled the two match. Attachment names are only
    checked here because EWS cannot restrict on them.
    """
    needle = query_text.casefold()

    def matches(item: Any) -> bool:
        if "subject" in areas and needle in str(safe_get(item, "subject", "") or "").casefold():
            return True
        if "body" in areas and needle in item_text_body(item).casefold():
            return True
        if "attachments" in areas:
            return any(
                needle in str(safe_get(attachment, "name", "") or "").casefold()
                for attachment in (safe_get(item, "attachments", None) or ())
            )
        return False

    return matches


# The DB fields ``_full_text_matcher`` reads for each area. ``attachments``
# loads attachment metadata (names), never the attachment content.
_FULL_TEXT_MATCH_FIELDS = {
    "subject": ("subject",),
    "body": ("text_body", "body"),
    "attachments": ("attachments",),
}


_FLAG_STATUS_NAMES = {name.lower(): name for name in FLAG_STATUS_MAP}


def normalize_flag_status(value: Any) -> Optional[str]:
    """Return the canonical ``flag_status`` filter value, or None when absent."""
    if value in (None, ""):
        return None
    name = _FLAG_STATUS_NAMES.get(str(value).strip().lower())
    if name is None:
        raise ValidationError(
            f"flag_status={value!r}: use one of {', '.join(FLAG_STATUS_MAP)}"
        )
    return name


def _flag_status_q(status: str) -> Q:
    """EWS restriction on PidTagFlagStatus for one ``flag_status`` name.

    A message that was never flagged has no such property, and comparing a
    missing property is undefined in Exchange restrictions, so NotFlagged
    names the missing property explicitly besides excluding both set values.
    """
    if status == "NotFlagged":
        return Q(flag_status_value__exists=False) | (
            ~Q(flag_status_value=FLAG_STATUS_MAP["Flagged"])
            & ~Q(flag_status_value=FLAG_STATUS_MAP["Complete"])
        )
    return Q(flag_status_value=FLAG_STATUS_MAP[status])


def _item_flag_status(item: Any) -> str:
    """``flag_status`` of an item fetched with PidTagFlagStatus."""
    return _FLAG_STATUS_BY_VALUE.get(safe_get(item, "flag_status_value", None), "NotFlagged")


# ``search_scope=["all"]`` searches every mail folder of the mailbox. These
# folders and their subfolders stay out unless named next to "all"; the value
# is the Account attribute of the folder.
_ALL_SCOPE = "all"
_ALL_SCOPE_OPTIONAL = {
    "deleted": "trash",
    "trash": "trash",
    "junk": "junk",
    "drafts": "drafts",
    "outbox": "outbox",
}
# Every page fetches up to offset + max_results + 1 items from each folder,
# so the folder count bounds the cost of one whole-mailbox search.
SEARCH_ALL_MAX_FOLDERS = int(os.environ.get("EWS_SEARCH_ALL_MAX_FOLDERS", "50"))


def _is_mail_folder_class(folder_class: Optional[str]) -> bool:
    # Folders created through exchangelib can lack a class altogether.
    return not folder_class or folder_class.strip().lower() == "ipf.note"


def _all_mail_folders(account: Any, include: set) -> List[Tuple[str, Any]]:
    """Every mail folder under msg_folder_root as ``(path, folder)`` pairs.

    Deleted Items, Junk, Drafts and Outbox are skipped with their whole
    subtree unless ``include`` names their Account attribute. Non-mail
    folders (calendar, contacts, ...) are skipped but still walked, so mail
    folders below them are found.
    """
    excluded = set()
    for attr in sorted(set(_ALL_SCOPE_OPTIONAL.values()) - include):
        try:
            folder = getattr(account, attr)
        except ErrorFolderNotFound:
            continue  # the mailbox has no such folder, so nothing to leave out
        folder_id = safe_get(folder, "id", None)
        if folder_id:
            excluded.add(folder_id)

    found: List[Tuple[str, Any]] = []

    def visit(parent: Any, prefix: str) -> None:
        for child in parent.children:
            if safe_get(child, "id", None) in excluded:
                continue
            name = safe_get(child, "name", "") or ""
            path = f"{prefix}/{name}" if prefix else name
            if _is_mail_folder_class(safe_get(child, "folder_class", None)):
                found.append((path, child))
            visit(child, path)

    visit(account.msg_folder_root, "")
    return found


def _scope_names(search_scope: Any) -> List[str]:
    """``search_scope`` entries, stripped; a bare value is one entry.

    Case is kept because folder IDs are case-sensitive.
    """
    if search_scope in (None, ""):
        return []
    if not isinstance(search_scope, (list, tuple)):
        search_scope = [search_scope]
    return [str(name).strip() for name in search_scope if str(name).strip()]


# The scope names that map straight to a well-known folder (Account attribute).
_SCOPE_FOLDERS = {
    "inbox": "inbox",
    "sent": "sent",
    "drafts": "drafts",
    "deleted": "trash",
    "junk": "junk",
}
_SCOPE_ACCEPTED = (
    f"{', '.join(_SCOPE_FOLDERS)}, all, or a folder path, name or ID from list_folders"
)


async def _resolve_search_scope(
    account: Any, search_scope: Any,
) -> Tuple[List[Tuple[str, Any]], bool]:
    """Folders named by ``search_scope`` as ``(label, folder)`` pairs.

    The flag says whether the scope is the whole mailbox (``"all"``), whose
    labels are folder paths. Any other name resolves like ``folder`` does
    (path, custom folder name or folder ID); a name that matches no mail
    folder is refused instead of being dropped from the search.
    """
    names = _scope_names(search_scope)
    if _ALL_SCOPE not in [name.lower() for name in names]:
        resolved: List[Tuple[str, Any]] = []
        seen: set = set()
        for name in names:
            attr = _SCOPE_FOLDERS.get(name.lower())
            if attr:
                folder = getattr(account, attr)
            else:
                try:
                    folder = await resolve_folder_for_account(account, name)
                except ToolExecutionError as exc:
                    raise ValidationError(
                        f"search_scope: folder {name!r} not found. Accepted: {_SCOPE_ACCEPTED}.",
                        hint=str(exc),
                    ) from exc
                folder_class = safe_get(folder, "folder_class", None)
                if not _is_mail_folder_class(folder_class):
                    raise ValidationError(
                        f"search_scope: {name!r} is not a mail folder ({folder_class}). "
                        f"Accepted: {_SCOPE_ACCEPTED}."
                    )
            key = safe_get(folder, "id", None) or id(folder)
            if key in seen:
                continue  # the same folder named twice is searched once
            seen.add(key)
            resolved.append((safe_get(folder, "name", None) or name, folder))
        return resolved, False

    extras = [name for name in names if name.lower() != _ALL_SCOPE]
    unknown = [
        name for name in extras
        if name.lower() not in _ALL_SCOPE_OPTIONAL and name.lower() not in ("inbox", "sent")
    ]
    if unknown:
        raise ValidationError(
            f"search_scope: {unknown[0]!r} cannot be combined with 'all', which "
            f"already covers every mail folder; next to 'all' name only "
            f"{', '.join(sorted(_ALL_SCOPE_OPTIONAL))} to search those as well."
        )
    include = {
        _ALL_SCOPE_OPTIONAL[name.lower()] for name in extras
        if name.lower() in _ALL_SCOPE_OPTIONAL
    }
    folders = _all_mail_folders(account, include)
    if not folders:
        raise ToolExecutionError("search_scope=['all'] found no mail folders in this mailbox")
    if len(folders) > SEARCH_ALL_MAX_FOLDERS:
        raise ToolExecutionError(
            f"search_scope=['all'] covers {len(folders)} mail folders, more than "
            f"the limit of {SEARCH_ALL_MAX_FOLDERS} for one search. Search named "
            f"folders instead, or raise EWS_SEARCH_ALL_MAX_FOLDERS on the server."
        )
    return folders, True


def _whole_mailbox_report(
    folders: List[Tuple[str, Any]], per_folder_errors: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Which folders a ``search_scope=['all']`` search covered and which failed.

    A failed folder does not fail the search; its error is listed instead.
    """
    failed = {error["folder"] for error in per_folder_errors}
    return {
        "folders_searched": [label for label, _folder in folders if label not in failed],
        "folders_failed": list(per_folder_errors),
    }


def _folder_errors(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The per-folder errors a multi-folder search put in ``meta``."""
    return (response.get("meta") or {}).get("per_folder_errors", [])


@dataclass
class _SearchFilters:
    """EWS restrictions plus the local checks every hit must also pass."""

    restrictions: List[Any] = field(default_factory=list)
    matchers: List[Any] = field(default_factory=list)
    #: DB fields the matchers read, to add to a ``.only()`` projection
    match_fields: Tuple[str, ...] = ()

    def restriction(self) -> Optional[Any]:
        combined = None
        for restriction in self.restrictions:
            combined = restriction if combined is None else combined & restriction
        return combined

    def matcher(self) -> Optional[Any]:
        if not self.matchers:
            return None
        matchers = list(self.matchers)
        return lambda item: all(match(item) for match in matchers)


def _date_restriction_value(kwargs: Dict[str, Any], key: str) -> Optional[Any]:
    raw = kwargs.get(key)
    if not raw:
        return None
    parsed = parse_datetime_tz_aware(raw)
    if parsed is None:
        # Dropping an unreadable date would silently widen the search.
        raise ValidationError(f"{key}={raw!r} is not an ISO 8601 date or date-time")
    return parsed


def _recipient_matcher(needle: str):
    """Local check that a message's To names ``needle``.

    EWS cannot restrict on recipient lists (exchangelib refuses
    ``to_recipients`` in a restriction), so the filter runs on the fetched
    To list: a full address must equal one recipient, anything else is a
    case-insensitive part of an address or display name.
    """
    wanted = needle.strip().casefold()
    exact = is_full_address(wanted)

    def matches(item: Any) -> bool:
        for recipient in safe_get(item, "to_recipients", None) or ():
            address = str(safe_get(recipient, "email_address", "") or "").casefold()
            name = str(safe_get(recipient, "name", "") or "").casefold()
            if address == wanted if exact else (wanted in address or wanted in name):
                return True
        return False

    return matches


def _quick_filters(kwargs: Dict[str, Any], *, free_text: bool = True) -> _SearchFilters:
    """The quick-mode filters in ``kwargs``; full_text mode applies them too.

    ``sender``/``recipient`` alias ``from_address``/``to_address``. ``query``
    (subject OR body) is a filter here only when ``free_text`` is set; in
    full_text mode it is the search text itself. A partial sender and the
    recipient are checked locally because EWS has no substring restriction on
    mailboxes and none at all on recipient lists, and the subject because
    SmarterMail can ignore a subject restriction.
    """
    filters = _SearchFilters()
    add = filters.restrictions.append

    subject_contains = kwargs.get("subject_contains")
    if subject_contains:
        add(Q(subject__contains=subject_contains))
        filters.matchers.append(_subject_search_matcher(subject_contains))
        filters.match_fields += ("subject",)
    if kwargs.get("body_contains"):
        add(Q(body__contains=kwargs["body_contains"]))
    if free_text and kwargs.get("query"):
        add(Q(subject__contains=kwargs["query"]) | Q(body__contains=kwargs["query"]))
    from_address = kwargs.get("from_address") or kwargs.get("sender")
    if from_address and is_full_address(from_address):
        add(Q(sender=from_address))
    elif from_address:
        filters.matchers.append(lambda item, needle=from_address: sender_matches(item, needle))
        filters.match_fields += ("sender",)
    to_address = kwargs.get("to_address") or kwargs.get("recipient")
    if to_address:
        filters.matchers.append(_recipient_matcher(to_address))
        filters.match_fields += ("to_recipients",)
    if kwargs.get("has_attachments") is not None:
        add(Q(has_attachments=kwargs["has_attachments"]))
    if kwargs.get("is_read") is not None:
        add(Q(is_read=kwargs["is_read"]))
    if kwargs.get("is_flagged") is not None:
        # Issue #115: PR_FLAG_STATUS 2 is flagged. false excludes only that
        # value, so completed messages count as not flagged.
        add(Q(flag_status_value=2) if kwargs["is_flagged"] else ~Q(flag_status_value=2))
    flag_status = normalize_flag_status(kwargs.get("flag_status"))
    if flag_status:
        add(_flag_status_q(flag_status))
    if kwargs.get("importance"):
        add(Q(importance=kwargs["importance"]))
    if kwargs.get("categories"):
        # A message must carry every listed category.
        add(Q(categories__contains=kwargs["categories"]))
    start = _date_restriction_value(kwargs, "start_date")
    if start is not None:
        add(Q(datetime_received__gte=start))
    end = _date_restriction_value(kwargs, "end_date")
    if end is not None:
        add(Q(datetime_received__lte=end))
    return filters


def address_list(value: Any, field: str) -> List[str]:
    """Accept a list, or a comma/semicolon separated string, of addresses."""
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(";", ",").split(",")]
        return [p for p in parts if p]
    if isinstance(value, (list, tuple)):
        out: List[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif item is not None and not isinstance(item, str):
                raise ValidationError(f"{field} must contain e-mail address strings")
        return out
    raise ValidationError(f"{field} must be a string or an array of strings")


def _missing_last(value: Any) -> tuple:
    # An item without the value sorts as the smallest (last when newest
    # first) and None is never compared with a real value.
    return (0,) if value is None else (1, value)


def _mailbox_sort_value(mailbox: Any) -> Optional[str]:
    # A mailbox sorts by its display name, or its address when it has none.
    text = safe_get(mailbox, "name") or safe_get(mailbox, "email_address")
    return text.casefold() if text else None


_IMPORTANCE_RANK = {"Low": 0, "Normal": 1, "High": 2}

# Local stand-ins for the server sort on each orderable exchangelib field.
_MERGE_SORT_KEYS: Dict[str, Any] = {
    "datetime_received": lambda item: _missing_last(safe_get(item, "datetime_received")),
    "datetime_sent": lambda item: _missing_last(safe_get(item, "datetime_sent")),
    "subject": lambda item: (safe_get(item, "subject") or "").casefold(),
    "importance": lambda item: _IMPORTANCE_RANK.get(safe_get(item, "importance"), 1),
    "sender": lambda item: _missing_last(_mailbox_sort_value(safe_get(item, "sender"))),
}


def _folder_fetch_window(folder_count: int, offset: int, limit: int) -> Tuple[int, int]:
    """Return the ``(offset, max_results)`` to request from every folder.

    A single folder pages on the server. Across several, any one folder may
    supply anywhere from none to all of the first ``offset + limit`` merged
    items, so each must hand over that many. The extra item tells whether
    another page follows.
    """
    if folder_count <= 1:
        return offset, limit + 1
    return 0, offset + limit + 1


def _merge_folder_page(
    per_folder: List[Tuple[str, List[Any]]],
    *,
    sort_field: str,
    descending: bool,
    skip: int,
    limit: int,
) -> Tuple[List[Tuple[str, Any]], bool]:
    """Merge each folder's server-ordered window into one page.

    ``per_folder`` holds ``(folder_name, items)`` in server order, fetched
    with :func:`_folder_fetch_window`; ``skip`` drops the merged items that
    precede the page. ``heapq.merge`` only compares folder heads, so every
    folder keeps the server's order even where the local key collates
    differently, and the first ``n`` merged items depend only on each
    folder's first ``n``. Consecutive pages therefore cut one sequence and
    each item appears on exactly one of them. Ties go to the earlier folder.

    Returns the page as ``(folder_name, item)`` pairs and whether more
    items follow it.
    """
    item_key = _MERGE_SORT_KEYS.get(sort_field, lambda item: 0)
    merged = heapq.merge(
        *([(name, item) for item in items] for name, items in per_folder),
        key=lambda pair: item_key(pair[1]),
        reverse=descending,
    )
    window = list(itertools.islice(merged, skip + limit + 1))
    return window[skip:skip + limit], len(window) > skip + limit


def _build_list_item(
    email: Any,
    *,
    fields: List[str],
    folder_name: str,
) -> Dict[str, Any]:
    """Build the canonical list-endpoint item dict for one Message.

    Pulled out of the three search paths so they share one shape.
    """
    sender = safe_get(email, "sender", None)
    from_email = ""
    if sender and hasattr(sender, "email_address"):
        from_email = sender.email_address or ""
    text_body = item_text_body(email)
    received_iso = iso_or_none(safe_get(email, "datetime_received", None))
    item = {
        "message_id": ews_id_to_str(safe_get(email, "id", None)) or "",
        "subject": safe_get(email, "subject", "") or "",
        "from": from_email,
        "to": [
            r.email_address for r in (safe_get(email, "to_recipients", []) or [])
            if hasattr(r, "email_address")
        ],
        "received_time": received_iso,
        "is_read": safe_get(email, "is_read", False),
        "flag_status": _item_flag_status(email),
        "has_attachments": safe_get(email, "has_attachments", False),
        "importance": safe_get(email, "importance", "Normal"),
        "categories": safe_get(email, "categories", []) or [],
        "snippet": truncate_text(text_body, 200),
        "folder": folder_name,
    }
    if "body" in fields:
        item["body"] = text_body
    strip_body_by_default(item, keep_body="body" in fields)
    return project_fields(item, fields)


# Search results show the follow-up flag state by default; a whole-mailbox
# search also names the folder each hit came from.
_SEARCH_DEFAULT_FIELDS: Tuple[str, ...] = (*LIST_DEFAULT_FIELDS, "flag_status")


def _search_fields(kwargs: Dict[str, Any], *, all_scope: bool = False) -> List[str]:
    """The caller's ``fields`` projection, else the search default."""
    if kwargs.get("fields"):
        return kwargs["fields"]
    return [*_SEARCH_DEFAULT_FIELDS, *(("folder",) if all_scope else ())]


SEARCH_MODES = ("quick", "advanced", "full_text")

# Parameters that mean something in some search modes only.
_MODE_PARAMS: Dict[str, Tuple[str, ...]] = {
    "query": ("quick", "full_text"),
    "search_query": ("full_text",),
    "keywords": ("advanced",),
    "search_in": ("full_text",),
    "exact_phrase": ("full_text",),
    "sort_by": ("quick", "advanced"),
    "sort_order": ("quick", "advanced"),
}
_MODE_PARAM_NOTES = {
    "query": "advanced mode searches subject and body with keywords",
    "search_query": "it is full_text mode's old name for query",
    "keywords": "quick and full_text mode take the text as query",
    "sort_by": "full_text results are always newest first",
    "sort_order": "full_text results are always newest first",
}
# A value equal to the schema default changes nothing, and some clients fill
# defaults in, so it is not a request for a mode-specific behaviour.
_SEARCH_PARAM_DEFAULTS: Dict[str, Any] = {
    "search_in": ["subject", "body"],
    "exact_phrase": False,
    "sort_by": "datetime_received",
    "sort_order": "descending",
}
_SORT_FIELDS = ("datetime_received", "datetime_sent", "from", "subject", "importance")


def search_param_is_default(name: str, value: Any) -> bool:
    """True when ``value`` is absent or equals the search schema default."""
    if value in (None, "", []):
        return True
    default = _SEARCH_PARAM_DEFAULTS.get(name)
    if isinstance(default, list) and isinstance(value, (list, tuple)):
        return sorted(str(v).strip().lower() for v in value) == sorted(default)
    return default is not None and value == default


def search_mode(kwargs: Dict[str, Any]) -> str:
    """The requested search mode; an unknown one is refused, not run as quick."""
    mode = str(kwargs.get("mode") or "quick").strip().lower()
    if mode not in SEARCH_MODES:
        raise ValidationError(
            f"mode={kwargs.get('mode')!r}: use one of {', '.join(SEARCH_MODES)}"
        )
    return mode


def reject_other_mode_params(kwargs: Dict[str, Any], mode: str) -> None:
    """Refuse a parameter the requested mode would ignore, naming its mode."""
    for name, modes in _MODE_PARAMS.items():
        if mode in modes or search_param_is_default(name, kwargs.get(name)):
            continue
        note = _MODE_PARAM_NOTES.get(name)
        raise ValidationError(
            f"{name} is not used in mode {mode!r}; use mode "
            f"{' or '.join(repr(m) for m in modes)}" + (f" ({note})." if note else ".")
        )


def _sort_spec(kwargs: Dict[str, Any]) -> Tuple[str, bool]:
    """``(exchangelib field, descending)`` for sort_by / sort_order."""
    sort_by = kwargs.get("sort_by") or "datetime_received"
    sort_order = kwargs.get("sort_order") or "descending"
    if sort_by not in _SORT_FIELDS:
        raise ValidationError(f"sort_by={sort_by!r}: use one of {', '.join(_SORT_FIELDS)}")
    if sort_order not in ("ascending", "descending"):
        raise ValidationError(f"sort_order={sort_order!r}: use ascending or descending")
    # ``from`` is a public name, not an exchangelib field. Results and the
    # from_address filter read it from ``sender``, so sort on that too.
    return _DB_FIELDS_BY_PUBLIC.get(sort_by, (sort_by,))[0], sort_order == "descending"


def extract_body_html(message) -> str:
    """
    Properly extract HTML body content from an Exchange message.

    In exchangelib, message.body is an HTMLBody object, not a string.
    The actual HTML content is inside message.body.body.

    Args:
        message: Exchange Message object

    Returns:
        HTML body content as string, or empty string if no body
    """
    body = safe_get(message, "body", None)
    if not body:
        return ""

    # HTMLBody object has inner 'body' property with actual content
    if hasattr(body, 'body') and body.body:
        html = body.body
    elif isinstance(body, str):
        html = body
    else:
        html = str(body) if body else ""

    # Strip CDATA wrappers that may appear in Exchange HTML bodies
    # These cause visible "]]>" text at the bottom of forwarded/replied emails
    if html and '<![CDATA[' in html:
        html = html.replace('<![CDATA[', '').replace(']]>', '')

    # Strip document-level HTML tags to prevent nested <html><body> issues
    # when embedding in quoted content. Nested document tags break HTML structure.
    return strip_html_document_tags(html)


def strip_html_document_tags(html: str) -> str:
    """
    Strip document-level HTML tags from content while preserving styles.

    When forwarding/replying, the original email body may contain full HTML
    document structure (<html>, <head>, <body>). We need to:
    - Remove structural tags (<html>, <body>) to prevent nesting issues
    - Preserve <style> blocks and MSO conditional comments for proper rendering
    - Keep @font-face declarations for Arabic fonts (Sakkal Majalla) and RTL layout

    Args:
        html: HTML content that may contain document-level tags

    Returns:
        Inner content with document structure stripped but styles preserved
    """
    if not html:
        return html

    # Extract <style> blocks to preserve them
    style_blocks = re.findall(r'<style[^>]*>.*?</style>', html, flags=re.IGNORECASE | re.DOTALL)

    # Extract MSO conditional comments (<!--[if gte mso 9]>...<![endif]-->)
    mso_comments = re.findall(r'<!--\[if[^\]]*\]>.*?<!\[endif\]-->', html, flags=re.IGNORECASE | re.DOTALL)

    # Remove DOCTYPE declaration
    html = re.sub(r'<!DOCTYPE[^>]*>', '', html, flags=re.IGNORECASE)

    # Remove <html> open/close tags (with any attributes)
    html = re.sub(r'</?html[^>]*>', '', html, flags=re.IGNORECASE)

    # Remove <head> tags but extract content we want to preserve
    html = re.sub(r'<head[^>]*>.*?</head>', '', html, flags=re.IGNORECASE | re.DOTALL)

    # Remove <body> open/close tags but keep the content inside
    html = re.sub(r'</?body[^>]*>', '', html, flags=re.IGNORECASE)

    # Prepend preserved styles and MSO comments
    preserved_content = '\n'.join(style_blocks + mso_comments)
    if preserved_content:
        html = preserved_content + '\n' + html

    return html.strip()


_FORWARD_PREFIX_RE = re.compile(
    r"^(?:fw|fwd|forward)\s*:\s*", re.IGNORECASE
)
_REPLY_PREFIX_RE = re.compile(r"^(?:re|rply|reply)\s*:\s*", re.IGNORECASE)


def has_forward_prefix(subject: str) -> bool:
    """Check if subject already has a forward prefix (FW:, Fwd:, FWD:, Forward:)."""
    if not subject:
        return False
    return bool(_FORWARD_PREFIX_RE.match(subject))


def has_reply_prefix(subject: str) -> bool:
    """Check if subject already has a reply prefix (RE:, Re:, Reply:)."""
    if not subject:
        return False
    return bool(_REPLY_PREFIX_RE.match(subject))


def add_forward_prefix(subject: str) -> str:
    """Return ``subject`` prefixed with the canonical ``FW:``.

    Normalises any existing variant — ``Fwd:``, ``FWD:``, ``Forward:``,
    mixed case, stray whitespace — to the single ``FW: <body>`` form
    and never stacks ("FW: FW: hello" cannot happen). The C10 fix
    stripped only ``FW:``; Bug 5 in the follow-up widens the strip to
    cover Fwd:/FWD:/Forward: too.
    """
    if not subject:
        return "FW:"
    # Strip leading whitespace before prefix detection so "  Fwd: x"
    # still normalises correctly.
    stripped_input = subject.lstrip()
    stripped = _FORWARD_PREFIX_RE.sub("", stripped_input, count=1).strip()
    if not stripped:
        return "FW:"
    return f"FW: {stripped}"


def add_reply_prefix(subject: str) -> str:
    """Return ``subject`` prefixed with the canonical ``RE:``.

    Same normalisation approach as :func:`add_forward_prefix`: any
    variant (Re:, Reply:) is stripped and replaced with ``RE:``.
    """
    if not subject:
        return "RE:"
    stripped_input = subject.lstrip()
    stripped = _REPLY_PREFIX_RE.sub("", stripped_input, count=1).strip()
    if not stripped:
        return "RE:"
    return f"RE: {stripped}"


def reply_recipients(
    sender: str,
    original_to: List[str],
    original_cc: List[str],
    own_address: Optional[str],
    *,
    reply_all: bool,
    extra_cc: Optional[List[str]] = None,
    extra_bcc: Optional[List[str]] = None,
) -> Tuple[List[str], List[str], List[str]]:
    """Return the ``(to, cc, bcc)`` addresses of a reply.

    Reply-all keeps the original roles, as Outlook does: the sender and the
    original To go in To, the original Cc stays in Cc, and our own address is
    dropped from both. A plain reply goes to the sender only. ``extra_cc`` and
    ``extra_bcc`` are the caller's additions and are kept even when they name
    us, because the caller asked for them explicitly.

    Addresses compare case-insensitively and each one lands only in the first
    field that claims it (To, then Cc, then Bcc), so nobody is listed twice.
    """
    own = (own_address or "").strip().lower()
    seen: set = set()

    def take(addresses, target: List[str], *, keep_own: bool = False) -> None:
        for address in addresses or ():
            address = (address or "").strip()
            key = address.lower()
            if not address or key in seen or (key == own and not keep_own):
                continue
            seen.add(key)
            target.append(address)

    to: List[str] = []
    cc: List[str] = []
    bcc: List[str] = []
    if reply_all:
        take([sender, *original_to], to)
        take(original_cc, cc)
        if not to and not cc:
            # A note to self leaves nobody else to answer; reply to its
            # sender as a plain reply would rather than to no one.
            take([sender], to, keep_own=True)
    else:
        take([sender], to, keep_own=True)
    take(extra_cc, cc, keep_own=True)
    take(extra_bcc, bcc, keep_own=True)
    return to, cc, bcc


def clean_original_body_for_signature(original_body_html: str) -> str:
    """
    Remove or rename WordSection1 from original content to prevent
    Exclaimer/server-side signature systems from placing signature after it.

    Exclaimer looks for the LAST </div> that closes a WordSection1 and
    inserts signature after it. If the original email has WordSection1,
    the signature ends up at the very end instead of after user's message.

    Args:
        original_body_html: The HTML body of the original email

    Returns:
        Cleaned HTML with WordSection1 renamed to OriginalSection
    """
    if not original_body_html:
        return original_body_html

    # Rename WordSection1 to OriginalSection to avoid confusing Exclaimer
    cleaned = original_body_html.replace('class="WordSection1"', 'class="OriginalSection"')
    cleaned = cleaned.replace("class='WordSection1'", "class='OriginalSection'")
    cleaned = cleaned.replace('class=WordSection1', 'class=OriginalSection')

    # Also handle variations with spaces
    cleaned = cleaned.replace('class = "WordSection1"', 'class="OriginalSection"')
    cleaned = cleaned.replace("class = 'WordSection1'", "class='OriginalSection'")

    return cleaned


def format_forward_header(message) -> dict:
    """
    Format the forwarded message header like Outlook.

    - From: Name <email> format
    - To/Cc: Name <email> format
    - Sent: Full date format with day name

    Args:
        message: Exchange Message object

    Returns:
        Dictionary with formatted header fields
    """
    # From: Name <email> format
    # Try multiple sources for sender info (some may be None for distribution lists, etc.)
    sender_name = ""
    sender_email = ""

    # Try sender first (most common)
    sender = safe_get(message, "sender", None)
    if sender:
        sender_name = (sender.name or "") if hasattr(sender, "name") else ""
        sender_email = (sender.email_address or "") if hasattr(sender, "email_address") else ""

    # Fallback 1: Try author if sender.email_address is empty
    if not sender_email:
        author = safe_get(message, "author", None)
        if author:
            if not sender_name:
                sender_name = (author.name or "") if hasattr(author, "name") else ""
            sender_email = (author.email_address or "") if hasattr(author, "email_address") else ""

    # Fallback 2: Try from_ if still empty
    if not sender_email:
        from_field = safe_get(message, "from_", None)
        if from_field:
            if not sender_name:
                sender_name = (from_field.name or "") if hasattr(from_field, "name") else ""
            sender_email = (from_field.email_address or "") if hasattr(from_field, "email_address") else ""

    # Fallback 3: Extract from internet_message_headers as last resort
    if not sender_email:
        headers = safe_get(message, "headers", None) or safe_get(message, "internet_message_headers", None)
        if headers:
            for h in headers:
                header_name = getattr(h, 'name', '') or ''
                if header_name.lower() == 'from':
                    header_value = getattr(h, 'value', '') or ''
                    # Parse "Name <email>" format
                    match = re.search(r'<([^>]+)>', header_value)
                    if match:
                        sender_email = match.group(1)
                        # Also extract name if we don't have it
                        if not sender_name:
                            name_part = header_value[:header_value.find('<')].strip()
                            if name_part:
                                sender_name = name_part.strip('"\'')
                    elif '@' in header_value:
                        sender_email = header_value.strip()
                    break

    # Return plain text with literal angle brackets. The reply / forward callers
    # run this output through escape_html() exactly once before HTML interpolation.
    # Pre-escaping here would double-escape on every cycle
    # (`&lt;` → `&amp;lt;` → `&amp;amp;lt;`...), which is the "multiple `&` between
    # names in the thread" bug users see after a few reply rounds.
    if sender_name and sender_email:
        from_str = f"{sender_name} <{sender_email}>"
    elif sender_email:
        from_str = sender_email
    elif sender_name:
        from_str = sender_name
    else:
        from_str = ""

    # To/Cc: same convention — raw text, single escape happens at the call site.
    def format_recipients(recipients):
        if not recipients:
            return ""
        parts = []
        for r in recipients:
            if not r:
                continue
            # Use 'or ""' to convert None to empty string
            name = (r.name or "") if hasattr(r, "name") else ""
            email = (r.email_address or "") if hasattr(r, "email_address") else ""
            if name and email:
                parts.append(f"{name} <{email}>")
            elif email:
                parts.append(email)
            elif name:
                parts.append(name)
        return '; '.join(parts)

    to_recipients = safe_get(message, "to_recipients", []) or []
    cc_recipients = safe_get(message, "cc_recipients", []) or []

    to_str = format_recipients(to_recipients)
    cc_str = format_recipients(cc_recipients)

    # Date: Full format with day name
    sent_date = safe_get(message, "datetime_sent", None)
    date_str = ""
    if sent_date:
        date_str = sent_date.strftime('%A, %B %d, %Y %I:%M:%S %p')

    return {
        'from': from_str,
        'sent': date_str,
        'to': to_str,
        'cc': cc_str,
        'subject': safe_get(message, "subject", "") or ""
    }


def copy_attachments_to_message(original_message, new_message) -> tuple:
    """
    Copy all attachments from original message to new message,
    preserving inline image properties (content_id, is_inline).

    This is critical for email signatures with embedded images to display correctly.

    Args:
        original_message: Source message with attachments
        new_message: Target message to attach files to

    Returns:
        Tuple of (inline_count, regular_count)
    """
    # Let unreadable or unsupported attachments fail the operation instead of
    # sending a message that silently omits part of the original.

    attachments = safe_get(original_message, "attachments", []) or []
    if not attachments:
        return 0, 0

    inline_count = 0
    regular_count = 0

    for att in attachments:
        metadata = {
            "name": att.name,
            "content_type": att.content_type,
            "content_id": att.content_id,
            "content_location": att.content_location,
            "is_inline": att.is_inline,
        }
        if isinstance(att, FileAttachment):
            new_att = FileAttachment(content=att.content, **metadata)
        elif isinstance(att, ItemAttachment):
            source_item = att.item
            if not isinstance(source_item, Message):
                raise ToolExecutionError(
                    f"Cannot preserve embedded {type(source_item).__name__} attachment: {att.name}"
                )
            item, fidelity = build_embedded_message(new_message.account, source_item)
            if fidelity == "fields":
                copy_attachments_to_message(source_item, item)
            new_att = ItemAttachment(item=item, **metadata)
        else:
            raise ToolExecutionError(f"Unsupported original attachment: {att.name}")
        new_message.attach(new_att)
        if att.is_inline:
            inline_count += 1
        else:
            regular_count += 1

    return inline_count, regular_count


def is_exchange_folder_id(identifier: str) -> bool:
    """
    Check if the identifier looks like an Exchange folder/item ID.

    Exchange IDs are base64-encoded strings. The most common modern
    prefixes are:

    * ``AAMk`` — newer EwsLegacyId / EwsId format (most common)
    * ``AQMk`` — also seen on real mailboxes (Issue #112)
    * ``AAE``  — older legacy hex-prefix variant

    Base64 can contain '/' characters, so we need to detect IDs before
    attempting to parse as folder paths.
    """
    if not identifier or not isinstance(identifier, str):
        return False
    # Newer EwsId formats (Issue #112: AQMk seen on production mailboxes
    # alongside AAMk; both must be recognised).
    if len(identifier) > 50 and identifier[:4] in ("AAMk", "AQMk"):
        return True
    # Legacy hex-prefix variant.
    if identifier.startswith("AAE") and len(identifier) > 50:
        return True
    return False


async def resolve_folder_for_account(account, folder_identifier: str):
    """
    Resolve folder from name, path, or ID for a specific account.

    Supports:
    - Standard names: inbox, sent, drafts, deleted, junk
    - Folder paths: Inbox/CC, Inbox/Projects/2024
    - Folder IDs: AAMkADc3MWUy... (base64 encoded, may contain '/' characters)
    - Custom folder names: CC, Archive, Projects

    Args:
        account: Exchange Account object (primary or impersonated)
        folder_identifier: Folder name, path, or ID
    """
    folder_identifier = folder_identifier.strip()

    folder_map = get_standard_folder_map(account)
    folder_map["trash"] = account.trash

    def traverse_folder_path(start_folder, path_parts):
        """Walk a folder path from an explicit starting folder."""
        current_folder = start_folder
        for subfolder_name in path_parts:
            found = None
            try:
                for child in list(getattr(current_folder, "children", []) or []):
                    if safe_get(child, "name", "").lower() == subfolder_name.lower():
                        found = child
                        break
            except Exception as e:
                current_name = safe_get(current_folder, "name", "root")
                raise ToolExecutionError(
                    f"Error accessing subfolders of '{current_name}': {e}"
                ) from e

            if not found:
                current_name = safe_get(current_folder, "name", "root")
                raise ToolExecutionError(
                    f"Subfolder '{subfolder_name}' not found under '{current_name}'"
                )
            current_folder = found
        return current_folder

    # Try 1: Standard folder name (case-insensitive)
    folder_lower = folder_identifier.lower()
    if folder_lower in folder_map:
        return folder_map[folder_lower]

    # Try 2: Folder ID (starts with AAMk or similar Exchange ID pattern)
    # IMPORTANT: Check this BEFORE path parsing, as base64 IDs can contain '/'
    if is_exchange_folder_id(folder_identifier):
        found_folder = find_folder_by_id(account.root, folder_identifier)
        if found_folder:
            return found_folder
        # If not found as folder ID, don't fall through to path parsing
        raise ToolExecutionError(
            f"Folder ID '{folder_identifier[:20]}...' not found. "
            f"The ID appears to be an Exchange folder ID but could not be located in your mailbox."
        )

    # Try 3: Folder path (e.g., "Inbox/CC", "/Archive/2024", "Archive/2024")
    if '/' in folder_identifier:
        parts = [part.strip() for part in folder_identifier.split('/') if part.strip()]
        if not parts:
            raise ToolExecutionError("Folder path is empty")

        if folder_identifier.startswith('/'):
            return traverse_folder_path(account.root, parts)

        parent_name = parts[0].lower()
        if parent_name in folder_map:
            return traverse_folder_path(folder_map[parent_name], parts[1:])

        path_errors = []
        for start_folder in (account.root, account.inbox):
            try:
                return traverse_folder_path(start_folder, parts)
            except ToolExecutionError as e:
                path_errors.append(str(e))

        raise ToolExecutionError(
            f"Folder path '{folder_identifier}' not found from mailbox root or inbox. "
            f"Resolution attempts: {' | '.join(path_errors)}"
        )

    # Try 4: Search for custom folder by name (recursively under inbox)
    def search_folder_tree(parent, target_name, max_depth=3, current_depth=0):
        """Recursively search for folder by name."""
        if current_depth >= max_depth:
            return None

        try:
            for child in parent.children:
                child_name = safe_get(child, 'name', '')
                if child_name.lower() == target_name.lower():
                    return child
                # Recurse into subfolders
                found = search_folder_tree(child, target_name, max_depth, current_depth + 1)
                if found:
                    return found
        except Exception:
            pass

        return None

    # Search under root first so top-level custom folders win over inbox children.
    custom_folder = search_folder_tree(account.root, folder_identifier)
    if custom_folder:
        return custom_folder

    # Search under inbox as fallback for mailbox layouts that hide folder tree roots.
    custom_folder = search_folder_tree(account.inbox, folder_identifier)
    if custom_folder:
        return custom_folder

    # If all methods fail, provide helpful error
    raise ToolExecutionError(
        f"Folder '{folder_identifier}' not found. "
        f"Available standard folders: {', '.join(folder_map.keys())}. "
        f"For custom folders, use full path (e.g., 'Inbox/CC') or get folder ID from list_folders."
    )


async def resolve_folder(ews_client, folder_identifier: str):
    """
    Backward-compatible wrapper for resolve_folder_for_account.

    Deprecated: Use resolve_folder_for_account with explicit account parameter.
    """
    return await resolve_folder_for_account(ews_client.account, folder_identifier)


class SendEmailTool(BaseTool):
    """Tool for sending emails."""

    _COMPOSE_FIELDS = (
        "to", "subject", "body", "cc", "bcc", "importance",
        "attachments", "inline_attachments", "body_format", "files",
    )

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "send_email",
            "description": (
                "Send a new email, or send an existing draft as-is with draft_id. "
                "Draft sends preserve stored recipients, body and attachments; "
                "do not pass compose fields with draft_id."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "draft_id": {
                        "type": "string",
                        "minLength": 1,
                        "pattern": "\\S",
                        "description": (
                            "Existing draft message ID to send unchanged. Omit "
                            "to/subject/body and all other compose fields. "
                            "Use target_mailbox for the mailbox holding the draft."
                        ),
                    },
                    "to": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Recipient email addresses"
                    },
                    "subject": {
                        "type": "string",
                        "description": "Email subject"
                    },
                    "body": {
                        "type": "string",
                        "description": "Email body. Format determined by body_format (default: html)."
                    },
                    "cc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "CC recipients (optional)"
                    },
                    "bcc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "BCC recipients (optional)"
                    },
                    "importance": {
                        "type": "string",
                        "enum": ["Low", "Normal", "High"],
                        "description": "Exchange-only email importance level (optional)"
                    },
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Exchange-only server-local file paths; use files on any supported provider."
                    },
                    "files": FILES_SCHEMA,
                    **INLINE_ATTACHMENTS_SCHEMA,
                    **BODY_FORMAT_SCHEMA,
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to send on behalf of (requires impersonation/delegate access)"
                    },
                    "dry_run": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Preview without sending or modifying a message. With draft_id, reads "
                            "the existing draft and returns stored recipients, subject and a body preview. "
                            "Compose-mode files are validated but not fetched unless verify_files=true."
                        )
                    },
                    "verify_files": VERIFY_FILES_SCHEMA,
                },
                "required": [],
                "oneOf": [
                    {
                        "required": ["draft_id"],
                        "not": {"anyOf": [
                            {"required": [field]} for field in self._COMPOSE_FIELDS
                        ]},
                    },
                    {
                        "required": ["to", "subject", "body"],
                        "not": {"required": ["draft_id"]},
                    },
                ],
            }
        }

    def _draft_id(self, kwargs: Dict[str, Any]) -> Optional[str]:
        if "draft_id" not in kwargs:
            return None
        draft_id = kwargs["draft_id"]
        if not isinstance(draft_id, str) or not draft_id.strip():
            raise ValidationError("draft_id must be a non-empty message ID")
        overrides = [field for field in self._COMPOSE_FIELDS if field in kwargs]
        if overrides:
            raise ValidationError(
                "draft_id sends the stored draft as-is; omit " + ", ".join(overrides)
            )
        return draft_id

    def _send_draft(
        self, draft_id: str, target_mailbox: Optional[str], dry_run: bool
    ) -> Dict[str, Any]:
        try:
            account = self.get_account(target_mailbox)
            draft = find_message_for_account(account, draft_id)
            if not isinstance(draft, Message) or draft.is_draft is not True:
                raise ValidationError("draft_id must identify an unsent draft message")
            mailbox = self.get_mailbox_info(target_mailbox)
            recipients = [r.email_address for r in (draft.to_recipients or [])]
            if dry_run:
                body = str(draft.body or "")
                return format_success_response(
                    "Dry run — no email sent",
                    dry_run=True, sent=False, draft_id=draft_id,
                    would_send_to=recipients,
                    would_cc=[r.email_address for r in (draft.cc_recipients or [])],
                    would_bcc=[r.email_address for r in (draft.bcc_recipients or [])],
                    subject=draft.subject,
                    body_preview=body[:280] + ("..." if len(body) > 280 else ""),
                    attachments_count=len(draft.attachments or []),
                    mailbox=mailbox,
                )
            # A saved Message uses SendItem, preserving server-side content.
            # send_and_save() would update writable fields instead.
            draft.send()
            return format_success_response(
                "Draft accepted by mail server; final delivery not yet confirmed",
                draft_id=draft_id, sent=True,
                accepted_by_mail_server=True, delivery_confirmed=False,
                recipients=recipients, mailbox=mailbox,
            )
        except (ValidationError, ToolExecutionError):
            raise
        except Exception as exc:
            raise ToolExecutionError(f"Failed to send draft: {exc}") from exc

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Send email via EWS."""
        draft_id = self._draft_id(kwargs)
        if draft_id is not None:
            return self._send_draft(
                draft_id, kwargs.get("target_mailbox"), bool(kwargs.get("dry_run", False))
            )
        # Get target mailbox for impersonation
        target_mailbox = kwargs.pop("target_mailbox", None)
        dry_run = bool(kwargs.pop("dry_run", False))
        body_format = kwargs.pop("body_format", "html")

        # v4.0: convert LLM-supplied body to HTML if it came in as markdown/text.
        # Backward-compatible: body_format="html" (default) is a no-op.
        if "body" in kwargs and body_format != "html":
            from ..body_format import compose_body
            converted_html, _actual_fmt = compose_body(kwargs["body"], body_format)
            kwargs["body"] = converted_html

        # Validate input
        request = self.validate_input(SendEmailRequest, **kwargs)

        try:
            files = validate_outgoing_files(kwargs.get("files"))
            fetch_files = not dry_run or bool(kwargs.get("verify_files"))
            outgoing_files = await load_outgoing_files(
                files, storage_client=lambda: smartermail_client(self.ews_client, target_mailbox)
            ) if fetch_files else []
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)
            tracking_id = new_outbound_tracking_id()

            # Clean and prepare email body
            email_body = request.body.strip()

            # Strip CDATA wrapper if present (CDATA is XML syntax, not needed for Exchange)
            if email_body.startswith('<![CDATA[') and email_body.endswith(']]>'):
                email_body = email_body[9:-3].strip()  # Remove <![CDATA[ and ]]>
                self.logger.info("Stripped CDATA wrapper from email body")

            # Validate body is not empty after processing
            if not email_body:
                raise ToolExecutionError("Email body is empty after processing")

            # Detect if body is HTML or plain text
            is_html = bool(re.search(r'<[^>]+>', email_body))  # Check for HTML tags

            # Log body details for debugging
            body_type = "HTML" if is_html else "Plain Text"
            self.logger.info(f"Email body: {body_type}, {len(email_body)} characters, "
                           f"{len(email_body.encode('utf-8'))} bytes (UTF-8)")

            # Create message with appropriate body type
            # CRITICAL: Use HTMLBody for HTML, Body for plain text
            # Using wrong type causes Exchange to strip content!
            if is_html:
                message = Message(
                    account=account,
                    author=Mailbox(email_address=account.primary_smtp_address),
                    outbound_tracking_id=tracking_id,
                    subject=request.subject,
                    body=HTMLBody(email_body),
                    to_recipients=[Mailbox(email_address=email) for email in request.to]
                )
                self.logger.info("Using HTMLBody for HTML content")
            else:
                message = Message(
                    account=account,
                    author=Mailbox(email_address=account.primary_smtp_address),
                    outbound_tracking_id=tracking_id,
                    subject=request.subject,
                    body=Body(email_body),
                    to_recipients=[Mailbox(email_address=email) for email in request.to]
                )
                self.logger.info("Using Body (plain text) for non-HTML content")

            # Add CC recipients
            if request.cc:
                message.cc_recipients = [Mailbox(email_address=email) for email in request.cc]

            # Add BCC recipients
            if request.bcc:
                message.bcc_recipients = [Mailbox(email_address=email) for email in request.bcc]

            # Set importance
            message.importance = request.importance.value

            # CRITICAL: Verify body was set correctly BEFORE attaching/sending
            if not message.body or len(str(message.body).strip()) == 0:
                raise ToolExecutionError(
                    f"Message body is empty after creation! Original body length: {len(email_body)}, "
                    f"Message body: {message.body}"
                )
            self.logger.info(f"Verified message body set correctly: {len(str(message.body))} characters")

            # Add attachments if provided
            attachment_count = 0
            if request.attachments:
                for file_path in request.attachments:
                    try:
                        # Use os.path.basename to handle both Windows and Unix paths
                        file_name = os.path.basename(file_path)
                        with open(file_path, 'rb') as f:
                            content = f.read()
                            attachment = FileAttachment(
                                name=file_name,
                                content=content
                            )
                            message.attach(attachment)
                            attachment_count += 1
                            self.logger.info(f"Attached file: {file_name} ({len(content)} bytes)")
                    except FileNotFoundError:
                        raise ToolExecutionError(f"Attachment file not found on the Email MCP server: {file_path}. {REMOTE_AGENT_POINTER}", hint=REMOTE_AGENT_HINT)
                    except PermissionError:
                        raise ToolExecutionError(f"Permission denied reading attachment: {file_path}")
                    except Exception as e:
                        raise ToolExecutionError(f"Failed to attach file {file_path}: {e}")

                self.logger.info(f"Total attachments added: {attachment_count}")

            # Add inline (base64) attachments if provided
            inline_count = attach_inline_files(message, kwargs.get("inline_attachments", []))
            if inline_count > 0:
                attachment_count += inline_count
                self.logger.info(f"Added {inline_count} inline (base64) attachment(s)")
            attachment_count += attach_outgoing_files(message, outgoing_files)

            # Dry-run short-circuit: return a preview of what WOULD be sent
            # without actually calling message.send(). Nothing is persisted
            # to Exchange, no Drafts entry is created.
            if dry_run:
                self.logger.info(
                    f"DRY RUN: would send to {', '.join(request.to)} "
                    f"with {attachment_count} attachment(s)"
                )
                body_preview = email_body[:280] + ("..." if len(email_body) > 280 else "")
                return format_success_response(
                    "Dry run — no email sent",
                    dry_run=True,
                    sent=False,
                    would_send_to=request.to,
                    would_cc=request.cc or [],
                    would_bcc=request.bcc or [],
                    subject=request.subject,
                    body_preview=body_preview,
                    body_type=body_type,
                    attachments_count=attachment_count,
                    would_attach=attachment_count + (0 if fetch_files else len(files)),
                    **dry_run_file_report(files, outgoing_files if fetch_files else None),
                    importance=request.importance.value,
                    mailbox=mailbox,
                )

            # Send the message (attachments are included automatically)
            message.send()
            # tracking_id is None when outbound tracking is turned off.
            accepted = f"message {tracking_id}" if tracking_id else "message"
            self.logger.info(
                f"Mail server accepted {accepted} to "
                f"{', '.join(request.to)} with {attachment_count} attachment(s)"
            )

            # FINAL VERIFICATION: Check message body after send
            if hasattr(message, 'body') and message.body and len(str(message.body).strip()) > 0:
                body_length = len(str(message.body))
                self.logger.info(f"✅ SUCCESS: Email sent with body content ({body_length} characters)")
            else:
                # This should not happen, but if it does, it's critical to know
                raise ToolExecutionError(
                    "CRITICAL: Message body is empty after send! "
                    "Email may have been sent without content. "
                    f"Original body length: {len(email_body)}, "
                    f"Body type: {body_type}"
                )

            return format_success_response(
                "Email accepted by mail server; final delivery not yet confirmed",
                message_id=ews_id_to_str(message.id) if hasattr(message, 'id') else None,
                tracking_id=tracking_id,
                accepted_by_mail_server=True,
                delivery_confirmed=False,
                sent_time=datetime.now().isoformat(),
                recipients=request.to,
                mailbox=mailbox
            )

        except (ValidationError, ToolExecutionError):
            raise
        except Exception as e:
            self.logger.error(f"Failed to send email: {e}")
            raise ToolExecutionError(f"Failed to send email: {e}")


class ReadEmailsTool(BaseTool):
    """Tool for reading emails from inbox."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "read_emails",
            "description": "Read emails from a folder (default: inbox).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "folder": {
                        "type": "string",
                        "description": "Folder name (standard names: inbox, sent, drafts; paths: Inbox/CC; or folder ID)",
                        "default": "inbox"
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of emails to retrieve",
                        "default": 50,
                        "maximum": 1000
                    },
                    "unread_only": {
                        "type": "boolean",
                        "description": "Only return unread emails",
                        "default": False
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to read from (requires impersonation/delegate access)"
                    },
                    **READ_FORMAT_SCHEMA,
                    # A listing carries previews only unless the caller asks
                    # for bodies: 50 full bodies per call would swamp the
                    # agent's context.
                    "include_body": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "When true, add each message's full body next to "
                            "its 200-character preview, rendered per 'format' "
                            "and 'trim_quoted'. Exchange mailboxes only; Graph "
                            "and IMAP listings return previews, so use "
                            "get_email_details for a body there."
                        ),
                    },
                }
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Read emails from folder."""
        folder_name = kwargs.get("folder", "inbox")
        max_results = kwargs.get("max_results", 50)
        unread_only = kwargs.get("unread_only", False)
        target_mailbox = kwargs.get("target_mailbox")
        # Preview-only by default; format/trim_quoted shape the opt-in body.
        fmt = kwargs.get("format") or DEFAULT_READ_FORMAT
        do_trim_quoted = bool(kwargs.get("trim_quoted", False))
        include_body = kwargs.get("include_body") is True

        try:
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Get folder - supports standard names, paths, and folder IDs
            folder = await resolve_folder_for_account(account, folder_name)
            self.logger.info(f"Resolved folder '{folder_name}' to: {safe_get(folder, 'name', folder_name)} in mailbox: {mailbox}")

            # Build query
            items = folder.all().order_by('-datetime_received')

            if unread_only:
                items = items.filter(is_read=False)

            # Fetch emails
            emails = []
            for item in items[:max_results]:
                # Get sender email safely
                sender = safe_get(item, "sender", None)
                from_email = ""
                if sender and hasattr(sender, "email_address"):
                    from_email = sender.email_address or ""

                # Get text body safely (falls back to HTML body / MIME)
                text_body = item_text_body(item)

                email_data = {
                    "message_id": ews_id_to_str(safe_get(item, "id", None)) or "unknown",
                    "subject": safe_get(item, "subject", "") or "",
                    "from": from_email,
                    "to": [r.email_address for r in (safe_get(item, "to_recipients", []) or []) if r and hasattr(r, "email_address") and r.email_address],
                    "cc": [r.email_address for r in (safe_get(item, "cc_recipients", []) or []) if r and hasattr(r, "email_address") and r.email_address],
                    "bcc": [r.email_address for r in (safe_get(item, "bcc_recipients", []) or []) if r and hasattr(r, "email_address") and r.email_address],
                    "received_time": iso_or_none(safe_get(item, "datetime_received", None)),
                    "is_read": safe_get(item, "is_read", False),
                    "flag_status": _item_flag_status(item),
                    "has_attachments": safe_get(item, "has_attachments", False),
                    "preview": truncate_text(text_body, 200)
                }
                if include_body:
                    html_raw = str(safe_get(item, "body", "") or "")
                    try:
                        body_value, body_fmt = render_body(html_raw, text_body, fmt)
                    except ValueError:
                        body_value, body_fmt = text_body, "text"
                    if do_trim_quoted and body_fmt in ("markdown", "text"):
                        body_value = _trim_quoted_body(body_value)
                    email_data["body"] = body_value or text_body
                    email_data["body_format"] = body_fmt
                emails.append(email_data)

            self.logger.info(f"Retrieved {len(emails)} emails from {folder_name} in mailbox: {mailbox}")

            return format_success_response(
                f"Retrieved {len(emails)} emails",
                emails=emails,
                total_count=len(emails),
                folder=folder_name,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to read emails: {e}")
            raise ToolExecutionError(f"Failed to read emails: {e}")


class SearchEmailsTool(BaseTool):
    """Unified email search tool with quick/advanced/full_text modes."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "search_emails",
            "description": "Search emails with quick, advanced, or full-text modes.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["quick", "advanced", "full_text"],
                        "description": "Search mode: quick (filter by subject/sender/date), advanced (multi-folder with sort/categories/importance), full_text (search text in subject/body/attachment names, narrowed by the same filters as quick)",
                        "default": "quick"
                    },
                    "folder": {
                        "type": "string",
                        "description": "Folder to search (quick mode without search_scope). Standard names: inbox, sent, drafts; paths: Inbox/CC; or folder ID",
                        "default": "inbox"
                    },
                    "subject_contains": {
                        "type": "string",
                        "description": "Filter by subject containing text"
                    },
                    "from_address": {
                        "type": "string",
                        "description": "Filter by sender email address"
                    },
                    "sender": {
                        "type": "string",
                        "description": "Alias of from_address"
                    },
                    "to_address": {
                        "type": "string",
                        "description": "Filter by recipient (all modes): a full address must equal one To recipient; anything else matches part of a To address or name"
                    },
                    "recipient": {
                        "type": "string",
                        "description": "Alias of to_address"
                    },
                    "query": {
                        "type": "string",
                        "description": "Quick mode: subject-OR-body substring. Full-text mode: search text (also accepted as search_query). Refused in advanced mode, which uses keywords."
                    },
                    "search_query": {
                        "type": "string",
                        "description": "Legacy alias for 'query' in full_text mode; refused in other modes."
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Result projection. Returned items include only the "
                            "named fields. Default: message_id, subject, from, "
                            "received_time, is_read, flag_status, has_attachments, "
                            "snippet, plus folder for search_scope=['all']. "
                            "Include 'body' / 'body_html' to opt into heavy fields."
                        ),
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": (
                            "Skip this many matches before returning results. "
                            "Pair with the response's ``next_offset`` for paging."
                        ),
                    },
                    "has_attachments": {
                        "type": "boolean",
                        "description": "Filter by attachment presence"
                    },
                    "is_read": {
                        "type": "boolean",
                        "description": "Filter by read status"
                    },
                    "is_flagged": {
                        "type": "boolean",
                        "description": (
                            "Filter by Outlook follow-up flag. true returns only "
                            "messages currently flagged for follow-up; false returns "
                            "only unflagged messages. Maps to the EWS "
                            "PR_FLAG_STATUS extended property (Issue #115); "
                            "false also returns completed messages, so use "
                            "flag_status to tell NotFlagged from Complete."
                        )
                    },
                    "flag_status": {
                        "type": "string",
                        "enum": ["NotFlagged", "Flagged", "Complete"],
                        "description": (
                            "Filter by follow-up flag state (all modes): "
                            "NotFlagged (never flagged or cleared), Flagged, or "
                            "Complete. Combines with is_flagged like any other "
                            "filter. Results report each message's flag_status."
                        )
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Start date (ISO 8601 format)"
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date (ISO 8601 format)"
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results",
                        "default": 50,
                        "maximum": 1000
                    },
                    "keywords": {
                        "type": "string",
                        "description": "Keywords to search in subject and body (advanced mode only; quick and full_text take the text as query)"
                    },
                    "body_contains": {
                        "type": "string",
                        "description": "Filter by body containing text (all modes)"
                    },
                    "importance": {
                        "type": "string",
                        "enum": ["Low", "Normal", "High"],
                        "description": "Filter by importance level (all modes)"
                    },
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by email categories; a message must carry every listed category (all modes)"
                    },
                    "search_scope": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Folders to search, in any mode (quick mode uses it "
                            "instead of folder), e.g. ['inbox', 'sent']. Names: "
                            "inbox, sent, drafts, deleted, junk, or a folder path, "
                            "name or ID from list_folders; a name that matches no "
                            "mail folder is refused. ['all'] searches every mail "
                            "folder of the mailbox in one call, merged into one "
                            "sorted, paged result with each item's folder; "
                            "Deleted Items, Junk, Drafts and Outbox (with their "
                            "subfolders) stay out unless listed next to 'all', "
                            "e.g. ['all', 'deleted']. On Exchange each page reads "
                            "up to offset + max_results + 1 messages per folder, "
                            "a mailbox with more mail folders than "
                            "EWS_SEARCH_ALL_MAX_FOLDERS (default 50) is refused, "
                            "and the response lists folders_searched and "
                            "folders_failed. Graph mailboxes support 'all'; IMAP "
                            "mailboxes search one folder per call."
                        ),
                        "default": ["inbox"]
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["datetime_received", "datetime_sent", "from", "subject", "importance"],
                        "description": "Sort field (quick and advanced modes; full_text results are always newest first)",
                        "default": "datetime_received"
                    },
                    "sort_order": {
                        "type": "string",
                        "enum": ["ascending", "descending"],
                        "description": "Sort order (quick and advanced modes)",
                        "default": "descending"
                    },
                    "search_in": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["subject", "body", "attachments"]},
                        "description": (
                            "Where to search (full_text mode only). The text must "
                            "occur within one selected field; a match in any "
                            "selected field counts. 'attachments' matches "
                            "attachment file names, case-insensitively."
                        ),
                        "default": ["subject", "body"]
                    },
                    "exact_phrase": {
                        "type": "boolean",
                        "description": (
                            "Verify every hit locally as a contiguous, "
                            "case-insensitive phrase within one selected field "
                            "(full_text mode only)"
                        ),
                        "default": False
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to search in (requires impersonation/delegate access)"
                    }
                }
            }
        }

    # Parameter vocabulary accepted across all modes (schema-level).
    # See _validate_kwargs for the strict check. All modes share the filters
    # and search_scope; full_text adds query/search_in/exact_phrase.
    _ALLOWED_PARAMS: set = {
        # Routing
        "mode", "folder", "target_mailbox", "max_results", "offset",
        # Filters shared by every mode, except query (quick, full_text) and
        # keywords (advanced); see _MODE_PARAMS
        "subject_contains", "from_address", "to_address", "sender",
        "recipient", "body_contains", "query",
        "has_attachments", "is_read", "is_flagged", "flag_status", "importance",
        "categories", "keywords",
        "start_date", "end_date",
        # Scope (every mode) and sort (quick and advanced)
        "search_scope", "sort_by", "sort_order",
        # Full-text-only
        "search_query", "search_in", "exact_phrase",
        # Projection
        "fields",
    }

    # Anything in this set gates off the "auto-add last 30 days" default
    # when neither start_date nor end_date was supplied (see _search_quick).
    # A supplied boolean counts even when False: is_flagged=False is a filter.
    _QUICK_FILTER_KEYS: tuple = (
        "subject_contains", "from_address", "sender", "to_address", "recipient",
        "body_contains", "query", "has_attachments", "is_read", "is_flagged",
        "flag_status", "importance", "categories",
    )

    def _validate_kwargs(self, kwargs: Dict[str, Any]) -> None:
        """Reject unknown params with a 400 (ValidationError) + suggestion.

        Previously unknown params were silently ignored, which let the
        ``query`` / ``sender`` filters disappear into the default "last 30
        days" fallback without any error signal for the caller.
        """
        from difflib import get_close_matches

        unknown = [k for k in kwargs.keys() if k not in self._ALLOWED_PARAMS]
        if not unknown:
            return
        first = unknown[0]
        suggestions = get_close_matches(first, sorted(self._ALLOWED_PARAMS), n=1)
        hint = f"; did you mean {suggestions[0]!r}?" if suggestions else ""
        raise ValidationError(
            f"unknown param {first!r}{hint}. "
            f"Accepted params: {', '.join(sorted(self._ALLOWED_PARAMS))}."
        )

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Route to appropriate search mode (serialized, throttle-aware)."""
        self._validate_kwargs(kwargs)
        mode = search_mode(kwargs)
        reject_other_mode_params(kwargs, mode)

        if mode == "advanced":
            run = self._search_advanced
        elif mode == "full_text":
            run = self._search_full_text
        else:
            run = self._search_quick
        return await run_throttled(lambda: run(**dict(kwargs)), self.logger)

    async def _search_quick(self, **kwargs) -> Dict[str, Any]:
        """Quick search: filter by subject, sender, date, read status, attachments.

        Accepts a union of filter keys. ``query`` matches subject OR body.
        ``sender`` is a synonym for ``from_address`` (and ``recipient`` for
        ``to_address``) — the schema previously advertised both spellings
        but only the ``*_address`` forms were wired up. ``search_scope``
        searches its folders instead of ``folder``, merged in sort order.
        """
        import socket

        target_mailbox = kwargs.get("target_mailbox")
        # A folder or scope equal to its schema default (inbox) yields to the
        # other, because some clients fill defaults in; two real choices
        # contradict each other.
        folder_named = str(kwargs.get("folder") or "").strip().lower() not in ("", "inbox")
        scoped = [name.lower() for name in _scope_names(kwargs.get("search_scope"))] not in (
            [], ["inbox"],
        )
        if scoped and folder_named:
            raise ValidationError(
                "quick mode searches either 'folder' or 'search_scope'; pass one of them."
            )
        sort_field, descending = _sort_spec(kwargs)

        # Auto-add date range ONLY when *no* filter was supplied. The
        # Bug 1 regression was that this check only considered four
        # filter keys; query/sender/body_contains/etc. used to fall
        # through the default and get replaced by "last 30 days",
        # silently discarding the caller's intent.
        if not kwargs.get("start_date") and not kwargs.get("end_date"):
            has_filters = any(
                kwargs.get(key) not in (None, "", [])
                for key in self._QUICK_FILTER_KEYS
            )

            if not has_filters:
                from datetime import timedelta
                default_days_back = 30
                auto_start_date = datetime.now() - timedelta(days=default_days_back)
                kwargs["start_date"] = auto_start_date.isoformat()
                self.logger.info(
                    "search_emails quick: no filters and no date range; "
                    f"auto-limiting to last {default_days_back} days"
                )
        filters = _quick_filters(kwargs)
        restriction = filters.restriction()
        text_matcher = filters.matcher()
        max_results = kwargs.get("max_results", 50)
        offset = max(0, int(kwargs.get("offset", 0)))

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            if scoped:
                search_scope = kwargs["search_scope"]
                folders, all_scope = await _resolve_search_scope(account, search_scope)
                if not folders:
                    raise ToolExecutionError(f"No valid folders found in search_scope: {search_scope}")
                fields = _search_fields(kwargs, all_scope=all_scope)
                # The merge reads the sort value from each item.
                only_fields = _with_fields(
                    _db_fields_for(fields), *filters.match_fields,
                    *((sort_field,) if sort_field in _MERGE_SORT_KEYS else ()),
                )
                response = self._search_folders(
                    folders,
                    restriction=restriction,
                    matcher=text_matcher,
                    only_fields=only_fields,
                    sort_field=sort_field,
                    descending=descending,
                    offset=offset,
                    max_results=max_results,
                    fields=fields,
                    tool="search_emails.quick",
                )
                response["folders_searched"] = search_scope
                if all_scope:
                    response.update(_whole_mailbox_report(folders, _folder_errors(response)))
                response["mailbox"] = mailbox
                return format_success_response(
                    f"Found {response['count']} matching emails", **response,
                )

            folder_name = kwargs.get("folder") or "inbox"
            folder = await resolve_folder_for_account(account, folder_name)

            query = folder.all()
            if restriction is not None:
                query = query.filter(restriction)
            query = query.order_by(f"-{sort_field}" if descending else sort_field)

            # Field projection: if the caller supplied ``fields=[...]``,
            # the response items will be restricted to that set. When
            # omitted, we use the list-default (no raw body; snippet only).
            fields = _search_fields(kwargs)
            only_fields = _with_fields(_db_fields_for(fields), *filters.match_fields)
            if only_fields:
                try:
                    query = query.only(*only_fields)
                except Exception as only_exc:
                    self.logger.debug(
                        "query.only(%s) rejected: %s", only_fields, only_exc,
                    )

            folder_label = safe_get(folder, "name", "inbox")
            start_time = datetime.now()
            if text_matcher is None:
                outcome = _paginate_query(
                    query,
                    max_results=max_results,
                    offset=offset,
                    chunk_size=50,
                    logger=self.logger,
                    folder_label=folder_label,
                )
            else:
                outcome = _paginate_query_matching(
                    query,
                    matcher=text_matcher,
                    max_results=max_results,
                    offset=offset,
                    chunk_size=50,
                    logger=self.logger,
                    folder_label=folder_label,
                )

            emails: List[Dict[str, Any]] = [
                _build_list_item(e, fields=fields, folder_name=folder_label)
                for e in outcome.items
            ]
            duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            ews_call_log(
                self.logger, "FindItem",
                duration_ms=duration_ms,
                result_count=len(emails),
                total_available=outcome.total_available,
                page_offset=offset,
                folder=folder_label,
                outcome="ok" if outcome.error_code is None else outcome.error_code,
                extra_fields={"tool": "search_emails.quick"},
            )

            response: Dict[str, Any] = {
                "items": emails,
                "count": len(emails),
                "total_available": outcome.total_available,
                "mailbox": mailbox,
            }
            if (
                outcome.total_available is not None
                and len(emails) + offset < outcome.total_available
            ):
                response["next_offset"] = offset + len(emails)
            if outcome.error_code:
                response["meta"] = {
                    "error_code": outcome.error_code,
                    "error_message": outcome.error_message,
                }

            return format_success_response(
                f"Found {len(emails)} matching emails",
                **response,
            )

        except (ErrorTimeoutExpired, socket.timeout) as e:
            self.logger.error(f"Search timed out: {e}")
            raise ToolExecutionError(
                f"Search timed out. Try adding start_date/end_date, reducing max_results, or adding more filters."
            )
        except (ValidationError, ToolExecutionError):
            raise
        except Exception as e:
            self.logger.error(f"Failed to search emails: {e}")
            raise ToolExecutionError(f"Failed to search emails: {e}")

    def _search_folders(
        self,
        folders: List[Tuple[str, Any]],
        *,
        restriction: Any,
        matcher: Any,
        only_fields: tuple,
        sort_field: str,
        descending: bool,
        offset: int,
        max_results: int,
        fields: List[str],
        tool: str,
    ) -> Dict[str, Any]:
        """Search every ``(label, folder)`` and merge one sorted page.

        Returns the response fields every multi-folder search shares: items,
        count, total_available (None when a folder's count is unknown),
        next_offset when more follow, and meta.per_folder_errors. A failing
        folder is reported there and never fails the search.
        """
        per_folder: List[Tuple[str, List[Any]]] = []
        per_folder_errors: List[Dict[str, Any]] = []
        total_available_sum = 0
        some_total_unknown = False
        fetch_offset, fetch_limit = _folder_fetch_window(len(folders), offset, max_results)
        order = f"-{sort_field}" if descending else sort_field
        start_time = datetime.now()

        for folder_name, folder in folders:
            try:
                query = folder.filter(restriction) if restriction is not None else folder.all()
                if only_fields:
                    try:
                        query = query.only(*only_fields)
                    except Exception as only_exc:
                        # .only() can reject unknown fields on older
                        # exchangelib versions — fall back to full
                        # projection rather than fail the search.
                        self.logger.debug(
                            "query.only(%s) rejected on %s: %s",
                            only_fields, folder_name, only_exc,
                        )
                query = query.order_by(order)
            except Exception as setup_exc:
                # Filter/sort setup itself failed — classify and carry.
                per_folder_errors.append({
                    "folder": folder_name,
                    "error_code": _classify_ews_error(setup_exc),
                    "error_message": f"{type(setup_exc).__name__}: {setup_exc}",
                })
                self.logger.warning(
                    "%s setup on %s failed: %s: %s",
                    tool, folder_name, type(setup_exc).__name__, setup_exc,
                )
                some_total_unknown = True
                continue

            if matcher is None:
                outcome = _paginate_query(
                    query,
                    max_results=fetch_limit,
                    offset=fetch_offset,
                    chunk_size=50,
                    logger=self.logger,
                    folder_label=folder_name,
                )
            else:
                outcome = _paginate_query_matching(
                    query,
                    matcher=matcher,
                    max_results=fetch_limit,
                    offset=fetch_offset,
                    chunk_size=50,
                    logger=self.logger,
                    folder_label=folder_name,
                )
            ews_call_log(
                self.logger, "FindItem",
                result_count=len(outcome.items),
                total_available=outcome.total_available,
                page_offset=fetch_offset,
                folder=folder_name,
                outcome="ok" if outcome.error_code is None else outcome.error_code,
                error_type=(
                    outcome.error_message.split(":", 1)[0]
                    if outcome.error_message else None
                ),
                extra_fields={"tool": tool},
            )

            if outcome.total_available is None:
                some_total_unknown = True
            else:
                total_available_sum += outcome.total_available
            if outcome.error_code:
                per_folder_errors.append({
                    "folder": folder_name,
                    "error_code": outcome.error_code,
                    "error_message": outcome.error_message,
                })
            per_folder.append((folder_name, outcome.items))

        page, has_more = _merge_folder_page(
            per_folder,
            sort_field=sort_field,
            descending=descending,
            skip=offset - fetch_offset,
            limit=max_results,
        )
        items = [
            _build_list_item(item, fields=fields, folder_name=name) for name, item in page
        ]
        # A sum across folders when every folder reported a count; None if
        # at least one was unknown.
        total_available = None if some_total_unknown else total_available_sum
        response: Dict[str, Any] = {
            "items": items,
            "count": len(items),
            "total_available": total_available,
        }
        if has_more:
            response["next_offset"] = offset + len(items)
        if per_folder_errors:
            response["meta"] = {"per_folder_errors": per_folder_errors}

        ews_call_log(
            self.logger, f"{tool}_total",
            duration_ms=int((datetime.now() - start_time).total_seconds() * 1000),
            result_count=len(items),
            total_available=total_available,
            page_offset=offset,
            outcome="ok" if not per_folder_errors else "partial",
            extra_fields={"folders": len(folders)},
        )
        return response

    async def _search_advanced(self, **kwargs) -> Dict[str, Any]:
        """Advanced search: multi-folder with sort, categories, importance, keywords.

        Takes the same filters as quick mode (aliases included) plus keywords,
        which match subject OR body.
        """
        target_mailbox = kwargs.get("target_mailbox")
        search_scope = kwargs.get("search_scope", ["inbox"])
        max_results = kwargs.get("max_results", 250)
        sort_field, descending = _sort_spec(kwargs)
        filters = _quick_filters(kwargs, free_text=False)
        if kwargs.get("keywords"):
            keywords = kwargs["keywords"]
            filters.restrictions.insert(
                0, Q(subject__contains=keywords) | Q(body__contains=keywords),
            )
        if not filters.restrictions and not filters.matchers:
            raise ToolExecutionError("No valid search filters provided for advanced mode")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            folders, all_scope = await _resolve_search_scope(account, search_scope)
            if not folders:
                raise ToolExecutionError(f"No valid folders found in search_scope: {search_scope}")

            # Field projection (Bug 6). Default: no body. The merge reads the
            # sort value from each item.
            fields = _search_fields(kwargs, all_scope=all_scope)
            offset = max(0, int(kwargs.get("offset", 0)))
            only_fields = _with_fields(
                _db_fields_for(fields), *filters.match_fields,
                *((sort_field,) if sort_field in _MERGE_SORT_KEYS else ()),
            )

            response = self._search_folders(
                folders,
                restriction=filters.restriction(),
                matcher=filters.matcher(),
                only_fields=only_fields,
                sort_field=sort_field,
                descending=descending,
                offset=offset,
                max_results=max_results,
                fields=fields,
                tool="search_emails.advanced",
            )
            response["folders_searched"] = search_scope
            if all_scope:
                response.update(_whole_mailbox_report(folders, _folder_errors(response)))
            response["mailbox"] = mailbox
            return format_success_response(
                f"Found {response['count']} result(s)",
                **response,
            )

        except (ValidationError, ToolExecutionError):
            raise
        except Exception as e:
            self.logger.exception(f"Failed to perform advanced search: {e}")
            raise ToolExecutionError(
                f"Failed to perform advanced search: {type(e).__name__}: {e}"
            )

    async def _search_full_text(self, **kwargs) -> Dict[str, Any]:
        """Full-text search across subject, body, and attachment names.

        The quick-mode filters (sender, recipient, dates, read, flag,
        attachments, importance, categories, subject/body) narrow it further.
        """
        target_mailbox = kwargs.get("target_mailbox")
        # Accept both ``query`` (current preferred name) and ``search_query``
        # (legacy schema name some callers are pinned to). If both are set,
        # ``query`` wins — that matches the shared-mode semantics used by
        # _search_quick.
        query_text = kwargs.get("query") or kwargs.get("search_query")
        search_scope = kwargs.get("search_scope", ["inbox", "sent"])
        max_results = kwargs.get("max_results", 50)
        exact_phrase = kwargs.get("exact_phrase", False)

        if not query_text:
            # ValidationError maps to HTTP 400 in the openapi_adapter; raising
            # ToolExecutionError caused this to surface as HTTP 500 (Bug 2).
            raise ValidationError(
                "query is required for full_text mode "
                "(accepted under 'query' or 'search_query')"
            )
        search_in = kwargs.get("search_in")
        search_in = _full_text_areas(["subject", "body"] if search_in is None else search_in)
        filters = _quick_filters(kwargs, free_text=False)
        if exact_phrase or "attachments" in search_in:
            # Verified client-side during the scan, not after it, so pages
            # and totals count real matches. The projection loads what the
            # check reads, including attachment names but not their content.
            filters.matchers.insert(0, _full_text_matcher(query_text, search_in))
            filters.match_fields += tuple(
                db_field for area in search_in for db_field in _FULL_TEXT_MATCH_FIELDS[area]
            )

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            folders_to_search, all_scope = await _resolve_search_scope(account, search_scope)
            if not folders_to_search:
                raise ToolExecutionError("No valid folders to search")

            # Field projection (Bug 6). Default: list-default keys, no body.
            fields = _search_fields(kwargs, all_scope=all_scope)
            offset = max(0, int(kwargs.get("offset", 0)))

            # EWS restricts subject and body text but not attachment names,
            # so the attachments area asks for every message with attachments
            # and its names are checked locally. Selected areas are ORed; the
            # other filters must all hold as well.
            area_filters = {
                "subject": Q(subject__contains=query_text),
                "body": Q(body__contains=query_text),
                "attachments": Q(has_attachments=True),
            }
            combined_filter = area_filters[search_in[0]]
            for area in search_in[1:]:
                combined_filter |= area_filters[area]
            other_filters = filters.restriction()
            if other_filters is not None:
                combined_filter &= other_filters

            # The merge orders by received time.
            only_fields = _with_fields(
                _db_fields_for(fields), *filters.match_fields, "datetime_received",
            )
            response = self._search_folders(
                folders_to_search,
                restriction=combined_filter,
                matcher=filters.matcher(),
                only_fields=only_fields,
                sort_field="datetime_received",
                descending=True,
                offset=offset,
                max_results=max_results,
                fields=fields,
                tool="search_emails.full_text",
            )
            response["query"] = query_text
            response["searched_folders"] = search_scope
            if all_scope:
                response.update(
                    _whole_mailbox_report(folders_to_search, _folder_errors(response))
                )
            response["mailbox"] = mailbox
            return format_success_response(
                f"Found {response['count']} emails matching '{query_text}'",
                **response,
            )

        except (ValidationError, ToolExecutionError):
            raise
        except Exception as e:
            self.logger.exception(f"Failed to perform full-text search: {e}")
            raise ToolExecutionError(
                f"Failed to perform full-text search: {type(e).__name__}: {e}"
            )


class GetEmailDetailsTool(BaseTool):
    """Tool for getting full email details."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "get_email_details",
            "description": "Get full details of a specific email by ID.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Email message ID"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to access (requires impersonation/delegate access)"
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional field projection. When supplied, the "
                            "response 'email' object contains only these "
                            "fields. Default (no fields param): the full email "
                            "shape is returned unchanged for backward "
                            "compatibility."
                        ),
                    },
                    **READ_FORMAT_SCHEMA,
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Get email details."""
        message_id = kwargs.get("message_id")
        target_mailbox = kwargs.get("target_mailbox")
        fields = kwargs.get("fields")  # None -> full detail shape
        fmt = kwargs.get("format") or DEFAULT_READ_FORMAT
        do_trim_quoted = bool(kwargs.get("trim_quoted", False))
        include_body = bool(kwargs.get("include_body", True))

        # Validate up front. Previously a missing or empty message_id fell
        # through to ``find_message_for_account(account, None)`` which
        # raised a generic exception and surfaced as HTTP 500. A missing
        # required field is a caller error, not a server crash, so raise
        # ValidationError which the openapi_adapter maps to HTTP 400.
        if not message_id or not isinstance(message_id, str) or not message_id.strip():
            raise ValidationError(
                "message_id is required and must be a non-empty string"
            )

        try:
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Find message across all folders (including custom subfolders)
            item = find_message_for_account(account, message_id)

            # Get sender email safely
            sender = safe_get(item, "sender", None)
            from_email = ""
            if sender and hasattr(sender, "email_address"):
                from_email = sender.email_address or ""

            # Get recipients safely
            to_recipients = safe_get(item, "to_recipients", []) or []
            to_emails = [r.email_address for r in to_recipients if r and hasattr(r, "email_address") and r.email_address]

            cc_recipients = safe_get(item, "cc_recipients", []) or []
            cc_emails = [r.email_address for r in cc_recipients if r and hasattr(r, "email_address") and r.email_address]

            # Get attachments safely
            attachments = safe_get(item, "attachments", []) or []
            attachment_names = [att.name for att in attachments if att and hasattr(att, "name") and att.name]

            html_body_raw = str(safe_get(item, "body", "") or "")
            text_body_raw = item_text_body(item)
            if not html_body_raw.strip() and not text_body_raw.strip():
                # Last resort: decode the MIME source on demand.
                try:
                    from ..utils import mime_text_body
                    full = account.fetch(ids=[item], only_fields=["mime_content"])
                    full = next(iter(full), None)
                    text_body_raw = mime_text_body(safe_get(full, "mime_content", None))
                except Exception as mime_exc:
                    self.logger.debug("MIME body fallback failed: %s", mime_exc)

            # Default fmt='markdown' (~12x cheaper than Outlook HTML).
            # fmt='html' is the explicit escape hatch for the raw body.
            # fmt='text' returns text_body only.
            mid_str = ews_id_to_str(safe_get(item, "id", None)) or "unknown"
            cache = getattr(self.ews_client, "sqlite_cache", None)
            body_value = text_body_raw
            body_format_used = DEFAULT_READ_FORMAT
            if include_body:
                if fmt == "html":
                    body_value = html_body_raw
                    body_format_used = "html"
                elif fmt == "text":
                    body_value = text_body_raw
                    body_format_used = "text"
                elif fmt == "markdown":
                    cached = cache.get_body(mid_str, "markdown") if cache else None
                    if cached is not None:
                        body_value = cached
                        body_format_used = "markdown"
                    else:
                        rendered, actual = render_body(
                            html_body_raw, text_body_raw, "markdown"
                        )
                        body_value = rendered
                        body_format_used = actual
                        if cache and actual == "markdown":
                            cache.put_body(mid_str, "markdown", rendered)
                if do_trim_quoted and body_format_used in ("markdown", "text"):
                    body_value = _trim_quoted_body(body_value)

            # v4.0 — body_html is sent ONLY when fmt=='html' AND include_body.
            # Shipping it on every call defeats the 12x token-saving promise:
            # a caller asking for format=markdown got both the markdown body
            # and the full HTML in the same response. Now markdown callers
            # get just the markdown; html callers get the html in `body`
            # (we still keep body_html as an explicit alias on html mode for
            # any v3.4 caller that read body_html directly rather than body).
            ship_body_html = include_body and body_format_used == "html"

            email_details = {
                "message_id": mid_str,
                "subject": safe_get(item, "subject", "") or "",
                "from": from_email,
                "to": to_emails,
                "cc": cc_emails,
                "body": body_value if include_body else "",
                "body_format": body_format_used if include_body else "omitted",
                "body_html": html_body_raw if ship_body_html else "",
                "received_time": iso_or_none(safe_get(item, "datetime_received", None)),
                "sent_time": iso_or_none(safe_get(item, "datetime_sent", None))
                or iso_or_none(safe_get(item, "datetime_received", None)),
                "is_read": safe_get(item, "is_read", False),
                "flag_status": _item_flag_status(item),
                "has_attachments": safe_get(item, "has_attachments", False),
                "importance": safe_get(item, "importance", "Normal") or "Normal",
                "attachments": attachment_names,
            }

            # Projection: when fields=[...] is provided, return only those
            # keys inside the ``email`` object. Default (no fields) keeps
            # the full shape for backward-compat with existing callers —
            # this is one of the non-negotiable invariants.
            if fields:
                email_details = project_fields(email_details, fields)

            return format_success_response(
                "Email details retrieved",
                email=email_details,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to get email details: {e}")
            raise ToolExecutionError(f"Failed to get email details: {e}")


class GetEmailsBulkTool(BaseTool):
    """Fetch multiple messages in a single EWS round-trip (Issue 5).

    Motivation: clients wanting a weekly-report view had to call
    ``get_email_details`` N times for N messages — O(N) HTTP requests at
    ~1s each. ``account.fetch`` issues a single ``GetItem`` batch so 50
    messages come back in one network hop.
    """

    # Hard ceiling: the EWS server rejects very large GetItem batches,
    # and callers who need >100 items should page explicitly.
    _MAX_MESSAGES_HARD_CAP = 100

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "get_emails_bulk",
            "description": (
                "Fetch multiple emails by ID in one EWS round-trip. "
                "Uses exchangelib's account.fetch() batch API so N "
                "messages cost one HTTP call, not N."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Message IDs to fetch (from search_emails / list_attachments)",
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": list(LIST_DEFAULT_FIELDS),
                        "description": (
                            "Projection, same vocabulary as list tools. "
                            "Default: list-shaped fields (no raw body; "
                            "snippet only). Pass fields=[...] to opt into "
                            "extra keys such as body / body_html."
                        ),
                    },
                    "max_messages": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": self._MAX_MESSAGES_HARD_CAP,
                        "default": 50,
                        "description": (
                            "Cap on input list size. Default 50, hard cap "
                            f"{self._MAX_MESSAGES_HARD_CAP}. Use paging for larger sets."
                        ),
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)",
                    },
                },
                "required": ["message_ids"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        message_ids = kwargs.get("message_ids") or []
        if not isinstance(message_ids, list) or not message_ids:
            raise ValidationError("message_ids must be a non-empty list")
        # Dedupe while preserving order. Strings only.
        seen: set = set()
        clean_ids: List[str] = []
        for raw in message_ids:
            if not isinstance(raw, str):
                raise ValidationError(
                    f"message_ids must be strings, got {type(raw).__name__}"
                )
            if raw in seen:
                continue
            seen.add(raw)
            clean_ids.append(raw)

        max_messages = int(kwargs.get("max_messages", 50))
        if max_messages < 1:
            raise ValidationError("max_messages must be >= 1")
        if max_messages > self._MAX_MESSAGES_HARD_CAP:
            max_messages = self._MAX_MESSAGES_HARD_CAP
        if len(clean_ids) > max_messages:
            raise ValidationError(
                f"received {len(clean_ids)} ids but max_messages={max_messages}. "
                f"Reduce the list or split into pages."
            )

        target_mailbox = kwargs.get("target_mailbox")
        fields = kwargs.get("fields") or list(LIST_DEFAULT_FIELDS)
        account = self.get_account(target_mailbox)
        mailbox = self.get_mailbox_info(target_mailbox)

        # Single GetItem batch — O(1) round trips regardless of N.
        start_time = datetime.now()
        try:
            fetched = list(account.fetch([Message(id=mid) for mid in clean_ids]))
        except Exception as exc:
            self.logger.exception(
                "get_emails_bulk: batch fetch failed: %s: %s",
                type(exc).__name__, exc,
            )
            raise ToolExecutionError(
                f"Bulk fetch failed: {type(exc).__name__}: {exc}"
            )
        duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)

        items: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        # exchangelib returns items in input order; pair by index so we
        # can surface per-id errors correctly.
        for requested_id, fetched_item in zip(clean_ids, fetched):
            if isinstance(fetched_item, BaseException):
                errors.append({
                    "message_id": requested_id,
                    "error_code": (
                        "NOT_FOUND"
                        if "ErrorItemNotFound" in type(fetched_item).__name__
                        else "FETCH_ERROR"
                    ),
                    "error_message": f"{type(fetched_item).__name__}: {fetched_item}",
                })
                continue
            item_dict = self._message_to_dict(fetched_item, fields=fields)
            items.append(item_dict)

        # If exchangelib returned a shorter list than we asked for
        # (shouldn't happen with account.fetch, but be defensive), mark
        # the missing ids.
        if len(fetched) < len(clean_ids):
            missing = clean_ids[len(fetched):]
            for mid in missing:
                errors.append({
                    "message_id": mid,
                    "error_code": "NOT_FOUND",
                    "error_message": "no response entry for id",
                })

        ews_call_log(
            self.logger, "GetItem",
            duration_ms=duration_ms,
            result_count=len(items),
            outcome="ok" if not errors else "partial",
            extra_fields={
                "tool": "get_emails_bulk",
                "requested": len(clean_ids),
                "errors": len(errors),
            },
        )

        return format_success_response(
            f"Fetched {len(items)} of {len(clean_ids)} message(s)",
            items=items,
            count=len(items),
            requested=len(clean_ids),
            errors=errors,
            mailbox=mailbox,
        )

    @staticmethod
    def _message_to_dict(
        message: Any, *, fields: Optional[List[str]],
    ) -> Dict[str, Any]:
        """Render an exchangelib Message to the canonical email dict.

        Shape matches ``GetEmailDetailsTool`` so callers can swap in
        this batch tool without changing their downstream parser.
        """
        sender = safe_get(message, "sender", None)
        from_email = getattr(sender, "email_address", "") or ""

        to_recipients = safe_get(message, "to_recipients", []) or []
        to_emails = [
            r.email_address for r in to_recipients
            if hasattr(r, "email_address") and r.email_address
        ]
        cc_recipients = safe_get(message, "cc_recipients", []) or []
        cc_emails = [
            r.email_address for r in cc_recipients
            if hasattr(r, "email_address") and r.email_address
        ]
        attachments = safe_get(message, "attachments", []) or []
        attachment_names = [
            att.name for att in attachments if hasattr(att, "name") and att.name
        ]

        received = safe_get(message, "datetime_received", None)
        sent = safe_get(message, "datetime_sent", None)

        email_details = {
            "message_id": ews_id_to_str(safe_get(message, "id", None)) or "unknown",
            "subject": safe_get(message, "subject", "") or "",
            "from": from_email,
            "to": to_emails,
            "cc": cc_emails,
            "body": safe_get(message, "text_body", "") or "",
            "body_html": str(safe_get(message, "body", "") or ""),
            "received_time": received.isoformat() if received and hasattr(received, "isoformat") else None,
            "sent_time": sent.isoformat() if sent and hasattr(sent, "isoformat") else None,
            "is_read": safe_get(message, "is_read", False),
            "has_attachments": safe_get(message, "has_attachments", False),
            "importance": safe_get(message, "importance", "Normal") or "Normal",
            "attachments": attachment_names,
        }
        email_details["snippet"] = truncate_text(email_details["body"], 200)
        return project_fields(email_details, fields)


class DeleteEmailTool(BaseTool):
    """Tool for deleting emails."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "delete_email",
            "description": (
                "Delete an email by ID. Default is soft delete (moves to "
                "Deleted Items). Set ``permanent`` (or ``hard_delete`` "
                "alias) true to bypass Trash and remove permanently."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Email message ID to delete"
                    },
                    "permanent": {
                        "type": "boolean",
                        "description": "Permanently delete (bypasses Trash). Alias: hard_delete.",
                        "default": False
                    },
                    "hard_delete": {
                        "type": "boolean",
                        "description": "Alias for 'permanent'.",
                        "default": False
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to delete from (requires impersonation/delegate access)"
                    }
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Delete email."""
        message_id = kwargs.get("message_id")
        # Accept both ``permanent`` (canonical) and ``hard_delete`` (alias
        # matching manage_folder / callers' muscle memory). Either truthy
        # value triggers a permanent delete.
        permanent = bool(
            kwargs.get("permanent", False) or kwargs.get("hard_delete", False)
        )
        target_mailbox = kwargs.get("target_mailbox")

        if not message_id or not isinstance(message_id, str) or not message_id.strip():
            raise ValidationError("message_id is required")

        try:
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Find message across all folders (including custom subfolders)
            item = find_message_for_account(account, message_id)

            if permanent:
                # In exchangelib 5.x, ``Item.delete()`` is already a
                # HardDelete by default — its body calls ``self._delete(
                # delete_type=HARD_DELETE, ...)`` internally and the public
                # method takes no ``disposal_type``/``delete_type`` kwarg.
                # Earlier versions of this code passed ``disposal_type=``,
                # which raised ``TypeError`` against exchangelib 5.x and
                # surfaced as a 500 from delete_email(permanent=True).
                # See ``Item.delete`` in exchangelib/items/item.py.
                item.delete()
                action = "permanently deleted"
            else:
                # Move to trash (Deleted Items) so the user can recover.
                item.move(account.trash)
                action = "moved to trash"

            self.logger.info(f"Email {message_id} {action} in mailbox: {mailbox}")

            return format_success_response(
                f"Email {action}",
                message_id=message_id,
                permanent=permanent,
                hard_delete=permanent,
                mailbox=mailbox
            )

        except (ValidationError, ToolExecutionError):
            raise
        except Exception as e:
            self.logger.exception(f"Failed to delete email: {type(e).__name__}: {e}")
            raise ToolExecutionError(
                f"Failed to delete email: {type(e).__name__}: {e}"
            )


class MoveEmailTool(BaseTool):
    """Tool for moving emails between folders."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "move_email",
            "description": "Move an email to a different folder.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Email message ID to move"
                    },
                    "destination_folder": {
                        "type": "string",
                        "description": "Destination folder name or path (e.g. inbox, Inbox/Projects)"
                    },
                    "destination_folder_id": {
                        "type": "string",
                        "description": "Destination folder ID (alternative to destination_folder)"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Move email to folder."""
        message_id = kwargs.get("message_id")
        destination_folder = kwargs.get("destination_folder")
        destination_folder_id = kwargs.get("destination_folder_id")
        target_mailbox = kwargs.get("target_mailbox")

        if not message_id:
            raise ToolExecutionError("message_id is required")
        if not destination_folder and not destination_folder_id:
            raise ToolExecutionError("Either destination_folder or destination_folder_id is required")

        try:
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Issue #112: when ``destination_folder_id`` is supplied, resolve
            # by ID directly rather than running through the generic resolver.
            # The generic resolver does path / name / prefix heuristics that
            # waste round-trips and can mis-route on folders whose display
            # names happen to look like an Exchange ID. Explicit ID inputs
            # should be a single ID lookup.
            if destination_folder_id:
                dest_folder = find_folder_by_id(account.root, destination_folder_id)
                if dest_folder is None:
                    raise ToolExecutionError(
                        f"destination_folder_id not found: {destination_folder_id}"
                    )
                dest_name = safe_get(dest_folder, "name", destination_folder_id)
            else:
                dest_folder = await resolve_folder_for_account(account, destination_folder)
                dest_name = safe_get(dest_folder, "name", destination_folder)

            # Find message across all folders (including custom subfolders)
            item = find_message_for_account(account, message_id)
            item.move(dest_folder)

            self.logger.info(f"Email {message_id} moved to {dest_name} in mailbox: {mailbox}")

            return format_success_response(
                f"Email moved to {dest_name}",
                message_id=message_id,
                destination_folder=dest_name,
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to move email: {e}")
            raise ToolExecutionError(f"Failed to move email: {e}")


class UpdateEmailTool(BaseTool):
    """Tool for updating email properties."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "update_email",
            "description": "Update email properties (read status, flags, categories, importance).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Email message ID"
                    },
                    "is_read": {
                        "type": "boolean",
                        "description": "Mark as read (true) or unread (false)"
                    },
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Email categories (labels)"
                    },
                    "flag_status": {
                        "type": "string",
                        "enum": ["NotFlagged", "Flagged", "Complete"],
                        "description": "Flag status"
                    },
                    "importance": {
                        "type": "string",
                        "enum": ["Low", "Normal", "High"],
                        "description": "Email importance level"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Update email properties."""
        message_id = kwargs.get("message_id")
        target_mailbox = kwargs.get("target_mailbox")

        if not message_id:
            raise ToolExecutionError("message_id is required")

        try:
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Find the message across common folders
            message = find_message_for_account(account, message_id)

            # Track what was updated. The field names double as the EWS
            # update_fields list: a bare save() asks exchangelib to update
            # EVERY writable Message field, which pushes unloaded/unset values
            # back to the server.
            updates = {}
            changed_fields = []

            # Update read status
            if "is_read" in kwargs:
                message.is_read = kwargs["is_read"]
                updates["is_read"] = kwargs["is_read"]
                changed_fields.append("is_read")

            # Update categories
            if "categories" in kwargs:
                message.categories = kwargs["categories"]
                updates["categories"] = kwargs["categories"]
                changed_fields.append("categories")

            # Update flag status using ExtendedProperty
            if "flag_status" in kwargs:
                flag_value = FLAG_STATUS_MAP.get(kwargs["flag_status"])
                if kwargs["flag_status"] not in FLAG_STATUS_MAP:
                    raise ToolExecutionError(
                        f"Invalid flag_status: {kwargs['flag_status']}. "
                        f"Valid values: {', '.join(FLAG_STATUS_MAP.keys())}"
                    )
                message.flag_status_value = flag_value
                updates["flag_status"] = kwargs["flag_status"]
                changed_fields.append("flag_status_value")

            # Update importance
            if "importance" in kwargs:
                message.importance = kwargs["importance"]
                updates["importance"] = kwargs["importance"]
                changed_fields.append("importance")

            # Save changes
            if changed_fields:
                message.save(update_fields=changed_fields)

            self.logger.info(f"Email {message_id} updated in mailbox {mailbox}: {updates}")

            return format_success_response(
                "Email updated successfully",
                message_id=message_id,
                updates=updates,
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to update email: {e}")
            raise ToolExecutionError(f"Failed to update email: {e}")


class CopyEmailTool(BaseTool):
    """Tool for copying emails to another folder."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "copy_email",
            "description": "Copy an email to another folder.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Email message ID to copy"
                    },
                    "destination_folder": {
                        "type": "string",
                        "description": "Destination folder name",
                        "enum": ["inbox", "sent", "drafts", "deleted", "junk"]
                    },
                    "destination_folder_id": {
                        "type": "string",
                        "description": "Destination folder ID (alternative to destination_folder)"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Copy email to another folder."""
        message_id = kwargs.get("message_id")
        destination_folder_name = kwargs.get("destination_folder")
        destination_folder_id = kwargs.get("destination_folder_id")
        target_mailbox = kwargs.get("target_mailbox")

        if not message_id:
            raise ToolExecutionError("message_id is required")

        if not destination_folder_name and not destination_folder_id:
            raise ToolExecutionError("Either destination_folder or destination_folder_id is required")

        try:
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            message = find_message_for_account(account, message_id)
            source_folder_name = safe_get(safe_get(message, "folder", None), "name", "unknown")

            # Issue #112: explicit destination_folder_id resolves directly,
            # skipping the generic name/path resolver.
            if destination_folder_id:
                destination_folder = find_folder_by_id(account.root, destination_folder_id)
                if destination_folder is None:
                    raise ToolExecutionError(
                        f"destination_folder_id not found: {destination_folder_id}"
                    )
                dest_name = safe_get(destination_folder, "name", destination_folder_id)
            else:
                destination_folder = await resolve_folder_for_account(account, destination_folder_name)
                dest_name = safe_get(destination_folder, "name", destination_folder_name)

            # Copy the message (exchangelib uses .copy() method)
            copied_message = message.copy(to_folder=destination_folder)

            subject = safe_get(message, 'subject', 'No Subject')

            self.logger.info(f"Copied email '{subject}' from {source_folder_name} to {dest_name} in mailbox: {mailbox}")

            # exchangelib returns either a Message-like object (with .id) or
            # the bare ItemId after a copy. Handle both — earlier versions
            # always emitted "" because the bare ItemId path returned empty.
            if copied_message is None:
                copied_id_str = ""
            else:
                inner = safe_get(copied_message, "id", None)
                copied_id_str = ews_id_to_str(inner) or ews_id_to_str(copied_message) or ""

            return format_success_response(
                f"Email copied from {source_folder_name} to {dest_name}",
                message_id=message_id,
                copied_message_id=copied_id_str,
                subject=subject,
                source_folder=source_folder_name,
                destination_folder=dest_name,
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to copy email: {e}")
            raise ToolExecutionError(f"Failed to copy email: {e}")


class ReplyEmailTool(BaseTool):
    """Tool for replying to emails while preserving conversation thread."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "reply_email",
            "description": "Reply to an email, preserving the conversation thread.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The message ID of the email to reply to in target_mailbox. For IMAP, also supply its folder when it is not inbox."
                    },
                    "body": {
                        "type": "string",
                        "description": "The reply body. Format determined by body_format (default: html)."
                    },
                    "reply_all": {
                        "type": "boolean",
                        "description": "If true, reply to all recipients; if false, reply only to sender",
                        "default": False
                    },
                    "cc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Additional Cc recipients. Added to Cc beside the "
                            "derived recipients: the sender in To, plus on "
                            "reply_all the original To in To and the original Cc "
                            "in Cc. Each address appears once across To, Cc and "
                            "Bcc, compared case-insensitively."
                        ),
                    },
                    "bcc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Blind-copy recipients. Bcc holds only these "
                            "addresses; the original message's Bcc is never "
                            "copied."
                        ),
                    },
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Exchange-only server-local file paths; use files on any supported provider."
                    },
                    "files": FILES_SCHEMA,
                    **INLINE_ATTACHMENTS_SCHEMA,
                    **BODY_FORMAT_SCHEMA,
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to reply from (requires impersonation/delegate access)"
                    },
                    "dry_run": dry_run_schema("sending the reply"),
                    "verify_files": VERIFY_FILES_SCHEMA,
                },
                "required": ["message_id", "body"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Reply to an email via EWS."""
        message_id = kwargs.get("message_id")
        body = kwargs.get("body", "").strip()
        body_format = kwargs.get("body_format", "html")
        reply_all = kwargs.get("reply_all", False)
        attachments = kwargs.get("attachments", [])
        target_mailbox = kwargs.get("target_mailbox")
        dry_run = bool(kwargs.get("dry_run", False))
        fetch_files = not dry_run or bool(kwargs.get("verify_files"))
        extra_cc = address_list(kwargs.get("cc"), "cc")
        extra_bcc = address_list(kwargs.get("bcc"), "bcc")

        if not message_id:
            raise ToolExecutionError("message_id is required")
        if not body:
            raise ToolExecutionError("body is required")

        # v4.0: convert markdown/text body to HTML before the existing reply
        # pipeline runs. The signature-preservation path (copy_attachments_to_message
        # below) is unaffected — we only change what goes into format_body_for_html().
        if body_format != "html":
            body, _actual_fmt = compose_body(body, body_format)

        try:
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            tracking_id = new_outbound_tracking_id()
            file_entries = validate_outgoing_files(kwargs.get("files"))
            outgoing_files = await load_outgoing_files(
                file_entries,
                storage_client=lambda: smartermail_client(self.ews_client, target_mailbox),
            ) if fetch_files else []
            mailbox = self.get_mailbox_info(target_mailbox)

            # Find the original message across folders
            original_message = find_message_for_account(account, message_id)

            # Get original message details for the response
            original_subject = safe_get(original_message, "subject", "") or ""
            original_sender = safe_get(original_message, "sender", None)
            original_from_email = ""
            if original_sender and hasattr(original_sender, "email_address"):
                original_from_email = original_sender.email_address or ""

            # Get original recipients for reply-all
            original_to = [r.email_address for r in (safe_get(original_message, "to_recipients", []) or [])
                          if r and hasattr(r, "email_address") and r.email_address]
            original_cc = [r.email_address for r in (safe_get(original_message, "cc_recipients", []) or [])
                         if r and hasattr(r, "email_address") and r.email_address]

            # IMPORTANT: DO NOT use create_reply()/create_reply_all() - they auto-append content we can't control
            # This causes duplication and wrong order issues with Exclaimer signature placement.
            # Instead, always create a fresh Message with manually constructed body.
            self.logger.info("Creating fresh Message for reply (not using create_reply)")

            # Get the reply subject (avoid duplicate RE: prefix)
            reply_subject = add_reply_prefix(original_subject)

            # Determine recipients for the reply. Reply-all keeps the original
            # Cc in Cc rather than promoting it to To; the caller's cc/bcc are
            # added on top, as create_reply_draft does.
            reply_to_list, reply_cc_list, reply_bcc_list = reply_recipients(
                original_from_email,
                original_to,
                original_cc,
                account.primary_smtp_address,
                reply_all=reply_all,
                extra_cc=extra_cc,
                extra_bcc=extra_bcc,
            )
            if reply_all:
                self.logger.info(
                    f"Reply-all to {len(reply_to_list)} recipient(s), "
                    f"cc {len(reply_cc_list)}"
                )
            else:
                self.logger.info(f"Reply to {original_from_email}")

            # Build the complete reply body manually
            # 1. User's message at top.
            #    v4.0: when body_format=='markdown' the body has already been
            #    converted to trusted HTML by compose_body() upstream (which
            #    can produce block-level elements <h1>/<ul>/etc). Wrapping
            #    that inside <p class="MsoNormal">...</p> like the v3.4
            #    plain-text path produces invalid HTML and Outlook drops the
            #    MsoNormal styling on promoted blocks. So for markdown/text
            #    we emit the converted HTML directly under WordSection1
            #    (which still contains it for Exclaimer's signature-anchor
            #    needs); for the legacy html path we keep the v3.4 wrap.
            #
            #    Also: for markdown/text we skip the second `format_body_for_html
            #    -> sanitize_html` pass because compose_body's output is
            #    already trusted markup and the regex-based sanitiser can
            #    mangle code fences and shell snippets the LLM legitimately
            #    produced (adversarial review #9).
            if body_format != "html":
                user_message_html = body  # already HTML from compose_body, trusted
                user_block = (
                    f'<div style="font-size:11pt;font-family:Calibri,sans-serif;">'
                    f'{user_message_html}</div>'
                )
            else:
                user_message_html = format_body_for_html(body)
                user_block = (
                    f'<p class="MsoNormal" style="font-size:11pt;font-family:Calibri,sans-serif;">'
                    f'{user_message_html}</p>'
                )

            # 2. Format the reply headers from original email metadata. These
            #    fields originate in an inbound email (attacker-controlled) and
            #    MUST be HTML-escaped before interpolation.
            header = format_forward_header(original_message)
            safe_from = escape_html(header.get('from', ''))
            safe_to = escape_html(header.get('to', ''))
            safe_cc = escape_html(header.get('cc', ''))
            safe_sent = escape_html(header.get('sent', ''))
            safe_subject = escape_html(header.get('subject', ''))

            # 3. Get the original email body HTML (preserves styles but strips document structure)
            original_body_html = sanitize_html(extract_body_html(original_message))
            self.logger.info(f"Extracted original body: {len(original_body_html)} characters")

            # Clean original body - rename WordSection1 to OriginalSection
            # This prevents Exclaimer from placing signature after the original content
            original_body_html = clean_original_body_for_signature(original_body_html)

            # 4. Construct complete body matching Outlook's exact structure
            # - WordSection1: user's new content (Exclaimer injects signature at end of this div)
            # - border-top div: Outlook-style separator (NOT <hr>)
            # - Headers inline in separator div
            # - original body (with OriginalSection class to avoid Exclaimer confusion)
            complete_body = f'''<div class="WordSection1">
{user_block}
</div>
<div style="border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0in 0in 0in">
<p class="MsoNormal" style="font-size:11pt;font-family:Calibri,sans-serif;"><b>From:</b> {safe_from}<br/>
<b>Sent:</b> {safe_sent}<br/>
<b>To:</b> {safe_to}<br/>'''
            if safe_cc:
                complete_body += f'''<b>Cc:</b> {safe_cc}<br/>'''
            complete_body += f'''<b>Subject:</b> {safe_subject}</p>
</div>
{original_body_html}'''

            self.logger.info(f"Constructed complete reply body: {len(complete_body)} characters")

            # Create a new Message with the complete body
            message = Message(
                account=account,
                author=Mailbox(email_address=account.primary_smtp_address),
                outbound_tracking_id=tracking_id,
                subject=reply_subject,
                body=HTMLBody(complete_body),
                to_recipients=[Mailbox(email_address=e) for e in reply_to_list]
            )
            if reply_cc_list:
                message.cc_recipients = [Mailbox(email_address=e) for e in reply_cc_list]
            if reply_bcc_list:
                message.bcc_recipients = [Mailbox(email_address=e) for e in reply_bcc_list]

            # Set threading headers so reply stays in the same conversation
            original_internet_msg_id = safe_get(original_message, "message_id", None)
            original_references = safe_get(original_message, "references", None)
            if original_internet_msg_id:
                message.in_reply_to = original_internet_msg_id
                if original_references:
                    message.references = f"{original_references} {original_internet_msg_id}"
                else:
                    message.references = original_internet_msg_id
                self.logger.info(f"Set threading headers: in_reply_to={original_internet_msg_id}")

            # Copy original inline attachments (signatures, embedded images)
            inline_count, _ = copy_attachments_to_message(original_message, message)
            if inline_count > 0:
                self.logger.info(f"Copied {inline_count} inline attachment(s) from original message")

            # Add new attachments if provided
            new_attachment_count = 0
            if attachments:
                for file_path in attachments:
                    try:
                        # Use os.path.basename for cross-platform path handling
                        file_name = os.path.basename(file_path)
                        with open(file_path, 'rb') as f:
                            content = f.read()
                            attachment = FileAttachment(
                                name=file_name,
                                content=content
                            )
                            message.attach(attachment)
                            new_attachment_count += 1
                            self.logger.info(f"Attached file: {file_name} ({len(content)} bytes)")
                    except FileNotFoundError:
                        raise ToolExecutionError(f"Attachment file not found on the Email MCP server: {file_path}. {REMOTE_AGENT_POINTER}", hint=REMOTE_AGENT_HINT)
                    except PermissionError:
                        raise ToolExecutionError(f"Permission denied reading attachment: {file_path}")
                    except Exception as e:
                        raise ToolExecutionError(f"Failed to attach file {file_path}: {e}")

            # Add inline (base64) attachments if provided
            inline_b64_count = attach_inline_files(message, kwargs.get("inline_attachments", []))
            if inline_b64_count > 0:
                new_attachment_count += inline_b64_count
                self.logger.info(f"Added {inline_b64_count} inline (base64) attachment(s)")
            new_attachment_count += attach_outgoing_files(message, outgoing_files)

            if dry_run:
                # Everything above only read the original; nothing is sent
                # and no draft is saved.
                return format_success_response(
                    "Dry run - no reply sent",
                    dry_run=True,
                    sent=False,
                    message_checked=True,
                    would_send_to=reply_to_list,
                    would_cc=reply_cc_list,
                    would_bcc=reply_bcc_list,
                    subject=reply_subject,
                    original_subject=original_subject,
                    reply_all=reply_all,
                    attachments_count=new_attachment_count,
                    would_attach=new_attachment_count + (0 if fetch_files else len(file_entries)),
                    inline_attachments_preserved=inline_count,
                    mailbox=mailbox,
                    **dry_run_file_report(file_entries, outgoing_files if fetch_files else None),
                )

            # Send the message
            message.send()
            self.logger.info(f"Reply sent to {original_from_email} from mailbox: {mailbox}")
            return format_success_response(
                "Reply accepted by mail server; final delivery not yet confirmed",
                tracking_id=tracking_id,
                accepted_by_mail_server=True,
                delivery_confirmed=False,
                original_subject=original_subject,
                reply_to=reply_to_list,
                cc=reply_cc_list,
                bcc=reply_bcc_list,
                reply_all=reply_all,
                attachments_count=new_attachment_count,
                inline_attachments_preserved=inline_count,
                mailbox=mailbox
            )

        except (ValidationError, ToolExecutionError):
            raise
        except Exception as e:
            self.logger.error(f"Failed to send reply: {e}")
            raise ToolExecutionError(f"Failed to send reply: {e}")


class ForwardEmailTool(BaseTool):
    """Tool for forwarding emails to new recipients while preserving original content."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "forward_email",
            "description": (
                "Forward an email with its original content and attachments, "
                "optionally adding files from a URL, base64 content, server-local path, "
                "or SmarterMail personal file storage."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The message ID of the email to forward"
                    },
                    "to": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of recipient email addresses"
                    },
                    "body": {
                        "type": "string",
                        "description": "Optional message to add before the forwarded content"
                    },
                    "cc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "CC recipients (optional)"
                    },
                    "bcc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "BCC recipients (optional)"
                    },
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Exchange-only server-local file paths; use files on any supported provider."
                    },
                    "files": FILES_SCHEMA,
                    **INLINE_ATTACHMENTS_SCHEMA,
                    **BODY_FORMAT_SCHEMA,
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to forward from (requires impersonation/delegate access)"
                    },
                    "dry_run": dry_run_schema("sending the forward"),
                    "verify_files": VERIFY_FILES_SCHEMA,
                },
                "required": ["message_id", "to"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Forward an email via EWS."""
        message_id = kwargs.get("message_id")
        to_recipients = kwargs.get("to", [])
        body = kwargs.get("body", "").strip() if kwargs.get("body") else ""
        body_format = kwargs.get("body_format", "html")
        cc_recipients = kwargs.get("cc", [])
        bcc_recipients = kwargs.get("bcc", [])
        additional_attachments = kwargs.get("attachments", [])
        target_mailbox = kwargs.get("target_mailbox")
        dry_run = bool(kwargs.get("dry_run", False))
        fetch_files = not dry_run or bool(kwargs.get("verify_files"))

        if not message_id:
            raise ToolExecutionError("message_id is required")
        if not to_recipients:
            raise ToolExecutionError("to recipients are required")

        # v4.0: convert markdown/text body to HTML before the existing forward
        # pipeline runs. Signature/inline-attachment preservation downstream
        # is unaffected — only the prelude body gets converted.
        if body and body_format != "html":
            body, _actual_fmt = compose_body(body, body_format)

        try:
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            tracking_id = new_outbound_tracking_id()
            mailbox = self.get_mailbox_info(target_mailbox)
            file_entries = validate_outgoing_files(kwargs.get("files"))
            outgoing_files = await load_outgoing_files(
                file_entries,
                storage_client=lambda: smartermail_client(self.ews_client, target_mailbox),
            ) if fetch_files else []

            # Find the original message across folders
            original_message = find_message_for_account(account, message_id)

            # Get original message details
            original_subject = safe_get(original_message, "subject", "") or ""

            # IMPORTANT: DO NOT use create_forward() - it auto-appends content we can't control
            # This causes duplication and wrong order issues with Exclaimer signature placement.
            # Instead, always create a fresh Message with manually constructed body.
            self.logger.info("Creating fresh Message for forward (not using create_forward)")

            # Get the forward subject (avoid duplicate FW: prefix)
            forward_subject = add_forward_prefix(original_subject)

            # Build the complete forward body manually
            # 1. User's message at top, rendered safely (plain text -> escaped +
            #    <br/>, existing HTML -> lightly sanitised). See utils.format_body_for_html.
            user_message_html = format_body_for_html(body)

            # 2. Format the forward headers from original email metadata; these
            #    are attacker-controlled and must be HTML-escaped.
            header = format_forward_header(original_message)
            safe_from = escape_html(header.get('from', ''))
            safe_to = escape_html(header.get('to', ''))
            safe_cc = escape_html(header.get('cc', ''))
            safe_sent = escape_html(header.get('sent', ''))
            safe_subject = escape_html(header.get('subject', ''))

            # 3. Get the original email body HTML (preserves styles but strips document structure)
            original_body_html = sanitize_html(extract_body_html(original_message))
            self.logger.info(f"Extracted original body: {len(original_body_html)} characters")

            # Clean original body - rename WordSection1 to OriginalSection
            # This prevents Exclaimer from placing signature after the original content
            original_body_html = clean_original_body_for_signature(original_body_html)

            # 4. Build headers block (Outlook format)
            headers_html = f'''<p style="font-size:11pt;font-family:Calibri,sans-serif;">
<b>From:</b> {safe_from}<br/>
<b>Date:</b> {safe_sent}<br/>
<b>Subject:</b> {safe_subject}<br/>'''
            if safe_to:
                headers_html += f'''<b>To:</b> {safe_to}<br/>'''
            if safe_cc:
                headers_html += f'''<b>Cc:</b> {safe_cc}<br/>'''
            headers_html += '''</p>'''

            # 5. Construct complete body matching Outlook's exact structure
            # - WordSection1: user's new content (Exclaimer injects signature at end of this div)
            # - border-top div: Outlook-style separator (NOT <hr>)
            # - divRplyFwdMsg: headers block
            # - original body (with OriginalSection class to avoid Exclaimer confusion)
            complete_body = f'''<div class="WordSection1">
<p class="MsoNormal" style="font-size:11pt;font-family:Calibri,sans-serif;">{user_message_html}</p>
</div>
<div style="border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0in 0in 0in">
<p class="MsoNormal" style="font-size:11pt;font-family:Calibri,sans-serif;"><b>From:</b> {safe_from}<br/>
<b>Sent:</b> {safe_sent}<br/>
<b>To:</b> {safe_to}<br/>'''
            if safe_cc:
                complete_body += f'''<b>Cc:</b> {safe_cc}<br/>'''
            complete_body += f'''<b>Subject:</b> {safe_subject}</p>
</div>
{original_body_html}'''

            self.logger.info(f"Constructed complete forward body: {len(complete_body)} characters")

            # Create a new Message with the complete body
            message = Message(
                account=account,
                author=Mailbox(email_address=account.primary_smtp_address),
                outbound_tracking_id=tracking_id,
                subject=forward_subject,
                body=HTMLBody(complete_body),
                to_recipients=[Mailbox(email_address=email) for email in to_recipients]
            )

            # Set threading headers so forward stays in the same conversation
            original_internet_msg_id = safe_get(original_message, "message_id", None)
            original_references = safe_get(original_message, "references", None)
            if original_internet_msg_id:
                message.in_reply_to = original_internet_msg_id
                if original_references:
                    message.references = f"{original_references} {original_internet_msg_id}"
                else:
                    message.references = original_internet_msg_id
                self.logger.info(f"Set threading headers: in_reply_to={original_internet_msg_id}")

            if cc_recipients:
                message.cc_recipients = [Mailbox(email_address=email) for email in cc_recipients]
            if bcc_recipients:
                message.bcc_recipients = [Mailbox(email_address=email) for email in bcc_recipients]

            # Copy original attachments
            inline_count, regular_count = copy_attachments_to_message(original_message, message)
            total_original_attachments = inline_count + regular_count
            self.logger.info(f"Copied {total_original_attachments} attachment(s) from original "
                           f"({inline_count} inline, {regular_count} regular)")

            # Add additional attachments if provided
            additional_attachment_count = 0
            if additional_attachments:
                for file_path in additional_attachments:
                    try:
                        # Use os.path.basename for cross-platform path handling
                        file_name = os.path.basename(file_path)
                        with open(file_path, 'rb') as f:
                            content = f.read()
                            attachment = FileAttachment(
                                name=file_name,
                                content=content
                            )
                            message.attach(attachment)
                            additional_attachment_count += 1
                            self.logger.info(f"Attached additional file: {file_name} ({len(content)} bytes)")
                    except FileNotFoundError:
                        raise ToolExecutionError(f"Attachment file not found on the Email MCP server: {file_path}. {REMOTE_AGENT_POINTER}", hint=REMOTE_AGENT_HINT)
                    except PermissionError:
                        raise ToolExecutionError(f"Permission denied reading attachment: {file_path}")
                    except Exception as e:
                        raise ToolExecutionError(f"Failed to attach file {file_path}: {e}")

            # Add inline (base64) attachments if provided
            inline_b64_count = attach_inline_files(message, kwargs.get("inline_attachments", []))
            if inline_b64_count > 0:
                additional_attachment_count += inline_b64_count
                self.logger.info(f"Added {inline_b64_count} inline (base64) attachment(s)")
            additional_attachment_count += attach_outgoing_files(message, outgoing_files)

            if dry_run:
                return format_success_response(
                    "Dry run - no forward sent",
                    dry_run=True,
                    sent=False,
                    message_checked=True,
                    would_send_to=to_recipients,
                    would_cc=cc_recipients or [],
                    would_bcc=bcc_recipients or [],
                    subject=forward_subject,
                    original_subject=original_subject,
                    attachments_included=total_original_attachments,
                    attachments_count=additional_attachment_count,
                    would_attach=additional_attachment_count + (0 if fetch_files else len(file_entries)),
                    mailbox=mailbox,
                    **dry_run_file_report(file_entries, outgoing_files if fetch_files else None),
                )

            # Send the message
            message.send()
            self.logger.info(f"Email forwarded to {', '.join(to_recipients)} from mailbox: {mailbox}")

            return format_success_response(
                "Forward accepted by mail server; final delivery not yet confirmed",
                tracking_id=tracking_id,
                accepted_by_mail_server=True,
                delivery_confirmed=False,
                original_subject=original_subject,
                forwarded_to=to_recipients,
                cc=cc_recipients if cc_recipients else None,
                bcc=bcc_recipients if bcc_recipients else None,
                attachments_included=total_original_attachments,
                inline_attachments_preserved=inline_count,
                additional_attachments=additional_attachment_count,
                mailbox=mailbox
            )

        except (ValidationError, ToolExecutionError):
            raise
        except Exception as e:
            self.logger.error(f"Failed to forward email: {e}")
            raise ToolExecutionError(f"Failed to forward email: {e}")
