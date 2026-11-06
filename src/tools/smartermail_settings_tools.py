"""SmarterMail per-mailbox settings tools.

Signatures, trusted/blocked senders, auto-clean, notes, disposable
addresses, the notification centre, auto-complete recipients, and
account-security reads (app passwords + from-address options). All of
these ride the private JSON API the webmail uses; EWS cannot express
them. Payloads were read off the webmail bundle and live-verified
against a hosted SmarterMail server.
"""

from __future__ import annotations

import asyncio
import html as html_lib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .smartermail_base import SmarterMailTool
from ..exceptions import ToolExecutionError, ValidationError
from ..utils import format_success_response

# Signature mapOption values used by the webmail UI.
_SIG_MAP = {
    "none": 0,
    "primary": 1,
    "signature": 2,
    "domain": 3,
}
_SIG_MAP_NAME = {v: k for k, v in _SIG_MAP.items()}


def _action(kwargs: Dict[str, Any]) -> str:
    action = (kwargs.get("action") or "").strip().lower().replace("-", "_")
    if not action:
        raise ValidationError("action is required")
    return action


def _as_list(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v not in (None, "")]
    return [str(value)]


def _as_html(text: Optional[str]) -> str:
    if not text:
        return "<div></div>"
    stripped = text.lstrip()
    if stripped.startswith("<"):
        return text
    return f"<div>{html_lib.escape(text)}</div>"


def _wl_values(items: Any) -> List[str]:
    """Flatten whitelistAddresses / whitelistDomains to email/domain strings."""
    out: List[str] = []
    for item in items or []:
        if isinstance(item, dict):
            val = item.get("value") or item.get("email") or ""
        else:
            val = item
        if val:
            out.append(str(val))
    return out


def _map_option(value: Any, default: Optional[int] = None) -> int:
    if value is None or value == "":
        if default is None:
            raise ValidationError("map_option required")
        return default
    if isinstance(value, int):
        return value
    key = str(value).strip().lower()
    if key.isdigit():
        return int(key)
    if key in _SIG_MAP:
        return _SIG_MAP[key]
    raise ValidationError(
        "map_option must be none, primary, signature, domain, or 0-3"
    )


class _SettingsTool(SmarterMailTool):
    """SmarterMailTool that runs the blocking HTTP hop off the event loop."""

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    async def execute(self, **kwargs) -> Dict[str, Any]:
        return await asyncio.to_thread(self._run, kwargs)

    def _run(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        client = self.client(kwargs.get("target_mailbox"))
        return self._dispatch(client, kwargs)


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------


class ManageSignaturesTool(_SettingsTool):
    """User signature CRUD plus per-from-address mapping."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_signatures",
            "Create, list, update, delete, or map SmarterMail user signatures "
            "(which from-address uses which signature). EWS has no signature API.",
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "get", "create", "update", "delete", "map"],
                    "description": (
                        "list/get signatures; create/update/delete one; "
                        "map a signature onto a from-address "
                        "(map_option: none, primary, signature, domain)."
                    ),
                },
                "signature_id": {
                    "type": "integer",
                    "description": "Numeric signature id (get/update/delete/map).",
                },
                "signature_guid": {
                    "type": "string",
                    "description": "Signature GUID (get/update/delete/map).",
                },
                "name": {
                    "type": "string",
                    "description": "Signature name (create/update, or get-by-name).",
                },
                "text": {
                    "type": "string",
                    "description": "Signature body. HTML is passed through; plain text is wrapped.",
                },
                "from_address": {
                    "type": "string",
                    "description": "From-address to map (map action). Default: the mailbox address.",
                },
                "map_option": {
                    "type": "string",
                    "enum": ["none", "primary", "signature", "domain"],
                    "description": (
                        "Which signature this from-address uses. "
                        "'signature' requires signature_id or signature_guid."
                    ),
                },
            },
            required=["action"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs)
        if action == "list":
            return self._list(client)
        if action == "get":
            return self._get(client, kwargs)
        if action == "create":
            return self._create(client, kwargs)
        if action == "update":
            return self._update(client, kwargs)
        if action == "delete":
            return self._delete(client, kwargs)
        if action == "map":
            return self._map(client, kwargs)
        raise ValidationError(f"unknown action: {action}")

    def _list_payload(self, client) -> Dict[str, Any]:
        sigs = self.call(client, "GET", "/api/v1/settings/user-signatures")
        maps = self.call(client, "GET", "/api/v1/settings/signature-mappings")
        return {
            "signatures": sigs.get("signatureConfigs") or [],
            "use_domain_signature": sigs.get("useDomainSignature"),
            "maps": maps.get("maps") or [],
        }

    def _find(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        data = self.call(client, "GET", "/api/v1/settings/user-signatures")
        configs = data.get("signatureConfigs") or []
        sid = kwargs.get("signature_id")
        guid = (kwargs.get("signature_guid") or "").strip()
        name = (kwargs.get("name") or "").strip()
        for sig in configs:
            if sid is not None and sid != "" and int(sig.get("id")) == int(sid):
                return sig
            if guid and str(sig.get("guid") or "") == guid:
                return sig
            if name and str(sig.get("name") or "") == name:
                return sig
        raise ToolExecutionError("signature not found")

    def _list(self, client) -> Dict[str, Any]:
        payload = self._list_payload(client)
        n = len(payload["signatures"])
        return format_success_response(
            f"{n} signature(s)", **payload
        )

    def _get(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        sig = self._find(client, kwargs)
        maps = self.call(client, "GET", "/api/v1/settings/signature-mappings")
        applied = [
            m for m in (maps.get("maps") or [])
            if m.get("signatureGuid") == sig.get("guid")
        ]
        return format_success_response(
            f"signature {sig.get('name')}",
            signature=sig,
            maps=applied,
        )

    def _create(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.require(kwargs, "name")
        body = {
            "signatureConfig": {
                "id": "",
                "name": kwargs["name"],
                "text": _as_html(kwargs.get("text") or ""),
            }
        }
        created = self.call(
            client, "POST", "/api/v1/settings/user-signature-put", json=body
        )
        return format_success_response(
            f"created signature {kwargs['name']}",
            signature_id=created.get("signatureID"),
            signature_guid=created.get("signatureGuid"),
        )

    def _update(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        sig = self._find(client, kwargs)
        name = kwargs.get("name") or sig.get("name")
        text = kwargs.get("text")
        if text is None:
            text = sig.get("text") or ""
        else:
            text = _as_html(text)
        body = {
            "signatureConfig": {
                "id": sig.get("id"),
                "guid": sig.get("guid"),
                "name": name,
                "text": text,
            }
        }
        self.call(client, "POST", "/api/v1/settings/user-signature", json=body)
        return format_success_response(
            f"updated signature {name}",
            signature_id=sig.get("id"),
            signature_guid=sig.get("guid"),
        )

    def _delete(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        sig = self._find(client, kwargs)
        sid = sig.get("id")
        self.call(
            client, "POST", f"/api/v1/settings/user-signature-delete/{sid}"
        )
        return format_success_response(
            f"deleted signature {sig.get('name')}",
            signature_id=sid,
        )

    def _map(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        maps_data = self.call(client, "GET", "/api/v1/settings/signature-mappings")
        maps = list(maps_data.get("maps") or [])
        from_address = (
            (kwargs.get("from_address") or "").strip() or client.email
        ).lower()
        guid = (kwargs.get("signature_guid") or "").strip() or None
        option = kwargs.get("map_option")
        if guid or kwargs.get("signature_id") or kwargs.get("name"):
            sig = self._find(client, kwargs)
            guid = sig.get("guid")
            if option in (None, ""):
                option = "signature"
        option_int = _map_option(option)
        if option_int == 2 and not guid:
            raise ValidationError(
                "signature_id or signature_guid required when map_option=signature"
            )
        if option_int != 2:
            guid = None

        updated = False
        for entry in maps:
            key = (entry.get("key") or "").lower()
            if key == from_address or (
                not key and entry.get("type") == 4
            ):
                entry["key"] = from_address
                entry["mapOption"] = option_int
                entry["signatureGuid"] = guid
                updated = True
                break
        if not updated:
            maps.append({
                "allowUsersToOverride": True,
                "key": from_address,
                "mapOption": option_int,
                "signatureGuid": guid,
                "type": 4,
            })
        self.call(
            client, "POST", "/api/v1/settings/signature-mappings",
            json={"signatureMaps": maps},
        )
        after = self.call(client, "GET", "/api/v1/settings/signature-mappings")
        return format_success_response(
            f"mapped {from_address} to {_SIG_MAP_NAME.get(option_int, option_int)}",
            from_address=from_address,
            map_option=option_int,
            signature_guid=guid,
            maps=after.get("maps") or [],
        )


# ---------------------------------------------------------------------------
# Trusted / blocked senders
# ---------------------------------------------------------------------------


class ManageSendersTool(_SettingsTool):
    """Trusted-sender whitelist and blocked-sender list."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_senders",
            "List, block, unblock, trust, or untrust senders on a SmarterMail "
            "mailbox. Block/unblock use settings/block-senders; trust by "
            "message UID uses mail/trust-senders, otherwise the whitelist.",
            {
                "action": {
                    "type": "string",
                    "enum": [
                        "list", "block", "unblock", "trust", "untrust", "check",
                    ],
                    "description": "Sender-list operation.",
                },
                "address": {
                    "type": "string",
                    "description": "Email address to block/unblock/trust/untrust/check.",
                },
                "addresses": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Multiple addresses (block/unblock/trust/untrust).",
                },
                "domain": {
                    "type": "string",
                    "description": "Domain to trust/untrust (whitelist domains).",
                },
                "uids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "Message UIDs in `folder` whose From is trusted/untrusted "
                        "via mail/trust-senders. Required for UID-based trust."
                    ),
                },
                "folder": {
                    "type": "string",
                    "description": "Folder for UID-based or block-senders calls. Default inbox.",
                },
                "is_sender": {
                    "type": "boolean",
                    "description": "True when the address is the message From. Default true.",
                },
            },
            required=["action"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs)
        if action == "list":
            return self._list(client)
        if action == "block":
            return self._block(client, kwargs, block=True)
        if action == "unblock":
            return self._block(client, kwargs, block=False)
        if action == "trust":
            return self._trust(client, kwargs, trust=True)
        if action == "untrust":
            return self._trust(client, kwargs, trust=False)
        if action == "check":
            return self._check(client, kwargs)
        raise ValidationError(f"unknown action: {action}")

    def _mail_settings(self, client) -> Dict[str, Any]:
        data = self.call(client, "GET", "/api/v1/settings/user-mail")
        return data.get("userMailSettings") or {}

    def _sender_snapshot(self, client) -> Dict[str, Any]:
        ums = self._mail_settings(client)
        return {
            "blocked_senders": list(ums.get("blockedSenders") or []),
            "blocked_domains": list(ums.get("blockedDomains") or []),
            "blocked_sender_action": ums.get("blockedSenderAction"),
            "trusted_senders": _wl_values(ums.get("whitelistAddresses")),
            "trusted_domains": _wl_values(ums.get("whitelistDomains")),
        }

    def _list(self, client) -> Dict[str, Any]:
        snap = self._sender_snapshot(client)
        return format_success_response(
            f"{len(snap['blocked_senders'])} blocked, "
            f"{len(snap['trusted_senders'])} trusted",
            **snap,
        )

    def _addresses(self, kwargs: Dict[str, Any], *, required: bool = True) -> List[str]:
        addrs = _as_list(kwargs.get("addresses"))
        if kwargs.get("address"):
            addrs.append(str(kwargs["address"]).strip())
        # de-dupe, preserve order
        seen = set()
        out = []
        for a in addrs:
            key = a.lower()
            if key and key not in seen:
                seen.add(key)
                out.append(a)
        if required and not out:
            raise ValidationError("address or addresses required")
        return out

    def _block(self, client, kwargs: Dict[str, Any], *, block: bool) -> Dict[str, Any]:
        addrs = self._addresses(kwargs)
        folder = (kwargs.get("folder") or "inbox").strip() or "inbox"
        is_sender = kwargs.get("is_sender")
        if is_sender is None:
            is_sender = True
        blocked = {addr.lower(): [] for addr in addrs}
        path = (
            "/api/v1/settings/block-senders" if block
            else "/api/v1/settings/unblock-senders"
        )
        result = self.call(
            client, "POST", path,
            json={
                "blockedSenders": blocked,
                "sourceFolder": folder,
                "sourceOwner": client.email,
                "isSender": bool(is_sender),
            },
        )
        snap = self._sender_snapshot(client)
        verb = "blocked" if block else "unblocked"
        return format_success_response(
            f"{verb} {', '.join(result.get('results') or addrs)}",
            results=result.get("results") or [],
            **snap,
        )

    def _trust(self, client, kwargs: Dict[str, Any], *, trust: bool) -> Dict[str, Any]:
        uids = kwargs.get("uids") or []
        if uids:
            folder = (kwargs.get("folder") or "inbox").strip() or "inbox"
            is_sender = kwargs.get("is_sender")
            if is_sender is None:
                is_sender = True
            path = (
                "/api/v1/mail/trust-senders" if trust
                else "/api/v1/mail/untrust-senders"
            )
            result = self.call(
                client, "POST", path,
                json={
                    "uids": list(uids),
                    "sourceOwner": client.email,
                    "sourceFolder": folder,
                    "isSender": bool(is_sender),
                },
            )
            snap = self._sender_snapshot(client)
            verb = "trusted" if trust else "untrusted"
            return format_success_response(
                f"{verb} via message UIDs",
                results=result.get("results") or [],
                **snap,
            )

        addrs = self._addresses(kwargs, required=False)
        domain = (kwargs.get("domain") or "").strip()
        if not addrs and not domain:
            raise ValidationError("address, addresses, domain, or uids required")

        ums = self._mail_settings(client)
        emails = _wl_values(ums.get("whitelistAddresses"))
        domains = _wl_values(ums.get("whitelistDomains"))
        emails_l = {e.lower() for e in emails}
        domains_l = {d.lower() for d in domains}

        if trust:
            for a in addrs:
                if a.lower() not in emails_l:
                    emails.append(a)
                    emails_l.add(a.lower())
            if domain and domain.lower() not in domains_l:
                domains.append(domain)
        else:
            drop_a = {a.lower() for a in addrs}
            emails = [e for e in emails if e.lower() not in drop_a]
            if domain:
                domains = [d for d in domains if d.lower() != domain.lower()]

        self.call(
            client, "POST", "/api/v1/settings/whitelist",
            json={"emails": emails, "domains": domains},
        )
        snap = self._sender_snapshot(client)
        verb = "trusted" if trust else "untrusted"
        target = ", ".join(addrs + ([domain] if domain else []))
        return format_success_response(f"{verb} {target}", **snap)

    def _check(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.require(kwargs, "address")
        addr = str(kwargs["address"]).strip()
        trusted = self.call(
            client, "POST", "/api/v1/settings/sender-trusted/",
            json={"email": addr},
        )
        blocked = self.call(
            client, "POST", "/api/v1/settings/sender-blocked/",
            json={"email": addr},
        )
        return format_success_response(
            f"checked {addr}",
            address=addr,
            trusted=trusted,
            blocked=blocked,
        )


# ---------------------------------------------------------------------------
# Auto-clean / retention
# ---------------------------------------------------------------------------


class ManageAutoCleanTool(_SettingsTool):
    """Mailbox auto-clean (retention) rules."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_auto_clean",
            "Read or replace this mailbox's auto-clean / retention rules. "
            "Setting rules requires allowUserOverride on the domain.",
            {
                "action": {
                    "type": "string",
                    "enum": ["get", "set"],
                    "description": "get current rules, or set user override + rules.",
                },
                "user_override_active": {
                    "type": "boolean",
                    "description": "Enable the user-level rule set (set action).",
                },
                "user_rules": {
                    "type": "array",
                    "description": (
                        "Replacement userRules list. Each rule: folderName, type "
                        "(0=size, 1=age), processThresholdMb, processGoalMb, "
                        "thresholdDays. Empty list + user_override_active=false "
                        "clears the override."
                    ),
                    "items": {"type": "object"},
                },
            },
            required=["action"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs)
        if action == "get":
            data = self.call(client, "GET", "/api/v1/settings/auto-clean")
            cal = {}
            try:
                cal = self.call(client, "GET", "/api/v1/settings/auto-clean-calendar")
            except ToolExecutionError:
                cal = {}
            return format_success_response(
                "auto-clean settings",
                auto_clean=data,
                calendar_auto_clean=cal or None,
            )
        if action == "set":
            current = self.call(client, "GET", "/api/v1/settings/auto-clean")
            override = kwargs.get("user_override_active")
            if override is None:
                override = current.get("userOverrideActive", False)
            rules = kwargs.get("user_rules")
            if rules is None:
                rules = current.get("userRules") or []
            normalized = []
            for rule in rules:
                item = dict(rule)
                if "processThresholdMb" in item and "processThresholdBytes" not in item:
                    item["processThresholdBytes"] = int(
                        item["processThresholdMb"]
                    ) * 1024 * 1024
                if "processGoalMb" in item and "processGoalBytes" not in item:
                    item["processGoalBytes"] = int(item["processGoalMb"]) * 1024 * 1024
                normalized.append(item)
            self.call(
                client, "POST", "/api/v1/settings/auto-clean",
                json={
                    "userOverrideActive": bool(override),
                    "userRules": normalized,
                },
            )
            after = self.call(client, "GET", "/api/v1/settings/auto-clean")
            return format_success_response("updated auto-clean", auto_clean=after)
        raise ValidationError(f"unknown action: {action}")


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


class ManageNotesTool(_SettingsTool):
    """Mailbox notes (sticky notes) and note folders."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_notes",
            "List, create, update, or delete SmarterMail notes. Also lists note "
            "folders (sources). EWS has no notes API.",
            {
                "action": {
                    "type": "string",
                    "enum": [
                        "list", "get", "create", "update", "delete",
                        "list_sources", "add_source", "rename_source",
                        "delete_source",
                    ],
                    "description": "Note or note-folder operation.",
                },
                "note_id": {
                    "type": "string",
                    "description": "Note id (get/update/delete).",
                },
                "subject": {
                    "type": "string",
                    "description": "Note subject (create/update). Defaults to the first 15 chars of text.",
                },
                "text": {
                    "type": "string",
                    "description": "Note body. HTML is passed through; plain text is wrapped.",
                },
                "color": {
                    "type": "string",
                    "description": "Note color. Default white.",
                },
                "source_id": {
                    "type": "string",
                    "description": "Notes folder id. Default: the primary Notes folder.",
                },
                "source_owner": {
                    "type": "string",
                    "description": "Notes folder owner username. Default: the primary folder's owner.",
                },
                "folder": {
                    "type": "string",
                    "description": "Folder name for add_source / rename_source.",
                },
            },
            required=["action"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs)
        dispatch = {
            "list": self._list,
            "get": self._get,
            "create": self._create,
            "update": self._update,
            "delete": self._delete,
            "list_sources": self._list_sources,
            "add_source": self._add_source,
            "rename_source": self._rename_source,
            "delete_source": self._delete_source,
        }
        handler = dispatch.get(action)
        if handler is None:
            raise ValidationError(f"unknown action: {action}")
        return handler(client, kwargs)

    def _sources(self, client) -> List[Dict[str, Any]]:
        data = self.call(client, "GET", "/api/v1/notes/sources")
        return data.get("sharedLists") or data.get("sources") or []

    def _primary_source(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        sources = self._sources(client)
        sid = (kwargs.get("source_id") or "").strip()
        owner = (kwargs.get("source_owner") or "").strip()
        if sid:
            for src in sources:
                if str(src.get("itemID") or "") == sid:
                    if not owner or str(src.get("ownerUsername") or "") == owner:
                        return src
            # still usable even if not in the list
            return {
                "itemID": sid,
                "ownerUsername": owner or (client.email.split("@")[0]),
            }
        for src in sources:
            if src.get("isPrimary"):
                return src
        if sources:
            return sources[0]
        raise ToolExecutionError("no notes folder on this mailbox")

    def _list_notes_in(self, client, src: Dict[str, Any]) -> List[Dict[str, Any]]:
        owner = src.get("ownerUsername") or "~"
        sid = src.get("itemID")
        data = self.call(client, "GET", f"/api/v1/notes/notes/{owner}/{sid}/")
        notes = data.get("notes") or []
        for note in notes:
            note.setdefault("sourceId", sid)
            note.setdefault("sourceOwner", owner)
        return notes

    def _find_note(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        nid = (kwargs.get("note_id") or "").strip()
        if not nid:
            raise ValidationError("note_id required")
        sources = self._sources(client)
        sid = (kwargs.get("source_id") or "").strip()
        owner = (kwargs.get("source_owner") or "").strip()
        search = sources
        if sid:
            search = [s for s in sources if str(s.get("itemID") or "") == sid]
            if not search:
                search = [{"itemID": sid, "ownerUsername": owner or (client.email.split("@")[0])}]
        for src in search:
            if owner and str(src.get("ownerUsername") or "") != owner:
                continue
            for note in self._list_notes_in(client, src):
                if str(note.get("id") or "") == nid:
                    return note
        raise ToolExecutionError(f"note not found: {nid}")

    def _list(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        sources = self._sources(client)
        sid = (kwargs.get("source_id") or "").strip()
        if sid:
            sources = [s for s in sources if str(s.get("itemID") or "") == sid] or [
                self._primary_source(client, kwargs)
            ]
        notes: List[Dict[str, Any]] = []
        for src in sources:
            notes.extend(self._list_notes_in(client, src))
        return format_success_response(
            f"{len(notes)} note(s)",
            notes=notes,
            sources=sources,
            count=len(notes),
        )

    def _get(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        note = self._find_note(client, kwargs)
        return format_success_response("note", note=note)

    def _create(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        text = kwargs.get("text") or ""
        subject = (kwargs.get("subject") or "").strip()
        if not text and not subject:
            raise ValidationError("text or subject required")
        html = _as_html(text or subject)
        if not subject:
            # Match webmail: first 15 chars of visible text.
            visible = text or subject
            subject = visible[:15]
        src = self._primary_source(client, kwargs)
        owner = src.get("ownerUsername")
        sid = src.get("itemID")
        payload = {
            "text": html,
            "subject": subject,
            "color": kwargs.get("color") or "white",
            "sourceId": sid,
            "sourceOwner": owner,
            "sourcePermission": src.get("access") or 8,
            "source": {
                "itemID": sid,
                "ownerUsername": owner,
                "displayName": src.get("displayName"),
                "access": src.get("access"),
                "enabled": True,
            },
        }
        created = self.call(
            client, "POST", f"/api/v1/notes/note-put/{sid}/{owner}/",
            json=payload,
        )
        note = created.get("note") or created
        if isinstance(note, dict):
            note.setdefault("sourceId", sid)
            note.setdefault("sourceOwner", owner)
        return format_success_response(
            f"created note {subject}",
            note=note,
        )

    def _update(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        note = self._find_note(client, kwargs)
        if kwargs.get("text") is not None:
            note["text"] = _as_html(kwargs.get("text"))
        if kwargs.get("subject"):
            note["subject"] = kwargs["subject"]
        if kwargs.get("color"):
            note["color"] = kwargs["color"]
        owner = note.get("sourceOwner")
        sid = note.get("sourceId")
        nid = note.get("id")
        self.call(
            client, "POST",
            f"/api/v1/notes/note-patch/{nid}/{sid}/{owner}",
            json=note,
        )
        return format_success_response(f"updated note {note.get('subject')}", note=note)

    def _delete(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        note = self._find_note(client, kwargs)
        owner = note.get("sourceOwner")
        sid = note.get("sourceId")
        nid = note.get("id")
        # Path order is id / sourceId / owner (verified live).
        self.call(
            client, "POST",
            f"/api/v1/notes/note-delete/{nid}/{sid}/{owner}",
        )
        return format_success_response(
            f"deleted note {note.get('subject')}",
            note_id=nid,
        )

    def _list_sources(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        sources = self._sources(client)
        return format_success_response(
            f"{len(sources)} notes folder(s)",
            sources=sources,
        )

    def _add_source(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.require(kwargs, "folder")
        result = self.call(
            client, "POST", "/api/v1/notes/sources/add",
            json={"folder": kwargs["folder"]},
        )
        return format_success_response(
            f"created notes folder {kwargs['folder']}",
            result=result,
            sources=self._sources(client),
        )

    def _rename_source(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.require(kwargs, "folder", "source_id")
        result = self.call(
            client, "POST", "/api/v1/notes/sources/edit",
            json={"folder": kwargs["folder"], "uid": kwargs["source_id"]},
        )
        return format_success_response(
            f"renamed notes folder to {kwargs['folder']}",
            result=result,
            sources=self._sources(client),
        )

    def _delete_source(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        src = self._primary_source(client, kwargs)
        if src.get("isPrimary"):
            raise ValidationError("cannot delete the primary notes folder")
        payload = {
            "folder": src.get("displayName") or src.get("name"),
            "uid": src.get("itemID"),
            "id": src.get("itemID"),
            "itemID": src.get("itemID"),
            "isPrimary": False,
        }
        result = self.call(
            client, "POST", "/api/v1/notes/sources/delete", json=payload
        )
        return format_success_response(
            "deleted notes folder",
            result=result,
            sources=self._sources(client),
        )


# ---------------------------------------------------------------------------
# Disposable addresses
# ---------------------------------------------------------------------------


class ManageDisposableAddressTool(_SettingsTool):
    """Temporary (disposable) receiving aliases."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_disposable_address",
            "Get, create, extend, or revoke this mailbox's disposable address. "
            "Returns configured=false when none is active "
            "(USER_NO_DISPOSABLE_ADDRESS).",
            {
                "action": {
                    "type": "string",
                    "enum": ["get", "create", "extend", "delete"],
                    "description": "get the current alias, create one, extend its lifetime, or revoke it.",
                },
                "folder": {
                    "type": "string",
                    "description": "Folder that receives mail to the alias. Default inbox.",
                },
                "hours": {
                    "type": "integer",
                    "description": "Lifetime in hours for create (default 24) or extend (default 4).",
                },
            },
            required=["action"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs)
        if action == "get":
            return self._get(client)
        if action == "create":
            folder = (kwargs.get("folder") or "inbox").strip() or "inbox"
            hours = int(kwargs.get("hours") or 24)
            data = self.call(
                client, "POST", "/api/v1/mail/disposable-address",
                json={"folderName": folder, "hours": hours},
            )
            return format_success_response(
                f"created disposable address {data.get('address')}",
                configured=True,
                address=data.get("address"),
                expire_time=data.get("expireTime"),
                folder=folder,
            )
        if action == "extend":
            hours = int(kwargs.get("hours") or 4)
            data = self.call(
                client, "POST", f"/api/v1/mail/disposable-address/{hours}"
            )
            return format_success_response(
                f"extended disposable address by {hours}h",
                configured=True,
                expire_time=data.get("date") or data.get("expireTime"),
            )
        if action == "delete":
            self.call(client, "POST", "/api/v1/mail/disposable-address-delete")
            return format_success_response(
                "revoked disposable address", configured=False
            )
        raise ValidationError(f"unknown action: {action}")

    def _get(self, client) -> Dict[str, Any]:
        try:
            data = self.call(client, "GET", "/api/v1/mail/disposable-address")
        except ToolExecutionError as exc:
            if "USER_NO_DISPOSABLE_ADDRESS" in str(exc):
                return format_success_response(
                    "no disposable address configured",
                    configured=False,
                )
            raise
        alias = data.get("alias") or {}
        return format_success_response(
            f"disposable address {alias.get('address')}",
            configured=True,
            alias=alias,
            address=alias.get("address"),
            expire_time=alias.get("disposeExpireTime"),
            folder=alias.get("folder"),
        )


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


class ManageNotificationsTool(_SettingsTool):
    """In-app notification centre."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_notifications",
            "List, dismiss, or snooze SmarterMail notification-centre items.",
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "dismiss", "dismiss_all", "snooze"],
                    "description": "list items, dismiss one/many, dismiss all, or snooze.",
                },
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Notification ids (dismiss / snooze).",
                },
                "id": {
                    "type": "string",
                    "description": "Single notification id (dismiss / snooze).",
                },
                "snooze_minutes": {
                    "type": "integer",
                    "description": "Minutes to snooze. Default 15. 0 unsnoozes.",
                },
            },
            required=["action"],
        )

    def _ids(self, kwargs: Dict[str, Any], *, required: bool) -> List[str]:
        ids = _as_list(kwargs.get("ids"))
        if kwargs.get("id"):
            ids.append(str(kwargs["id"]))
        if required and not ids:
            raise ValidationError("id or ids required")
        return ids

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs)
        if action == "list":
            data = self.call(client, "GET", "/api/v1/notifications/list")
            items = data.get("items") or []
            return format_success_response(
                f"{len(items)} notification(s)",
                items=items,
                count=len(items),
            )
        if action == "dismiss":
            ids = self._ids(kwargs, required=True)
            self.call(
                client, "POST", "/api/v1/notifications/dismiss",
                json={"ids": ids},
            )
            return format_success_response(f"dismissed {len(ids)} notification(s)", ids=ids)
        if action == "dismiss_all":
            self.call(client, "POST", "/api/v1/notifications/dismiss-all")
            return format_success_response("dismissed all notifications")
        if action == "snooze":
            ids = self._ids(kwargs, required=True)
            minutes = kwargs.get("snooze_minutes")
            if minutes is None:
                minutes = 15
            minutes = int(minutes)
            until = None
            if minutes > 0:
                until = (
                    datetime.now(timezone.utc) + timedelta(minutes=minutes)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.call(
                client, "POST", "/api/v1/notifications/snooze",
                json={"ids": ids, "snoozeUntil": until},
            )
            return format_success_response(
                f"snoozed {len(ids)} notification(s)",
                ids=ids,
                snooze_until=until,
            )
        raise ValidationError(f"unknown action: {action}")


# ---------------------------------------------------------------------------
# Auto-complete
# ---------------------------------------------------------------------------


class ManageAutoCompleteTool(_SettingsTool):
    """Recipient auto-complete (suggested addresses)."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_auto_complete",
            "List, search, or delete entries from this mailbox's recipient "
            "auto-complete list (GAL + recently used).",
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "search", "delete"],
                    "description": "list all, search by prefix, or delete entries by id.",
                },
                "query": {
                    "type": "string",
                    "description": "Search string (search action).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max search hits. Default 20.",
                },
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Auto-complete entry ids to delete.",
                },
                "id": {
                    "type": "string",
                    "description": "Single auto-complete entry id to delete.",
                },
            },
            required=["action"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs)
        if action == "list":
            data = self.call(client, "GET", "/api/v1/settings/auto-complete-list")
            items = data.get("emailData") or []
            return format_success_response(
                f"{len(items)} auto-complete entries",
                entries=items,
                count=len(items),
            )
        if action == "search":
            query = (kwargs.get("query") or "").strip()
            if not query:
                raise ValidationError("query required")
            limit = int(kwargs.get("limit") or 20)
            data = self.call(
                client, "POST", "/api/v1/settings/auto-complete-list-search",
                json={"search": query, "requestLimit": limit},
            )
            items = data.get("emailData") or []
            return format_success_response(
                f"{len(items)} match(es) for {query!r}",
                entries=items,
                count=len(items),
                query=query,
            )
        if action == "delete":
            ids = _as_list(kwargs.get("ids"))
            if kwargs.get("id"):
                ids.append(str(kwargs["id"]))
            if not ids:
                raise ValidationError("id or ids required")
            username = client.email.split("@")[0]
            self.call(
                client, "POST", "/api/v1/settings/auto-complete-delete",
                json={"username": username, "idsToDelete": ids},
            )
            return format_success_response(
                f"deleted {len(ids)} auto-complete entries",
                ids=ids,
            )
        raise ValidationError(f"unknown action: {action}")


# ---------------------------------------------------------------------------
# Account security (app passwords + from-address). No password/2FA changes.
# ---------------------------------------------------------------------------


class ManageAccountSecurityTool(_SettingsTool):
    """App-password listing and from-address options. No password or 2FA changes."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_account_security",
            "List app passwords and from-address options for a SmarterMail "
            "mailbox. Does not change the account password or 2FA.",
            {
                "action": {
                    "type": "string",
                    "enum": [
                        "list_app_passwords",
                        "from_addresses",
                        "determine_from",
                    ],
                    "description": (
                        "list_app_passwords: devices that have an app password. "
                        "from_addresses: allowed From: identities. "
                        "determine_from: pick the From: for a reply/forward given recipients."
                    ),
                },
                "input": {
                    "type": "string",
                    "description": (
                        "Recipient string (to/cc/bcc) for determine_from."
                    ),
                },
                "reply": {
                    "type": "boolean",
                    "description": "determine_from: true if this is a reply (not a forward). Default false.",
                },
            },
            required=["action"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs)
        if action == "list_app_passwords":
            data = self.call(client, "POST", "/api/v1/settings/app-passwords")
            devices = data.get("devices") or []
            return format_success_response(
                f"{len(devices)} app password(s)",
                devices=devices,
                count=len(devices),
            )
        if action == "from_addresses":
            data = self.call(client, "GET", "/api/v1/settings/from-address-options")
            options = data.get("options") or []
            return format_success_response(
                f"{len(options)} from-address option(s)",
                options=options,
            )
        if action == "determine_from":
            self.require(kwargs, "input")
            reply = bool(kwargs.get("reply"))
            suffix = "true" if reply else "false"
            data = self.call(
                client, "POST",
                f"/api/v1/settings/determine-from-address/{suffix}",
                json={"input": kwargs["input"]},
            )
            addresses = data.get("addresses") or []
            return format_success_response(
                "determined from-address",
                addresses=addresses,
                reply=reply,
            )
        raise ValidationError(f"unknown action: {action}")


SMARTERMAIL_TOOLS = [
    ManageSignaturesTool,
    ManageSendersTool,
    ManageAutoCleanTool,
    ManageNotesTool,
    ManageDisposableAddressTool,
    ManageNotificationsTool,
    ManageAutoCompleteTool,
    ManageAccountSecurityTool,
]
