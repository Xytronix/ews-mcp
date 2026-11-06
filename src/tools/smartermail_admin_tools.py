"""SmarterMail domain-administration tools (gated).

Every class sets ``requires_domain_admin = True`` so the operator must opt in
with ``SMARTERMAIL_ADMIN_TOOLS=true``. Payloads were read off the webmail
bundle and live-verified against a hosted SmarterMail server. System-admin
endpoints (``settings/sysadmin/*``, ``licensing/*``) are not exposed: these
tools need a domain administrator, not a system administrator.

WebhookAction is not in the live domain event catalog (possibleActions are
EmailAction and AlertAction). action_type=webhook raises ValidationError
naming the catalog; do not POST a synthetic webhook (server: "Not allowed").

show-password is gated by domainSettings.enableShowPasswords (currently
false). Domain-admin POST /settings/domain/domain does not send that field;
the write is sysadmin-only (POST /settings/sysadmin/domain-settings/{domain}/).
impersonate-user payload is {email, clientId: "WEBMAIL-<uuid>"}; this
domain-admin JWT has no CanImpersonate claim and the endpoint 403s.
"""


from __future__ import annotations

import asyncio
import base64
import uuid
from typing import Any, Dict, Iterable, List, Optional

import httpx

from .smartermail_base import SmarterMailTool
from ..exceptions import ToolExecutionError, ValidationError
from ..smartermail import SmarterMailClient, SmarterMailError
from ..utils import format_success_response

# account-search-quick / account-list-search take a string list, not a bitmask.
# The users grid posts searchFlags=["users"]; the aliases grid posts ["aliases"].
_SEARCH_FLAGS_USERS = ["users"]

# protocol-mailboxes-patch uses a per-page integer (var f=N in the bundle).
# connections-summary uses the section name ("WEBMAIL") or "*".
_PROTO_NUM = {
    "webmail": 1,
    "smtp": 2,
    "imap": 3,
    "pop": 4,
    "xmpp": 5,
    "eas": 9,
    "activesync": 9,
    "mapi_ews": 13,
    "mapi": 13,
    "ews": 13,
}
_PROTO_NAME = {
    "webmail": "WEBMAIL",
    "smtp": "SMTP",
    "imap": "IMAP",
    "pop": "POP",
    "xmpp": "XMPP",
    "eas": "ACTIVESYNC",
    "activesync": "ACTIVESYNC",
    "mapi_ews": "MAPI_EWS",
    "mapi": "MAPI_EWS",
    "ews": "MAPI_EWS",
    "webdav": "WEBDAV",
    "all": "*",
    "*": "*",
}

_EVENT_INPUT = {
    "from_address": ("fromaddress", "@EventInput_FromAddress"),
    "to_address": ("toaddress", "@EventInput_ToAddress"),
    "subject": ("subject", "@EventInput_Subject"),
    "body": ("body", "@EventInput_Body"),
    "webhook_url": ("webhookurl", "@EventInput_WebhookUrl"),
    "webhook_method": ("webhookmethod", "@EventInput_WebhookMethod"),
    "webhook_headers": ("webhookheaders", "@EventInput_WebhookHeaders"),
    "webhook_payload": ("webhookpayload", "@EventInput_WebhookPayload"),
    "browser_notify": ("browsernotify", "@EventInput_BrowserNotify"),
}

_SENSITIVE_ACCESS = frozenset({"show_password", "impersonate", "reset_app_password"})


def _action(kwargs: Dict[str, Any], allowed: Iterable[str]) -> str:
    action = (kwargs.get("action") or "").strip().lower().replace("-", "_")
    allowed_set = set(allowed)
    if action not in allowed_set:
        raise ValidationError(
            f"action must be one of {', '.join(sorted(allowed_set))}; got {action!r}"
        )
    return action


def _as_list(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if x not in (None, "")]
    return [part.strip() for part in str(value).replace(";", ",").split(",") if part.strip()]


def _domain(client: SmarterMailClient, kwargs: Dict[str, Any]) -> str:
    raw = (kwargs.get("domain") or "").strip()
    if raw:
        return raw
    email = client.email or ""
    return email.split("@")[-1] if "@" in email else email


def _local(email_or_user: str) -> str:
    text = (email_or_user or "").strip()
    return text.split("@")[0] if "@" in text else text


def _email_of(client: SmarterMailClient, kwargs: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        raw = kwargs.get(key)
        if raw not in (None, ""):
            text = str(raw).strip()
            if "@" in text:
                return text
            return f"{text}@{_domain(client, kwargs)}"
    raise ValidationError(f"{' or '.join(keys)} required")

def _search_params(
    client: SmarterMailClient,
    kwargs: Dict[str, Any],
    *,
    default_sort: str = "userName",
    default_flags: Optional[List[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    search = kwargs.get("search")
    if search is None:
        search = ""
    if default_flags is None:
        default_flags = list(_SEARCH_FLAGS_USERS)
    params: Dict[str, Any] = {
        "domain": _domain(client, kwargs),
        "skip": int(kwargs.get("skip") or 0),
        "take": int(kwargs.get("take") or 50),
        "search": search,
        "sortField": kwargs.get("sort_field") or default_sort,
        "sortDescending": bool(kwargs.get("sort_descending") or False),
    }
    flags = kwargs.get("search_flags")
    if flags in (None, ""):
        params["searchFlags"] = list(default_flags)
    elif isinstance(flags, list):
        params["searchFlags"] = flags
    elif isinstance(flags, str):
        params["searchFlags"] = [p.strip() for p in flags.split(",") if p.strip()]
    else:
        params["searchFlags"] = list(default_flags)
    if extra:
        params.update(extra)
    return params


def _ids(kwargs: Dict[str, Any], *keys: str) -> List[Any]:
    for key in keys:
        if kwargs.get(key) not in (None, "", [], {}):
            value = kwargs[key]
            if isinstance(value, list):
                return list(value)
            return [value]
    return []


def _client_id() -> str:
    # webmail: localStorage.clientId = "WEBMAIL-" + uuid v4 (authStorage.getClientId).
    return "WEBMAIL-" + str(uuid.uuid4())



def _public(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {"result": data}
    drop = {"accessToken", "refreshToken", "success"}
    out = {k: v for k, v in data.items() if k not in drop}
    if "message" in out:
        out["api_message"] = out.pop("message")
    return out


def _proto_num(value: Any) -> int:
    if value in (None, ""):
        raise ValidationError("protocol required")
    if isinstance(value, int):
        return value
    key = str(value).strip().lower().replace("-", "_")
    if key.isdigit():
        return int(key)
    if key in _PROTO_NUM:
        return _PROTO_NUM[key]
    raise ValidationError(
        f"unknown protocol {value!r}; use one of {', '.join(sorted(_PROTO_NUM))}"
    )


def _proto_name(value: Any) -> str:
    if value in (None, "", "*", "all"):
        return "*"
    if isinstance(value, int):
        for name, num in _PROTO_NUM.items():
            if num == value:
                return _PROTO_NAME.get(name, str(value))
        return str(value)
    key = str(value).strip()
    mapped = _PROTO_NAME.get(key.lower().replace("-", "_"))
    return mapped or key.upper()


class _AdminTool(SmarterMailTool):
    """Blocking HTTP hop off the event loop; every subclass is domain-admin gated."""

    requires_domain_admin = True

    def _dispatch(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    async def execute(self, **kwargs: Any) -> Dict[str, Any]:
        return await asyncio.to_thread(self._run, kwargs)

    def _run(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        client = self.client(kwargs.get("target_mailbox"))
        return self._dispatch(client, kwargs)

    def _call_timeout(
        self,
        client: SmarterMailClient,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 90.0,
    ) -> Dict[str, Any]:
        """list-users-extra timed out at the default 30s; override per request."""
        if not path.startswith("/"):
            path = "/" + path
        headers = {
            "Authorization": f"Bearer {client._token()}",
            "Accept": "application/json",
        }
        try:
            resp = client._client.request(
                method.upper(),
                path,
                json=json,
                params=params,
                headers=headers,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise ToolExecutionError(f"SmarterMail {path} failed: {exc}") from exc
        try:
            return SmarterMailClient._payload(resp, path)
        except SmarterMailError as exc:
            raise ToolExecutionError(str(exc)) from exc

    def _download(self, client: SmarterMailClient, method: str, path: str, **kwargs: Any) -> bytes:
        try:
            return client.download(method, path, **kwargs)
        except SmarterMailError as exc:
            raise ToolExecutionError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class ManageDomainUsersTool(_AdminTool):
    """List / create / update / delete / rename domain users."""

    requires_domain_admin = True

    _ACTIONS = (
        "list",
        "search",
        "counts",
        "get",
        "create",
        "update",
        "delete",
        "rename",
        "statuses",
        "disable",
        "enable",
        "recalculate",
        "reindex",
        "list_extra",
    )

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_domain_users",
            "Domain user CRUD, search, status, disable/enable, recalculate and "
            "reindex. Default list uses account-search-quick (fast). list_extra "
            "hits list-users-extra and can take >30s. Rename leaves a leftover "
            "folder named after the old username; delete both if you rename back.",
            {
                "action": {
                    "type": "string",
                    "enum": list(self._ACTIONS),
                    "description": (
                        "list/search/counts/get/create/update/delete/rename/"
                        "statuses/disable/enable/recalculate/reindex/list_extra."
                    ),
                },
                "email": {"type": "string", "description": "user@domain (get/update/delete/rename/disable)."},
                "username": {"type": "string", "description": "Local part, without @domain."},
                "new_username": {"type": "string", "description": "Local part for rename."},
                "password": {"type": "string", "description": "Required on create; optional on update."},
                "full_name": {"type": "string"},
                "is_domain_admin": {"type": "boolean"},
                "is_disabled": {"type": "boolean"},
                "max_mailbox_size": {"type": "integer", "description": "Mailbox cap in MB."},
                "search": {"type": "string", "description": "Filter text for list/search/statuses."},
                "skip": {"type": "integer", "description": "Pagination offset. Default 0."},
                "take": {"type": "integer", "description": "Page size. Default 50."},
                "sort_field": {"type": "string", "description": "Default userName."},
                "sort_descending": {"type": "boolean"},
                "search_flags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "account-search-quick flags. Default [\"users\"].",
                },
                "emails": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Bulk delete/disable/enable/reindex/recalculate.",
                },
                "allow_incoming": {
                    "type": "boolean",
                    "description": "On disable: keep receiving mail. Default false.",
                },
                "domain": {"type": "string"},
            },
            required=["action"],
        )

    def _dispatch(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs, self._ACTIONS)
        return getattr(self, f"_{action}")(client, kwargs)

    def _list(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        params = _search_params(client, kwargs)
        data = self.call(client, "POST", "/api/v1/settings/domain/account-search-quick", json=params)
        users = data.get("users") or []
        return format_success_response(
            f"{len(users)} user(s)",
            users=users,
            total=data.get("totalCount"),
        )

    def _search(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        params = _search_params(client, kwargs)
        data = self.call(client, "POST", "/api/v1/settings/domain/account-list-search", json=params)
        results = data.get("results") or []
        return format_success_response(
            f"{len(results)} account(s)",
            results=results,
            total=data.get("totalCount"),
        )

    def _counts(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        data = self.call(client, "GET", "/api/v1/settings/domain/account-list-counts")
        return format_success_response("account counts", **_public(data))

    def _list_extra(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        data = self._call_timeout(
            client,
            "GET",
            "/api/v1/settings/domain/list-users-extra",
            timeout=90.0,
        )
        users = data.get("userData") or data.get("users") or data.get("results") or []
        count = len(users) if not isinstance(users, dict) else len(users)
        return format_success_response(
            f"{count} user(s) (list-users-extra)",
            users=users,
            total=data.get("totalCount") or count,
        )

    def _get(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        email = _email_of(client, kwargs, "email", "username")
        data = self.call(client, "POST", "/api/v1/settings/domain/get-user", json={"email": email})
        return format_success_response(f"user {email}", **_public(data))

    def _create(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.require(kwargs, "password")
        domain = _domain(client, kwargs)
        username = _local(str(kwargs.get("username") or kwargs.get("email") or ""))
        if not username:
            raise ValidationError("username or email required")
        email = f"{username}@{domain}"
        payload = {
            "userData": {
                "userName": username,
                "password": kwargs["password"],
                "adUsername": kwargs.get("ad_username") or "",
                "securityFlags": {
                    "authType": 0,
                    "authProviderId": kwargs.get("auth_provider_id") or "",
                    "isDomainAdmin": bool(kwargs.get("is_domain_admin") or False),
                },
                "isPasswordExpired": bool(kwargs.get("must_change_password") or False),
            }
        }
        data = self.call(client, "POST", "/api/v1/settings/domain/user-put", json=payload)
        if kwargs.get("full_name") not in (None, ""):
            try:
                self.call(
                    client,
                    "POST",
                    "/api/v1/settings/domain/post-user",
                    json={
                        "email": email,
                        "userData": {"fullName": kwargs["full_name"]},
                        "clientId": _client_id(),
                    },
                )
            except ToolExecutionError:
                pass
        return format_success_response(f"created {email}", **{**_public(data), "email": email})

    def _update(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        email = _email_of(client, kwargs, "email", "username")
        patch: Dict[str, Any] = {}
        if kwargs.get("full_name") not in (None, ""):
            patch["fullName"] = kwargs["full_name"]
        if kwargs.get("password"):
            patch["password"] = kwargs["password"]
        flags: Dict[str, Any] = {}
        if kwargs.get("is_disabled") is not None:
            flags["isDisabled"] = bool(kwargs["is_disabled"])
        if kwargs.get("is_domain_admin") is not None:
            flags["isDomainAdmin"] = bool(kwargs["is_domain_admin"])
        if flags:
            patch["securityFlags"] = flags
        if kwargs.get("max_mailbox_size") not in (None, ""):
            patch["maxMailboxSize"] = int(kwargs["max_mailbox_size"])
        if not patch:
            raise ValidationError("nothing to update")
        data = self.call(
            client,
            "POST",
            "/api/v1/settings/domain/post-user",
            json={"email": email, "userData": patch, "clientId": _client_id()},
        )
        return format_success_response(f"updated {email}", **_public(data))

    def _delete(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        emails = _as_list(kwargs.get("emails"))
        if kwargs.get("email") or kwargs.get("username"):
            emails.append(_email_of(client, kwargs, "email", "username"))
        if not emails:
            raise ValidationError("email or emails required")
        data = self.call(
            client,
            "POST",
            "/api/v1/settings/domain/users-delete",
            json={"input": emails},
        )
        return format_success_response(f"deleted {len(emails)} user(s)", **_public(data))

    def _rename(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.require(kwargs, "new_username")
        old = _local(str(kwargs.get("username") or kwargs.get("email") or ""))
        new = _local(str(kwargs["new_username"]))
        if not old:
            raise ValidationError("username or email required")
        data = self.call(
            client,
            "POST",
            f"/api/v1/settings/domain/rename-user/{old}/{new}",
            json={"clientId": _client_id()},
        )
        return format_success_response(f"renamed {old} -> {new}", **_public(data))

    def _statuses(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        params = _search_params(client, kwargs, default_sort="userName")
        data = self.call(client, "POST", "/api/v1/settings/domain/users-statuses", json=params)
        users = data.get("usersStatuses") or []
        return format_success_response(
            f"{len(users)} status row(s)",
            users=users,
            total=data.get("totalCount"),
        )

    def _disable(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        emails = self._bulk_emails(client, kwargs)
        allow = bool(kwargs.get("allow_incoming") or False)
        data = self.call(
            client,
            "POST",
            f"/api/v1/settings/domain/users-disable/true/{str(allow).lower()}",
            json={"input": emails},
        )
        return format_success_response(f"disabled {len(emails)} user(s)", **_public(data))

    def _enable(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        emails = self._bulk_emails(client, kwargs)
        data = self.call(
            client,
            "POST",
            "/api/v1/settings/domain/users-disable/false",
            json={"input": emails},
        )
        return format_success_response(f"enabled {len(emails)} user(s)", **_public(data))

    def _recalculate(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        users = [_local(e) for e in self._bulk_emails(client, kwargs)]
        data = self.call(
            client,
            "POST",
            "/api/v1/settings/domain/recalculate-users",
            json={"input": users},
        )
        return format_success_response(f"recalculated {len(users)} user(s)", **_public(data))

    def _reindex(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        users = [_local(e) for e in self._bulk_emails(client, kwargs)]
        data = self.call(
            client,
            "POST",
            "/api/v1/settings/domain/reindex-users",
            json={"input": users},
        )
        return format_success_response(f"reindex queued for {len(users)} user(s)", **_public(data))

    def _bulk_emails(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> List[str]:
        emails = _as_list(kwargs.get("emails"))
        if kwargs.get("email") or kwargs.get("username"):
            emails.append(_email_of(client, kwargs, "email", "username"))
        if not emails:
            raise ValidationError("email or emails required")
        return emails


# ---------------------------------------------------------------------------
# Access / sessions
# ---------------------------------------------------------------------------


class ManageUserAccessTool(_AdminTool):
    """Protocol access, connections, sessions, app passwords, impersonation."""

    requires_domain_admin = True

    _ACTIONS = (
        "protocol_access",
        "protocol_mailboxes",
        "connections_counts",
        "connections_summary",
        "drop_connections",
        "kill_sessions",
        "show_password",
        "app_passwords",
        "reset_app_password",
        "impersonate",
    )

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_user_access",
            "Protocol access, live connections, session kill, app passwords, "
            "show-password and impersonate. show_password and impersonate are "
            "explicit actions only — they never run as a side effect of list. "
            "show_password needs domainSettings.enableShowPasswords (sysadmin "
            "write; not flipped here). impersonate needs CanImpersonate (this "
            "domain admin 403s; payload is {email, clientId: WEBMAIL-<uuid>}).",
            {
                "action": {
                    "type": "string",
                    "enum": list(self._ACTIONS),
                    "description": (
                        "protocol_access, protocol_mailboxes, connections_counts, "
                        "connections_summary, drop_connections, kill_sessions, "
                        "show_password, app_passwords, reset_app_password, impersonate."
                    ),
                },
                "email": {"type": "string"},
                "emails": {"type": "array", "items": {"type": "string"}},
                "protocol": {
                    "type": "string",
                    "description": (
                        "webmail, smtp, imap, pop, xmpp, eas, mapi_ews, webdav, all. "
                        "Or the integer the webmail uses for protocol-mailboxes-patch."
                    ),
                },
                "enabled": {
                    "type": "boolean",
                    "description": "protocol_mailboxes: activate (true) or deactivate.",
                },
                "webmail_enabled": {"type": "boolean"},
                "smtp_enabled": {"type": "boolean"},
                "imap_enabled": {"type": "boolean"},
                "pop_enabled": {"type": "boolean"},
                "xmpp_enabled": {"type": "boolean"},
                "eas_enabled": {"type": "boolean"},
                "mapi_ews_enabled": {"type": "boolean"},
                "webdav_enabled": {"type": "boolean"},
                "sessions": {
                    "type": "array",
                    "description": "Session objects from connections_summary for kill_sessions.",
                },
                "app_password_id": {
                    "type": "string",
                    "description": "App-password guid for reset_app_password.",
                },
                "search": {"type": "string"},
                "skip": {"type": "integer"},
                "take": {"type": "integer"},
                "sort_field": {"type": "string"},
                "sort_descending": {"type": "boolean"},
                "domain": {"type": "string"},
            },
            required=["action"],
        )

    def _dispatch(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs, self._ACTIONS)
        return getattr(self, f"_{action}")(client, kwargs)

    def _emails(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> List[str]:
        emails = _as_list(kwargs.get("emails"))
        if kwargs.get("email"):
            emails.append(_email_of(client, kwargs, "email"))
        if not emails:
            raise ValidationError("email or emails required")
        return emails

    def _protocol_access(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        items = self._emails(client, kwargs)
        payload = {
            "items": items,
            "webmailEnabled": bool(kwargs.get("webmail_enabled") or False),
            "smtpEnabled": bool(kwargs.get("smtp_enabled") or False),
            "imapEnabled": bool(kwargs.get("imap_enabled") or False),
            "popEnabled": bool(kwargs.get("pop_enabled") or False),
            "xmppEnabled": bool(kwargs.get("xmpp_enabled") or False),
            "easEnabled": bool(kwargs.get("eas_enabled") or False),
            "mapiEwsEnabled": bool(kwargs.get("mapi_ews_enabled") or False),
            "webdavEnabled": bool(kwargs.get("webdav_enabled") or False),
        }
        data = self.call(client, "POST", "/api/v1/settings/domain/modify-protocol-access", json=payload)
        return format_success_response("protocol access updated", **_public(data))

    def _protocol_mailboxes(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        emails = self._emails(client, kwargs)
        proto = _proto_num(kwargs.get("protocol"))
        active = True if kwargs.get("enabled") is None else bool(kwargs["enabled"])
        data = self.call(
            client,
            "POST",
            "/api/v1/settings/domain/protocol-mailboxes-patch",
            json={"type": proto, "isActive": active, "emailAddresses": emails},
        )
        return format_success_response(
            f"protocol {proto} {'enabled' if active else 'disabled'} for {len(emails)} mailbox(es)",
            **_public(data),
        )

    def _connections_counts(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        data = self.call(
            client,
            "POST",
            "/api/v1/settings/domain/users-connections-counts",
            json={"domain": _domain(client, kwargs)},
        )
        return format_success_response("connection counts", **_public(data))

    def _connections_summary(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        params = _search_params(client, kwargs, default_sort="emailAddress")
        params["type"] = _proto_name(kwargs.get("protocol") or "*")
        data = self.call(
            client, "POST", "/api/v1/settings/domain/users-connections-summary", json=params
        )
        rows = data.get("data") or []
        return format_success_response(
            f"{len(rows)} connection row(s)",
            connections=rows,
            total=data.get("totalCount"),
        )

    def _drop_connections(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        emails = self._emails(client, kwargs)
        proto = kwargs.get("protocol")
        type_val: Any = -1 if proto in (None, "", "all", "*") else _proto_num(proto)
        data = self.call(
            client,
            "POST",
            "/api/v1/settings/domain/drop-user-connections",
            json={"type": type_val, "dropInfo": emails},
        )
        return format_success_response(f"dropped connections for {len(emails)} user(s)", **_public(data))

    def _kill_sessions(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        sessions = kwargs.get("sessions")
        if not sessions:
            # webmail sends the checked session objects; emails alone are accepted
            # as a fallback (server returns success, may no-op if no session match).
            sessions = self._emails(client, kwargs)
        proto = kwargs.get("protocol")
        type_val: Any = -1 if proto in (None, "", "all", "*") else _proto_num(proto)
        data = self.call(
            client,
            "POST",
            "/api/v1/settings/domain/kill-user-sessions",
            json={"type": type_val, "sessions": sessions},
        )
        return format_success_response("kill-user-sessions submitted", **_public(data))

    def _show_password(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        email = _email_of(client, kwargs, "email")
        try:
            data = self.call(
                client,
                "POST",
                "/api/v1/settings/domain/show-password/",
                json={"token": client._token(), "emailAddress": email},
            )
        except ToolExecutionError as exc:
            raise ToolExecutionError(self._show_password_denied(client, str(exc))) from exc
        return format_success_response(
            f"password for {email}",
            email=email,
            password=data.get("password"),
            app_passwords=data.get("appPasswords") or [],
        )

    def _show_password_denied(self, client: SmarterMailClient, original: str) -> str:
        flag = None
        try:
            dom = self.call(client, "GET", "/api/v1/settings/domain/domain")
            flag = (dom.get("domainSettings") or {}).get("enableShowPasswords")
        except ToolExecutionError:
            pass
        return (
            f"{original}. domainSettings.enableShowPasswords={flag}. "
            "Webmail enables show-password for a domain admin only when that "
            "flag is true. The write path is sysadmin-only: POST "
            "/api/v1/settings/sysadmin/domain-settings/{domain}/ with "
            "domainSettings.enableShowPasswords. Domain-admin POST "
            "/api/v1/settings/domain/domain does not include the field. "
            "Not flipped."
        )

    def _app_passwords(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        email = _email_of(client, kwargs, "email")
        data = self.call(
            client,
            "POST",
            "/api/v1/settings/domain/get-available-app-passwords",
            json={"token": client._token(), "emailAddress": email},
        )
        return format_success_response(f"app passwords for {email}", **_public(data))

    def _reset_app_password(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.require(kwargs, "app_password_id")
        email = _email_of(client, kwargs, "email")
        data = self.call(
            client,
            "POST",
            "/api/v1/settings/domain/reset-app-password",
            json={
                "id": kwargs["app_password_id"],
                "email": email,
                "token": client._token(),
            },
        )
        return format_success_response("app password reset", **_public(data))

    def _impersonate(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        email = _email_of(client, kwargs, "email")
        payload = {"email": email, "clientId": _client_id()}
        try:
            data = self.call(
                client,
                "POST",
                "/api/v1/settings/domain/impersonate-user",
                json=payload,
            )
        except ToolExecutionError as exc:
            raise ToolExecutionError(
                f"{exc}. Webmail posts {{email, clientId: 'WEBMAIL-<uuid>'}} "
                "(authStorage.getClientId). This domain-admin JWT has role "
                "DomainAdmin and no CanImpersonate claim; authenticate-user "
                "returns no canImpersonate field. The only impersonate path in "
                "the bundle is POST /api/v1/settings/domain/impersonate-user. "
                "Sysadmin CanImpersonate is required on this host."
            ) from exc
        # _public drops accessToken/refreshToken; impersonate success is those
        # tokens (webmail copies impersonateAccessToken into localStorage.impUser).
        out = _public(data) if isinstance(data, dict) else {"result": data}
        if isinstance(data, dict):
            for src, dest in (
                ("impersonateAccessToken", "impersonate_access_token"),
                ("impersonateRefreshToken", "impersonate_refresh_token"),
                ("accessToken", "impersonate_access_token"),
                ("refreshToken", "impersonate_refresh_token"),
                ("impersonateAccessTokenExpiration", "impersonate_access_token_expiration"),
                ("impersonateRefreshTokenExpiration", "impersonate_refresh_token_expiration"),
            ):
                if data.get(src) and dest not in out:
                    out[dest] = data[src]
        return format_success_response(f"impersonate {email}", **out)


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------


class ManageDomainAliasesTool(_AdminTool):
    """Mailbox aliases and domain aliases (domain-alias needs DNS)."""

    requires_domain_admin = True

    _ACTIONS = (
        "list",
        "get",
        "create",
        "update",
        "delete",
        "available",
        "list_domain_aliases",
        "create_domain_alias",
        "verify_domain_alias",
        "delete_domain_alias",
    )

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_domain_aliases",
            "Mailbox aliases (alias/alias-put/aliases-delete) and domain aliases "
            "(domain-alias-put / verify / delete). Domain-alias create needs a "
            "DNS MX and is not a throwaway.",
            {
                "action": {
                    "type": "string",
                    "enum": list(self._ACTIONS),
                },
                "name": {"type": "string", "description": "Alias local-part or domain-alias hostname."},
                "old_name": {"type": "string", "description": "Current name when renaming via update."},
                "display_name": {"type": "string"},
                "targets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "aliasTargetList — destination addresses.",
                },
                "names": {"type": "array", "items": {"type": "string"}, "description": "Bulk delete."},
                "allow_sending": {"type": "boolean"},
                "show_in_gal": {"type": "boolean"},
                "internal_only": {"type": "boolean"},
                "include_all_domain_users": {"type": "boolean"},
                "is_catch_all": {"type": "boolean"},
                "verify_mx": {
                    "type": "boolean",
                    "description": "domain-alias-put path flag. Default true.",
                },
                "domain": {"type": "string"},
            },
            required=["action"],
        )

    def _dispatch(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs, self._ACTIONS)
        return getattr(self, f"_{action}")(client, kwargs)

    def _alias_body(self, kwargs: Dict[str, Any], *, name: str) -> Dict[str, Any]:
        show_gal = True if kwargs.get("show_in_gal") is None else bool(kwargs["show_in_gal"])
        alias: Dict[str, Any] = {
            "name": name,
            "displayName": kwargs.get("display_name") or name,
            "aliasTargetList": _as_list(kwargs.get("targets")),
            "allowSending": bool(kwargs.get("allow_sending") or False),
            "hideFromGAL": not show_gal,
            "enableForXmpp": bool(kwargs.get("show_in_xmpp") or False),
            "internalOnly": bool(kwargs.get("internal_only") or False),
            "includeAllDomainUsers": bool(kwargs.get("include_all_domain_users") or False),
            "isCatchAll": bool(kwargs.get("is_catch_all") or False),
            "description": kwargs.get("description") or "",
        }
        return {"alias": alias}

    def _list(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        data = self.call(client, "GET", "/api/v1/settings/domain/aliases")
        grid = data.get("gridInfo") or []
        return format_success_response(f"{len(grid)} alias(es)", aliases=grid)

    def _get(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.require(kwargs, "name")
        data = self.call(
            client, "POST", "/api/v1/settings/domain/alias-get", json={"name": kwargs["name"]}
        )
        return format_success_response(f"alias {kwargs['name']}", **_public(data))

    def _create(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.require(kwargs, "name")
        body = self._alias_body(kwargs, name=str(kwargs["name"]))
        if not body["alias"]["aliasTargetList"]:
            raise ValidationError("targets required")
        data = self.call(client, "POST", "/api/v1/settings/domain/alias-put/", json=body)
        return format_success_response(f"created alias {kwargs['name']}", **_public(data))

    def _update(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.require(kwargs, "name")
        body = self._alias_body(kwargs, name=str(kwargs["name"]))
        body["oldName"] = kwargs.get("old_name") or kwargs["name"]
        data = self.call(client, "POST", "/api/v1/settings/domain/alias/", json=body)
        return format_success_response(f"updated alias {kwargs['name']}", **_public(data))

    def _delete(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        names = _as_list(kwargs.get("names"))
        if kwargs.get("name"):
            names.append(str(kwargs["name"]))
        if not names:
            raise ValidationError("name or names required")
        data = self.call(
            client, "POST", "/api/v1/settings/domain/aliases-delete", json={"input": names}
        )
        return format_success_response(f"deleted {len(names)} alias(es)", **_public(data))

    def _available(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.require(kwargs, "name")
        try:
            data = self.call(
                client,
                "GET",
                f"/api/v1/settings/domain/email-alias-does-not-exists/{kwargs['name']}",
            )
        except ToolExecutionError as exc:
            text = str(exc)
            if "already exists" in text.lower() or "HTTP 400" in text:
                return format_success_response(
                    f"{kwargs['name']} is taken", available=False, name=kwargs["name"]
                )
            raise
        return format_success_response(
            f"{kwargs['name']} is available",
            available=True,
            name=kwargs["name"],
            **_public(data),
        )

    def _list_domain_aliases(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        data = self.call(client, "GET", "/api/v1/settings/domain/domain-aliases")
        return format_success_response("domain aliases", **_public(data))

    def _create_domain_alias(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.require(kwargs, "name")
        verify = True if kwargs.get("verify_mx") is None else bool(kwargs["verify_mx"])
        data = self.call(
            client,
            "POST",
            f"/api/v1/settings/domain/domain-alias-put/{kwargs['name']}/{str(verify).lower()}/",
        )
        return format_success_response(f"created domain alias {kwargs['name']}", **_public(data))

    def _verify_domain_alias(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.require(kwargs, "name")
        data = self.call(
            client,
            "POST",
            f"/api/v1/settings/domain/domain-alias-verify/{kwargs['name']}/",
        )
        return format_success_response(f"verified domain alias {kwargs['name']}", **_public(data))

    def _delete_domain_alias(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        names = _as_list(kwargs.get("names"))
        if kwargs.get("name"):
            names.append(str(kwargs["name"]))
        if not names:
            raise ValidationError("name or names required")
        data = self.call(
            client,
            "POST",
            "/api/v1/settings/domain/domain-aliases-delete",
            json={"input": names},
        )
        return format_success_response(f"deleted {len(names)} domain alias(es)", **_public(data))


# ---------------------------------------------------------------------------
# Mailing lists
# ---------------------------------------------------------------------------


class ManageMailingListsTool(_AdminTool):
    """Mailing lists, subscribers, bounce reset, system messages."""

    requires_domain_admin = True

    _ACTIONS = (
        "list",
        "get",
        "add",
        "update",
        "delete",
        "delete_bulk",
        "subscribers",
        "subscriber_add",
        "subscriber_remove",
        "subscriber_details",
        "subscriber_clear_bounce",
        "field_definitions",
        "system_messages",
        "update_system_message",
        "reset_system_message",
    )

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_mailing_lists",
            "Mailing-list CRUD, subscribers, bounce-count reset, and the "
            "per-list system-message templates.",
            {
                "action": {"type": "string", "enum": list(self._ACTIONS)},
                "list_id": {"type": "integer", "description": "Numeric mailingListId."},
                "list_address": {"type": "string", "description": "e.g. zz-probe (local part or full)."},
                "moderator_address": {"type": "string"},
                "description": {"type": "string"},
                "settings": {"type": "object", "description": "Full list settings object for update."},
                "list_ids": {"type": "array", "items": {"type": "integer"}},
                "subscriber_email": {"type": "string"},
                "subscriber_emails": {"type": "array", "items": {"type": "string"}},
                "search": {"type": "string"},
                "skip": {"type": "integer"},
                "take": {"type": "integer"},
                "sort_field": {"type": "string"},
                "sort_descending": {"type": "boolean"},
                "subscriber_type": {"type": "integer"},
                "message_name": {"type": "string", "description": "System-message key (SUBSCRIBE, etc.)."},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "message_names": {"type": "array", "items": {"type": "string"}},
                "domain": {"type": "string"},
            },
            required=["action"],
        )

    def _dispatch(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs, self._ACTIONS)
        return getattr(self, f"_{action}")(client, kwargs)

    def _list_id(self, kwargs: Dict[str, Any]) -> int:
        lid = kwargs.get("list_id")
        if lid in (None, ""):
            raise ValidationError("list_id required")
        return int(lid)

    def _list(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        data = self.call(client, "GET", "/api/v1/settings/domain/mailing-lists/list")
        items = data.get("items") or []
        return format_success_response(f"{len(items)} mailing list(s)", lists=items)

    def _get(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        lid = self._list_id(kwargs)
        data = self.call(
            client, "GET", f"/api/v1/settings/domain/mailing-lists/{lid}/settings"
        )
        return format_success_response(f"mailing list {lid}", **_public(data))

    def _add(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.require(kwargs, "list_address")
        moderator = kwargs.get("moderator_address") or client.email
        payload: Dict[str, Any] = {
            "listAddress": _local(str(kwargs["list_address"])),
            "moderatorAddress": moderator,
        }
        if kwargs.get("description"):
            payload["description"] = kwargs["description"]
        data = self.call(client, "POST", "/api/v1/settings/domain/mailing-lists/add", json=payload)
        item = (data.get("item") or data) if isinstance(data, dict) else data
        pub = _public(data)
        pub["item"] = item
        return format_success_response("mailing list created", **pub)

    def _update(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        lid = self._list_id(kwargs)
        settings = kwargs.get("settings")
        if not isinstance(settings, dict) or not settings:
            raise ValidationError("settings object required")
        data = self.call(
            client,
            "POST",
            f"/api/v1/settings/domain/mailing-lists/{lid}/settings",
            json=settings,
        )
        return format_success_response(f"updated mailing list {lid}", **_public(data))

    def _delete(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        addr = kwargs.get("list_address")
        if not addr:
            raise ValidationError("list_address required")
        data = self.call(
            client, "POST", "/api/v1/settings/domain/mailing-lists/delete", json=str(addr)
        )
        return format_success_response(f"deleted mailing list {addr}", **_public(data))

    def _delete_bulk(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        ids = [int(x) for x in _ids(kwargs, "list_ids", "list_id")]
        if not ids:
            raise ValidationError("list_id or list_ids required")
        data = self.call(
            client,
            "POST",
            "/api/v1/settings/domain/mailing-lists/delete-bulk",
            json={"ints": ids},
        )
        return format_success_response(f"deleted {len(ids)} mailing list(s)", **_public(data))

    def _subscribers(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        lid = self._list_id(kwargs)
        payload = {
            "skip": int(kwargs.get("skip") or 0),
            "take": int(kwargs.get("take") or 50),
            "search": kwargs.get("search") or "",
            "subscriberType": int(kwargs.get("subscriber_type") or 0),
            "sortField": kwargs.get("sort_field") or "emailAddress",
            "sortDescending": bool(kwargs.get("sort_descending") or False),
        }
        data = self.call(
            client,
            "POST",
            f"/api/v1/settings/domain/mailing-lists/{lid}/subscriber-search",
            json=payload,
        )
        items = data.get("items") or []
        return format_success_response(
            f"{len(items)} subscriber(s)", subscribers=items, total=data.get("total")
        )

    def _subscriber_add(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        lid = self._list_id(kwargs)
        emails = _as_list(kwargs.get("subscriber_emails"))
        if kwargs.get("subscriber_email"):
            emails.append(str(kwargs["subscriber_email"]))
        if not emails:
            raise ValidationError("subscriber_email or subscriber_emails required")
        data = self.call(
            client,
            "POST",
            f"/api/v1/settings/domain/mailing-lists/{lid}/subscriber-add",
            json=emails,
        )
        return format_success_response(f"added {len(emails)} subscriber(s)", **_public(data))

    def _subscriber_remove(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        lid = self._list_id(kwargs)
        emails = _as_list(kwargs.get("subscriber_emails"))
        if kwargs.get("subscriber_email"):
            emails.append(str(kwargs["subscriber_email"]))
        if not emails:
            raise ValidationError("subscriber_email or subscriber_emails required")
        data = self.call(
            client,
            "POST",
            f"/api/v1/settings/domain/mailing-lists/{lid}/subscriber-remove",
            json=emails,
        )
        return format_success_response(f"removed {len(emails)} subscriber(s)", **_public(data))

    def _subscriber_details(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        lid = self._list_id(kwargs)
        self.require(kwargs, "subscriber_email")
        data = self.call(
            client,
            "GET",
            f"/api/v1/settings/domain/mailing-lists/subscriber-details/"
            f"{kwargs['subscriber_email']}/{lid}",
        )
        return format_success_response("subscriber details", **_public(data))

    def _subscriber_clear_bounce(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.require(kwargs, "subscriber_email")
        data = self.call(
            client,
            "POST",
            "/api/v1/settings/domain/mailing-lists/subscriber-clear-bounce-count",
            json={"input": kwargs["subscriber_email"]},
        )
        return format_success_response("bounce count cleared", **_public(data))

    def _field_definitions(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        data = self.call(client, "GET", "/api/v1/settings/domain/subscriber-field-definitions")
        fields = data.get("subscriberFields") or []
        return format_success_response(f"{len(fields)} field definition(s)", fields=fields)

    def _system_messages(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        lid = self._list_id(kwargs)
        data = self.call(
            client, "GET", f"/api/v1/settings/domain/mailing-lists/{lid}/system-messages"
        )
        msgs = data.get("systemMessages") or []
        return format_success_response(f"{len(msgs)} system message(s)", messages=msgs)

    def _update_system_message(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        lid = self._list_id(kwargs)
        self.require(kwargs, "message_name")
        payload = {
            "name": kwargs["message_name"],
            "subject": kwargs.get("subject") or "",
            "body": kwargs.get("body") or "",
        }
        data = self.call(
            client,
            "POST",
            f"/api/v1/settings/domain/mailing-lists/{lid}/system-message",
            json=payload,
        )
        return format_success_response(f"updated system message {payload['name']}", **_public(data))

    def _reset_system_message(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        lid = self._list_id(kwargs)
        names = _as_list(kwargs.get("message_names"))
        if kwargs.get("message_name"):
            names.append(str(kwargs["message_name"]))
        if not names:
            raise ValidationError("message_name or message_names required")
        data = self.call(
            client,
            "POST",
            f"/api/v1/settings/domain/mailing-lists/{lid}/system-messages/reset",
            json=names,
        )
        return format_success_response(f"reset {len(names)} system message(s)", **_public(data))


# ---------------------------------------------------------------------------
# User groups
# ---------------------------------------------------------------------------


class ManageUserGroupsTool(_AdminTool):
    """Custom domain user groups."""

    requires_domain_admin = True

    _ACTIONS = ("list", "create", "update", "delete")

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_user_groups",
            "List, create, update, or delete custom domain user groups.",
            {
                "action": {"type": "string", "enum": list(self._ACTIONS)},
                "group_id": {"type": "string", "description": "Group id (update/delete)."},
                "name": {"type": "string"},
                "usernames": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Member usernames or emails.",
                },
                "group_ids": {"type": "array", "items": {"type": "string"}},
                "domain": {"type": "string"},
            },
            required=["action"],
        )

    def _dispatch(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs, self._ACTIONS)
        return getattr(self, f"_{action}")(client, kwargs)

    def _list(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        data = self.call(client, "GET", "/api/v1/settings/domain/all-user-groups")
        coll = data.get("userGroupCollection") or {}
        groups = coll.get("customUserGroups") or []
        return format_success_response(f"{len(groups)} user group(s)", groups=groups)

    def _create(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.require(kwargs, "name")
        payload = {
            "userGroup": {
                "name": kwargs["name"],
                "userNames": _as_list(kwargs.get("usernames")),
                "type": 2,
            }
        }
        data = self.call(client, "POST", "/api/v1/settings/domain/user-group-put", json=payload)
        return format_success_response(f"created group {kwargs['name']}", **_public(data))

    def _update(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.require(kwargs, "group_id", "name")
        payload = {
            "userGroup": {
                "id": kwargs["group_id"],
                "name": kwargs["name"],
                "userNames": _as_list(kwargs.get("usernames")),
            }
        }
        data = self.call(client, "POST", "/api/v1/settings/domain/user-group", json=payload)
        return format_success_response(f"updated group {kwargs['name']}", **_public(data))

    def _delete(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        ids = [str(x) for x in _ids(kwargs, "group_ids", "group_id")]
        if not ids:
            raise ValidationError("group_id or group_ids required")
        data = self.call(
            client, "POST", "/api/v1/settings/domain/remove-user-groups", json={"IDs": ids}
        )
        return format_success_response(f"deleted {len(ids)} group(s)", **_public(data))


# ---------------------------------------------------------------------------
# Domain mail policy
# ---------------------------------------------------------------------------


class ManageDomainMailPolicyTool(_AdminTool):
    """Spam, content filters, whitelist, DKIM, signatures, defaults, propagate."""

    requires_domain_admin = True

    _SECTIONS = (
        "spam",
        "content_filters",
        "whitelist",
        "auto_clean",
        "password",
        "permissions",
        "user_defaults",
        "propagate",
        "signatures",
        "signature_mappings",
        "dkim",
    )

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_domain_mail_policy",
            "Domain mail policy: spam, content filters, whitelist, auto-clean, "
            "password rules, permissions, user-defaults, propagate-settings, "
            "domain signatures, signature mappings, DKIM. Mutating spam/DKIM/"
            "defaults/propagate is implemented from the bundle; do not fire it "
            "against production state unless you mean to.",
            {
                "section": {
                    "type": "string",
                    "enum": list(self._SECTIONS),
                    "description": "Which policy surface to touch.",
                },
                "op": {
                    "type": "string",
                    "description": (
                        "spam: get|set. content_filters: list|create|update|delete|order. "
                        "whitelist/auto_clean/password/user_defaults/signature_mappings: get|set. "
                        "permissions: get. propagate: run. signatures: list|create|update|delete. "
                        "dkim: get|set|enable|disable|verify."
                    ),
                },
                "title": {"type": "string", "description": "Content-filter title."},
                "filter": {"type": "object", "description": "Full content-filter object."},
                "filter_id": {"type": "string"},
                "direction": {
                    "type": "string",
                    "enum": ["higher", "lower"],
                    "description": "content_filters order.",
                },
                "match_type": {"type": "integer", "description": "0 = AND (webmail default)."},
                "domains": {"type": "array", "items": {"type": "string"}, "description": "Whitelist domains."},
                "emails": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Whitelist emails, or propagate target emails ('*@domain' = all).",
                },
                "all_users": {"type": "boolean", "description": "propagate to every mailbox."},
                "settings": {"type": "object", "description": "POST body for set/run (bundle shape)."},
                "name": {"type": "string", "description": "Signature name."},
                "text": {"type": "string", "description": "Signature HTML/text."},
                "signature_id": {"type": "integer"},
                "signature_guid": {"type": "string"},
                "is_default": {"type": "boolean"},
                "maps": {"type": "array", "description": "signatureMaps array for set."},
                "global_update": {
                    "type": "array",
                    "description": "propagate-settings globalUpdate entries ({userField, intValue, ...}).",
                },
                "domain": {"type": "string"},
            },
            required=["section"],
        )

    def _dispatch(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        section = (kwargs.get("section") or "").strip().lower().replace("-", "_")
        if section not in self._SECTIONS:
            raise ValidationError(
                f"section must be one of {', '.join(self._SECTIONS)}; got {section!r}"
            )
        op = (kwargs.get("op") or "get").strip().lower().replace("-", "_")
        handler = getattr(self, f"_{section}", None)
        if handler is None:
            raise ValidationError(f"unknown section: {section}")
        return handler(client, kwargs, op)

    def _spam(self, client: SmarterMailClient, kwargs: Dict[str, Any], op: str) -> Dict[str, Any]:
        if op in ("get", "list"):
            data = self.call(client, "GET", "/api/v1/settings/domain/spam-settings")
            return format_success_response("spam settings", **_public(data))
        if op == "set":
            settings = kwargs.get("settings")
            if not isinstance(settings, dict):
                raise ValidationError("settings object required")
            data = self.call(client, "POST", "/api/v1/settings/domain/spam-settings", json=settings)
            return format_success_response("spam settings saved", **_public(data))
        raise ValidationError("spam op must be get or set")

    def _content_filters(self, client: SmarterMailClient, kwargs: Dict[str, Any], op: str) -> Dict[str, Any]:
        if op in ("get", "list"):
            data = self.call(client, "GET", "/api/v1/settings/domain/content-filter-groups")
            groups = data.get("contentFilterGroups") or []
            return format_success_response(f"{len(groups)} content filter(s)", filters=groups)
        if op == "create":
            filt = kwargs.get("filter") if isinstance(kwargs.get("filter"), dict) else {}
            filt = dict(filt)
            filt.setdefault("title", kwargs.get("title"))
            if not filt.get("title"):
                raise ValidationError("title or filter.title required")
            filt.setdefault("actions", [])
            filt.setdefault("filters", [])
            filt.setdefault("internalType", 0)
            filt.setdefault("matchType", int(kwargs.get("match_type") or 0))
            data = self.call(
                client,
                "POST",
                "/api/v1/settings/domain/content-filter-groups",
                json={"toAdd": [filt]},
            )
            return format_success_response(f"created filter {filt['title']}", **_public(data))
        if op == "update":
            filt = kwargs.get("filter")
            if not isinstance(filt, dict):
                raise ValidationError("filter object required")
            data = self.call(
                client,
                "POST",
                "/api/v1/settings/domain/content-filter-groups",
                json={"contentFilterGroups": [filt]},
            )
            return format_success_response("updated content filter", **_public(data))
        if op == "delete":
            ids = _as_list(kwargs.get("filter_id"))
            extra = kwargs.get("filter")
            if isinstance(extra, dict) and extra.get("id") is not None:
                ids.append(str(extra["id"]))
            if not ids:
                raise ValidationError("filter_id required")
            data = self.call(
                client,
                "POST",
                "/api/v1/settings/domain/content-filter-groups",
                json={"toRemove": ids},
            )
            return format_success_response(f"deleted {len(ids)} content filter(s)", **_public(data))
        if op == "order":
            fid = kwargs.get("filter_id")
            if fid in (None, ""):
                raise ValidationError("filter_id required")
            higher = (kwargs.get("direction") or "higher").lower() != "lower"
            data = self.call(
                client,
                "POST",
                f"/api/v1/settings/domain/content-filter-groups/order/{fid}/{str(higher).lower()}",
            )
            return format_success_response("content-filter order updated", **_public(data))
        raise ValidationError("content_filters op must be list/create/update/delete/order")

    def _whitelist(self, client: SmarterMailClient, kwargs: Dict[str, Any], op: str) -> Dict[str, Any]:
        if op in ("get", "list"):
            data = self.call(client, "GET", "/api/v1/settings/domain/domain")
            settings = data.get("domainSettings") or data
            return format_success_response(
                "whitelist",
                domains=settings.get("whitelistDomains") or [],
                emails=settings.get("whitelistAddresses") or [],
            )
        if op == "set":
            payload = {
                "domains": _as_list(kwargs.get("domains")),
                "emails": _as_list(kwargs.get("emails")),
            }
            extra = kwargs.get("settings")
            if isinstance(extra, dict):
                payload.update(extra)
            data = self.call(client, "POST", "/api/v1/settings/domain/whitelist", json=payload)
            return format_success_response("whitelist saved", **_public(data))
        raise ValidationError("whitelist op must be get or set")

    def _auto_clean(self, client: SmarterMailClient, kwargs: Dict[str, Any], op: str) -> Dict[str, Any]:
        if op in ("get", "list"):
            data = self.call(client, "GET", "/api/v1/settings/auto-clean")
            return format_success_response("auto-clean", **_public(data))
        if op == "set":
            settings = kwargs.get("settings")
            if not isinstance(settings, dict):
                raise ValidationError(
                    "settings object required "
                    "({domainOverrideActive, allowUserOverride, domainRules})"
                )
            data = self.call(client, "POST", "/api/v1/settings/auto-clean-domain", json=settings)
            return format_success_response("auto-clean saved", **_public(data))
        raise ValidationError("auto_clean op must be get or set")

    def _password(self, client: SmarterMailClient, kwargs: Dict[str, Any], op: str) -> Dict[str, Any]:
        if op in ("get", "list"):
            data = self.call(client, "GET", "/api/v1/settings/domain/password-settings")
            return format_success_response("password settings", **_public(data))
        if op == "set":
            settings = kwargs.get("settings")
            if not isinstance(settings, dict):
                raise ValidationError("settings object required (domainOverrides body)")
            data = self.call(
                client, "POST", "/api/v1/settings/domain/password-settings", json=settings
            )
            return format_success_response("password settings saved", **_public(data))
        raise ValidationError("password op must be get or set")

    def _permissions(self, client: SmarterMailClient, kwargs: Dict[str, Any], op: str) -> Dict[str, Any]:
        if op not in ("get", "list"):
            raise ValidationError("permissions is read-only (GET)")
        data = self.call(client, "GET", "/api/v1/settings/domain/permissions")
        return format_success_response("domain permissions", **_public(data))

    def _user_defaults(self, client: SmarterMailClient, kwargs: Dict[str, Any], op: str) -> Dict[str, Any]:
        if op in ("get", "list"):
            data = self.call(client, "GET", "/api/v1/settings/domain/user-defaults")
            return format_success_response("user defaults", **_public(data))
        if op == "set":
            settings = kwargs.get("settings")
            if not isinstance(settings, dict):
                raise ValidationError("settings object required (user-defaults body)")
            data = self.call(
                client, "POST", "/api/v1/settings/domain/user-defaults", json=settings
            )
            return format_success_response("user defaults saved", **_public(data))
        raise ValidationError("user_defaults op must be get or set")

    def _propagate(self, client: SmarterMailClient, kwargs: Dict[str, Any], op: str) -> Dict[str, Any]:
        if op not in ("run", "set", "get"):
            raise ValidationError("propagate op must be run")
        updates = kwargs.get("global_update")
        if not isinstance(updates, list) or not updates:
            extra = kwargs.get("settings")
            if isinstance(extra, dict):
                updates = extra.get("globalUpdate")
        if not isinstance(updates, list) or not updates:
            raise ValidationError("global_update list required")
        domain = _domain(client, kwargs)
        if kwargs.get("all_users"):
            emails = [f"*@{domain}"]
        else:
            emails = _as_list(kwargs.get("emails"))
        if not emails:
            raise ValidationError("emails or all_users=true required")
        data = self.call(
            client,
            "POST",
            "/api/v1/settings/domain/propagate-settings",
            json={"globalUpdate": updates, "emails": emails},
        )
        return format_success_response("propagation started", **_public(data))

    def _signatures(self, client: SmarterMailClient, kwargs: Dict[str, Any], op: str) -> Dict[str, Any]:
        if op in ("get", "list"):
            data = self.call(client, "GET", "/api/v1/settings/domain/signatures")
            sigs = data.get("signatureConfigs") or []
            return format_success_response(f"{len(sigs)} domain signature(s)", signatures=sigs)
        if op == "create":
            self.require(kwargs, "name")
            cfg: Dict[str, Any] = {
                "id": "",
                "name": kwargs["name"],
                "text": kwargs.get("text") or "",
                "isDefault": bool(kwargs.get("is_default") or False),
            }
            data = self.call(
                client,
                "POST",
                "/api/v1/settings/domain/signature-put",
                json={"signatureConfig": cfg},
            )
            return format_success_response(f"created signature {cfg['name']}", **_public(data))
        if op == "update":
            cfg = {
                "id": kwargs.get("signature_id"),
                "guid": kwargs.get("signature_guid"),
                "name": kwargs.get("name"),
                "text": kwargs.get("text"),
                "isDefault": kwargs.get("is_default"),
            }
            cfg = {k: v for k, v in cfg.items() if v is not None}
            if "id" not in cfg and "guid" not in cfg:
                raise ValidationError("signature_id or signature_guid required")
            data = self.call(
                client,
                "POST",
                "/api/v1/settings/domain/signature",
                json={"signatureConfig": cfg},
            )
            return format_success_response("updated signature", **_public(data))
        if op == "delete":
            sid = kwargs.get("signature_id")
            if sid in (None, ""):
                raise ValidationError("signature_id required")
            data = self.call(
                client, "POST", f"/api/v1/settings/domain/signature-delete/{int(sid)}"
            )
            return format_success_response(f"deleted signature {sid}", **_public(data))
        raise ValidationError("signatures op must be list/create/update/delete")

    def _signature_mappings(self, client: SmarterMailClient, kwargs: Dict[str, Any], op: str) -> Dict[str, Any]:
        if op in ("get", "list"):
            data = self.call(client, "GET", "/api/v1/settings/domain/signature-mappings")
            return format_success_response("signature mappings", maps=data.get("maps") or [])
        if op == "set":
            maps = kwargs.get("maps")
            if not isinstance(maps, list):
                raise ValidationError("maps array required")
            data = self.call(
                client,
                "POST",
                "/api/v1/settings/domain/signature-mappings",
                json={"signatureMaps": maps},
            )
            return format_success_response("signature mappings saved", **_public(data))
        raise ValidationError("signature_mappings op must be get or set")

    def _dkim(self, client: SmarterMailClient, kwargs: Dict[str, Any], op: str) -> Dict[str, Any]:
        if op in ("get", "list"):
            data = self.call(client, "GET", "/api/v1/settings/domain/domain")
            settings = data.get("domainSettings") or data
            return format_success_response(
                "dkim settings",
                dkim=settings.get("domainKeysSettings") or settings.get("dkimSettings") or {},
            )
        if op == "verify":
            data = self.call(client, "POST", "/api/v1/settings/domain/dkim-verify-active", json={})
            return format_success_response("dkim verify", **_public(data))
        if op == "enable":
            data = self.call(client, "POST", "/api/v1/settings/domain/dkim-enable")
            return format_success_response("dkim enable submitted", **_public(data))
        if op == "disable":
            data = self.call(client, "POST", "/api/v1/settings/domain/dkim-disable")
            return format_success_response("dkim disable submitted", **_public(data))
        if op == "set":
            settings = kwargs.get("settings")
            if not isinstance(settings, dict):
                raise ValidationError("settings object required (dkim-settings body)")
            data = self.call(client, "POST", "/api/v1/settings/domain/dkim-settings", json=settings)
            return format_success_response("dkim settings saved", **_public(data))
        raise ValidationError("dkim op must be get/set/enable/disable/verify")


# ---------------------------------------------------------------------------
# Event hooks
# ---------------------------------------------------------------------------


class ManageEventHooksTool(_AdminTool):
    """Domain (and user) event hooks — EmailAction / AlertAction / WebhookAction."""

    requires_domain_admin = True

    _ACTIONS = ("events", "variables", "list", "get", "create", "update", "delete")

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_event_hooks",
            "SmarterMail event hooks (push instead of polling). Domain catalog: "
            "GET /api/v1/settings/domain/domain-events. List: POST "
            "event-hooks-by-owner {startIndex,count,name,sortDescending}. "
            "Create/update POSTs the hook object (not {eventHook:...}); server "
            "returns a GUID id. Verified create body: {owner, name, eventID, "
            "groupID, enabled, isNew:true, conditions:[], conditionsByKey:{}, "
            "actions:[{id:'', action, key, inputs:[...]}], actionsByKey:{}}. "
            "Delete: POST event-hook-delete {input:[guid]}. Domain catalog "
            "possibleActions are EmailAction and AlertAction; action_type="
            "webhook raises ValidationError unless the catalog lists it. "
            "scope=user uses /api/v1/settings/event-hook and POST user-events.",
            {
                "action": {"type": "string", "enum": list(self._ACTIONS)},
                "scope": {
                    "type": "string",
                    "enum": ["domain", "user"],
                    "description": "Default domain.",
                },
                "hook_id": {"type": "string", "description": "Event hook GUID from create/list."},
                "name": {"type": "string"},
                "event_id": {
                    "type": "integer",
                    "description": "Numeric eventID (UserAdded is 20000 on this server).",
                },
                "event_name": {
                    "type": "string",
                    "description": "Catalog id such as UserAdded, UserLoginSuccessful.",
                },
                "action_type": {
                    "type": "string",
                    "enum": ["email", "alert", "webhook"],
                    "description": "Which possibleAction to attach. Default email.",
                },
                "from_address": {"type": "string"},
                "to_address": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "browser_notify": {"type": "boolean", "description": "AlertAction browser popup."},
                "webhook_url": {"type": "string"},
                "webhook_method": {"type": "string", "description": "POST/GET/PUT. Default POST."},
                "webhook_headers": {"type": "string"},
                "webhook_payload": {"type": "string"},
                "enabled": {"type": "boolean"},
                "conditions": {
                    "type": "array",
                    "description": "[{key, value1, value2, condition}] merged onto catalog templates.",
                },
                "hook": {"type": "object", "description": "Full eventHook object (update)."},
                "domain": {"type": "string"},
            },
            required=["action"],
        )

    def _dispatch(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs, self._ACTIONS)
        return getattr(self, f"_{action}")(client, kwargs)

    def _scope(self, kwargs: Dict[str, Any]) -> str:
        scope = (kwargs.get("scope") or "domain").strip().lower()
        if scope not in ("domain", "user"):
            raise ValidationError("scope must be domain or user")
        return scope

    def _prefix(self, scope: str) -> str:
        return "/api/v1/settings/domain" if scope == "domain" else "/api/v1/settings"

    def _events(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        scope = self._scope(kwargs)
        if scope == "domain":
            data = self.call(client, "GET", "/api/v1/settings/domain/domain-events")
        else:
            data = self.call(client, "POST", "/api/v1/settings/user-events", json={})
        events = data.get("events") or {}
        summary = []
        items = events.values() if isinstance(events, dict) else events
        for ev in items:
            if not isinstance(ev, dict):
                continue
            actions = [a.get("key") for a in (ev.get("possibleActions") or [])]
            summary.append(
                {
                    "eventId": ev.get("eventId") or ev.get("id"),
                    "id": ev.get("id"),
                    "category": ev.get("categoryResourceId"),
                    "possibleActions": actions,
                }
            )
        return format_success_response(
            f"{len(summary)} event type(s)", events=summary, catalog=events
        )

    def _variables(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        scope = self._scope(kwargs)
        path = (
            "/api/v1/settings/domain/event-variables"
            if scope == "domain"
            else "/api/v1/settings/event-variables"
        )
        data = self.call(client, "GET", path)
        vars_ = data.get("variables") or data
        return format_success_response("event variables", variables=vars_)

    def _list(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        scope = self._scope(kwargs)
        path = f"{self._prefix(scope)}/event-hooks-by-owner"
        data = self.call(
            client,
            "POST",
            path,
            json={
                "startIndex": int(kwargs.get("skip") or 0),
                "count": int(kwargs.get("take") or 100),
                "name": kwargs.get("search") or "",
                "sortDescending": bool(kwargs.get("sort_descending") or False),
            },
        )
        hooks = data.get("eventHooks") or []
        return format_success_response(
            f"{len(hooks)} event hook(s)", hooks=hooks, total=data.get("total")
        )

    def _get(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        hid = kwargs.get("hook_id")
        if hid in (None, ""):
            raise ValidationError("hook_id required")
        listed = self._list(client, kwargs)
        want = str(hid)
        for hook in listed.get("hooks") or []:
            if str(hook.get("id")) == want:
                return format_success_response(f"event hook {hid}", hook=hook)
        raise ToolExecutionError(f"event hook {hid} not found")

    def _catalog_events(self, client: SmarterMailClient, scope: str) -> Dict[str, Any]:
        if scope == "domain":
            data = self.call(client, "GET", "/api/v1/settings/domain/domain-events")
        else:
            data = self.call(client, "POST", "/api/v1/settings/user-events", json={})
        events = data.get("events") or {}
        if isinstance(events, list):
            out = {}
            for ev in events:
                key = str(ev.get("eventId") or ev.get("id"))
                out[key] = ev
            return out
        return events

    def _find_event(self, events: Dict[str, Any], kwargs: Dict[str, Any]) -> Dict[str, Any]:
        event_id = kwargs.get("event_id")
        event_name = (kwargs.get("event_name") or "").lstrip("@")
        if event_id not in (None, ""):
            ev = events.get(str(event_id))
            if isinstance(ev, dict):
                return ev
            for item in events.values():
                if not isinstance(item, dict):
                    continue
                if item.get("eventId") == int(event_id) or str(item.get("eventId")) == str(event_id):
                    return item
        if event_name:
            for item in events.values():
                if not isinstance(item, dict):
                    continue
                ident = (item.get("id") or "").lstrip("@")
                resid = (item.get("eventResourceId") or "").lstrip("@")
                if ident == event_name or resid == event_name:
                    return item
        raise ValidationError("event_id or event_name not found in the event catalog")

    def _fill_inputs(self, action: Dict[str, Any], kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = dict(action)
        inputs = [dict(i) for i in (action.get("inputs") or [])]
        mapping = {
            field: kwargs[field]
            for field in _EVENT_INPUT
            if kwargs.get(field) not in (None, "")
        }
        if "browser_notify" in mapping:
            val = mapping["browser_notify"]
            mapping["browser_notify"] = (
                "True" if val in (True, "true", "True", 1, "1") else "False"
            )
        if kwargs.get("webhook_method") in (None, "") and action.get("key") == "WebhookAction":
            mapping.setdefault("webhook_method", "POST")
        for inp in inputs:
            key = (inp.get("key") or "").lower()
            rid = inp.get("descriptionResourceId") or ""
            for field, (want_key, want_rid) in _EVENT_INPUT.items():
                if field not in mapping:
                    continue
                if key == want_key or rid == want_rid:
                    inp["value"] = mapping[field]
        action["inputs"] = inputs
        action["action"] = action.get("action") or action.get("key")
        if not action.get("id"):
            action["id"] = ""
        return action

    def _pick_action(self, event: Dict[str, Any], kwargs: Dict[str, Any]) -> Dict[str, Any]:
        want = (kwargs.get("action_type") or "email").strip().lower()
        key = {
            "email": "EmailAction",
            "alert": "AlertAction",
            "webhook": "WebhookAction",
        }.get(want)
        if not key:
            raise ValidationError("action_type must be email, alert, or webhook")
        for item in event.get("possibleActions") or []:
            if item.get("key") == key or item.get("action") == key:
                return self._fill_inputs(item, kwargs)
        raise ValidationError(
            f"{key} is not in possibleActions for this event "
            f"(available: {[a.get('key') for a in (event.get('possibleActions') or [])]}). "
            "WebhookAction is accepted when the catalog lists it; payload fields are "
            "webhookurl/webhookmethod/webhookheaders/webhookpayload."
        )

    def _build_hook(self, client: SmarterMailClient, kwargs: Dict[str, Any], *, is_new: bool) -> Dict[str, Any]:
        scope = self._scope(kwargs)
        events = self._catalog_events(client, scope)
        event = self._find_event(events, kwargs)
        action = self._pick_action(event, kwargs)
        domain = _domain(client, kwargs)
        owner = domain if scope == "domain" else (client.email or domain)
        name = kwargs.get("name")
        if not name:
            raise ValidationError("name required")
        eid = event.get("eventId")
        if eid in (None, ""):
            raise ValidationError("catalog event is missing eventId")
        hook: Dict[str, Any] = {
            "owner": owner,
            "name": name,
            "eventID": int(eid),
            "groupID": domain,
            "enabled": True if kwargs.get("enabled") is None else bool(kwargs["enabled"]),
            "isNew": is_new,
            "conditions": [],
            "conditionsByKey": {},
            "actions": [action],
            "actionsByKey": {},
        }
        if not is_new:
            hook["id"] = str(kwargs.get("hook_id") or "")
        conds = kwargs.get("conditions")
        if isinstance(conds, list) and conds:
            templates = {c.get("key"): c for c in (event.get("conditions") or []) if c.get("key")}
            built = []
            by_key = {}
            for cond in conds:
                if not isinstance(cond, dict) or not cond.get("key"):
                    continue
                base = dict(templates.get(cond["key"]) or {})
                base.update(cond)
                built.append(base)
                by_key[base["key"]] = base
            hook["conditions"] = built
            hook["conditionsByKey"] = by_key
        return {k: v for k, v in hook.items() if v is not None}

    def _create(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        scope = self._scope(kwargs)
        hook = kwargs.get("hook") if isinstance(kwargs.get("hook"), dict) else self._build_hook(
            client, kwargs, is_new=True
        )
        data = self.call(client, "POST", f"{self._prefix(scope)}/event-hook", json=hook)
        pub = _public(data)
        if data.get("id") is not None:
            pub["hook_id"] = data.get("id")
        return format_success_response(
            f"created event hook {hook.get('name')}",
            **pub,
        )

    def _update(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        scope = self._scope(kwargs)
        if isinstance(kwargs.get("hook"), dict):
            hook = dict(kwargs["hook"])
        else:
            if kwargs.get("hook_id") in (None, ""):
                raise ValidationError("hook_id or hook required")
            current = self._get(client, kwargs).get("hook") or {}
            hook = dict(current)
            if kwargs.get("name"):
                hook["name"] = kwargs["name"]
            if kwargs.get("enabled") is not None:
                hook["enabled"] = bool(kwargs["enabled"])
            if kwargs.get("action_type") or kwargs.get("to_address") or kwargs.get("webhook_url"):
                events = self._catalog_events(client, scope)
                event = self._find_event(
                    events,
                    {
                        "event_id": hook.get("eventID") or kwargs.get("event_id"),
                        "event_name": kwargs.get("event_name"),
                    },
                )
                hook["actions"] = [self._pick_action(event, kwargs)]
            hook["isNew"] = False
        data = self.call(client, "POST", f"{self._prefix(scope)}/event-hook", json=hook)
        return format_success_response("updated event hook", **_public(data))

    def _delete(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        scope = self._scope(kwargs)
        hid = kwargs.get("hook_id")
        if hid in (None, ""):
            raise ValidationError("hook_id required")
        data = self.call(
            client,
            "POST",
            f"{self._prefix(scope)}/event-hook-delete",
            json={"input": [str(hid)]},
        )
        return format_success_response(f"deleted event hook {hid}", **_public(data))


# ---------------------------------------------------------------------------
# Mailbox migration + archive search
# ---------------------------------------------------------------------------


class ManageMailboxMigrationTool(_AdminTool):
    """IMAP/Exchange mailbox migration and message-archive search/copy/download."""

    requires_domain_admin = True

    _ACTIONS = (
        "imap_status",
        "imap_size",
        "imap_test",
        "imap_start",
        "imap_account_put",
        "exchange_status",
        "exchange_size",
        "exchange_test",
        "exchange_start",
        "exchange_account_put",
        "kill",
        "archive_search",
        "archive_copy",
        "archive_download",
    )

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_mailbox_migration",
            "IMAP/Exchange mailbox migration (test / size / start / kill) and "
            "message-archive search/copy/download. imap_start / exchange_start / "
            "imap_account_put / exchange_account_put / archive_copy actually "
            "move mail — only fire them when you mean to.",
            {
                "action": {"type": "string", "enum": list(self._ACTIONS)},
                "server_address": {"type": "string"},
                "username": {"type": "string"},
                "password": {"type": "string"},
                "server_port": {"type": "integer", "description": "IMAP default 993, EWS 443."},
                "encryption": {
                    "type": "integer",
                    "description": "Passed through as the webmail encryption int (probe used 1=SSL).",
                },
                "download_to_folder": {"type": "string"},
                "delete_everything_before_import": {"type": "boolean"},
                "leave_mail_on_server": {"type": "boolean"},
                "bypass_ssl_errors": {"type": "boolean"},
                "account": {"type": "object", "description": "Full imapAccount/exchangeAccount override."},
                "migration_id": {"type": "string"},
                "search_query": {"type": "string", "description": "advanced-search searchQuery."},
                "section": {
                    "type": "integer",
                    "description": "advanced-search section (0=all, 1=email). Default 1.",
                },
                "search_criteria_map": {"type": "object"},
                "with_attachments_only": {"type": "boolean"},
                "search_guid": {"type": "string"},
                "message_ids": {"type": "array", "items": {"type": "string"}},
                "dest_email": {"type": "string"},
                "dest_folder": {"type": "string"},
                "domain": {"type": "string"},
            },
            required=["action"],
        )

    def _dispatch(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs, self._ACTIONS)
        return getattr(self, f"_{action}")(client, kwargs)

    def _imap_account(self, kwargs: Dict[str, Any], *, require_auth: bool) -> Dict[str, Any]:
        if isinstance(kwargs.get("account"), dict):
            return dict(kwargs["account"])
        if require_auth:
            self.require(kwargs, "server_address", "username", "password")
        account: Dict[str, Any] = {
            "serverAddress": kwargs.get("server_address"),
            "username": kwargs.get("username"),
            "password": kwargs.get("password"),
            "serverPort": int(kwargs.get("server_port") or 993),
            "encryption": int(kwargs.get("encryption") or 1),
            "deleteEverythingBeforeImport": bool(kwargs.get("delete_everything_before_import") or False),
            "downloadToFolder": kwargs.get("download_to_folder") or "Inbox",
            "enableSpamFilter": False,
            "isManualRetrieval": False,
            "folderTransferMethod": 0,
            "leaveMailOnServer": True if kwargs.get("leave_mail_on_server") is None else bool(kwargs["leave_mail_on_server"]),
            "useApop": False,
            "accountType": "IMAP",
            "isMailboxMigration": True,
            "bypassSSLErrors": bool(kwargs.get("bypass_ssl_errors") or False),
        }
        return {k: v for k, v in account.items() if v is not None}

    def _exchange_account(self, kwargs: Dict[str, Any], *, require_auth: bool) -> Dict[str, Any]:
        if isinstance(kwargs.get("account"), dict):
            return dict(kwargs["account"])
        if require_auth:
            self.require(kwargs, "server_address", "username", "password")
        account: Dict[str, Any] = {
            "serverAddress": kwargs.get("server_address"),
            "username": kwargs.get("username"),
            "password": kwargs.get("password"),
            "serverPort": int(kwargs.get("server_port") or 443),
            "encryption": int(kwargs.get("encryption") or 1),
            "deleteEverythingBeforeImport": bool(kwargs.get("delete_everything_before_import") or False),
            "downloadToFolder": kwargs.get("download_to_folder") or "Inbox",
            "accountType": "Exchange",
            "isMailboxMigration": True,
            "bypassSSLErrors": bool(kwargs.get("bypass_ssl_errors") or False),
        }
        return {k: v for k, v in account.items() if v is not None}

    def _imap_status(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        try:
            data = self.call(client, "GET", "/api/v1/settings/imap-migration")
        except ToolExecutionError:
            data = self.call(client, "POST", "/api/v1/settings/imap-migration", json={})
        return format_success_response("imap migration status", **_public(data))

    def _exchange_status(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        try:
            data = self.call(client, "GET", "/api/v1/settings/exchange-migration")
        except ToolExecutionError:
            data = self.call(client, "POST", "/api/v1/settings/exchange-migration", json={})
        return format_success_response("exchange migration status", **_public(data))

    def _imap_size(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        account = self._imap_account(kwargs, require_auth=True)
        data = self.call(
            client, "POST", "/api/v1/settings/imap-migration-size", json={"imapAccount": account}
        )
        return format_success_response("imap migration size", **_public(data))

    def _exchange_size(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        account = self._exchange_account(kwargs, require_auth=True)
        data = self.call(
            client,
            "POST",
            "/api/v1/settings/exchange-migration-size",
            json={"exchangeAccount": account},
        )
        return format_success_response("exchange migration size", **_public(data))

    def _imap_test(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        account = self._imap_account(kwargs, require_auth=True)
        data = self.call(
            client, "POST", "/api/v1/settings/imap-test-connection", json={"imapAccount": account}
        )
        return format_success_response("imap test-connection", **_public(data))

    def _exchange_test(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        account = self._exchange_account(kwargs, require_auth=True)
        data = self.call(
            client,
            "POST",
            "/api/v1/settings/exchange-test-connection",
            json={"exchangeAccount": account},
        )
        return format_success_response("exchange test-connection", **_public(data))

    def _imap_start(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        account = self._imap_account(kwargs, require_auth=True)
        data = self.call(
            client, "POST", "/api/v1/settings/imap-migration", json={"imapAccount": account}
        )
        return format_success_response("imap migration started", **_public(data))

    def _exchange_start(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        account = self._exchange_account(kwargs, require_auth=True)
        data = self.call(
            client, "POST", "/api/v1/settings/exchange-migration", json={"exchangeAccount": account}
        )
        return format_success_response("exchange migration started", **_public(data))

    def _imap_account_put(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        account = self._imap_account(kwargs, require_auth=True)
        account["isMailboxMigration"] = False
        data = self.call(
            client, "POST", "/api/v1/settings/imap-account-put", json={"imapAccount": account}
        )
        return format_success_response("imap retrieval account saved", **_public(data))

    def _exchange_account_put(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        account = self._exchange_account(kwargs, require_auth=True)
        account["isMailboxMigration"] = False
        data = self.call(
            client,
            "POST",
            "/api/v1/settings/exchange-account-put",
            json={"exchangeAccount": account},
        )
        return format_success_response("exchange retrieval account saved", **_public(data))

    def _kill(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.require(kwargs, "migration_id")
        data = self.call(
            client, "POST", f"/api/v1/settings/kill-migration/{kwargs['migration_id']}"
        )
        return format_success_response("kill-migration submitted", **_public(data))

    def _archive_search(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "searchCriteriaMap": kwargs.get("search_criteria_map") or {},
            "section": int(kwargs.get("section") if kwargs.get("section") is not None else 1),
            "searchQuery": kwargs.get("search_query") or "",
            "or": False,
            "withAttachmentsOnly": bool(kwargs.get("with_attachments_only") or False),
        }
        data = self.call(client, "POST", "/api/v1/settings/advanced-search", json=payload)
        result = data.get("advancedSearchResult") or data
        return format_success_response("archive search", **_public(result) if isinstance(result, dict) else {"result": result})

    def _archive_copy(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.require(kwargs, "search_guid", "dest_email", "dest_folder")
        ids = kwargs.get("message_ids") or []
        if not isinstance(ids, list) or not ids:
            raise ValidationError("message_ids required")
        data = self.call(
            client,
            "POST",
            "/api/v1/settings/domain/copy-message-archive",
            json={
                "searchGuid": kwargs["search_guid"],
                "messageIds": ids,
                "destEmail": kwargs["dest_email"],
                "destFolder": kwargs["dest_folder"],
            },
        )
        return format_success_response("archive messages copied", **_public(data))

    def _archive_download(self, client: SmarterMailClient, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.require(kwargs, "search_guid")
        ids = kwargs.get("message_ids") or []
        body = {
            "searchGuid": kwargs["search_guid"],
            "messageIds": ids if isinstance(ids, list) else [ids],
        }
        blob = self._download(
            client, "POST", "/api/v1/settings/domain/download-message-archive", json=body
        )
        result: Dict[str, Any] = {"size": len(blob)}
        if len(blob) <= 1_000_000:
            result["zip_base64"] = base64.b64encode(blob).decode("ascii")
        else:
            result["note"] = "zip larger than 1MB; not inlined"
        return format_success_response(
            f"archive zip ({len(blob)} bytes)",
            **result,
        )


SMARTERMAIL_TOOLS = [
    ManageDomainUsersTool,
    ManageUserAccessTool,
    ManageDomainAliasesTool,
    ManageMailingListsTool,
    ManageUserGroupsTool,
    ManageDomainMailPolicyTool,
    ManageEventHooksTool,
    ManageMailboxMigrationTool,
]
