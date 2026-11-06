"""SmarterMail workspace tools: virtual folders, sharing, file storage, RSS, chat, reports.

These ride the private webmail JSON API (the same one the SmarterMail UI uses).
Payloads were read from the site bundle and live-verified against a hosted SmarterMail server.
File-storage upload is the resumable.js handshake the webmail uses: POST /api/upload
with every field in the multipart body (no query string) and context=file-storage.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .smartermail_base import SmarterMailTool
from ..exceptions import ToolExecutionError, ValidationError
from ..smartermail import SmarterMailError
from ..smartermail.filestorage import (
    download_file as download_storage_file,
    files_in,
    find_file,
    walk_folders,
)
from ..utils import format_success_response

# list-owned-folders/{n} — live mapping on this server (not the JS enum).
_OWNED_FOLDER_KIND = {
    "notes": "2",
    "contacts": "3",
    "calendar": "4",
    "tasks": "5",
    "2": "2",
    "3": "3",
    "4": "4",
    "5": "5",
}

# UI default for a blank share-permission row.
_DEFAULT_SHARE_ACCESS = 4

# Built-in RSS root ("All Feeds") on this server.
_RSS_ROOT_FOLDER_ID = 30006

_VF_BRIEF_KEYS = (
    "folderId",
    "folderName",
    "folderPath",
    "folderType",
    "type",
    "guid",
    "ownerEmailAddress",
    "accessPermissionCount",
    "isMappedToMe",
    "isPrimary",
    "color",
    "allowWebcalSubscription",
    "access",
    "includeSubfolders",
)


def _action(kwargs: Dict[str, Any], default: Optional[str] = None) -> str:
    action = (kwargs.get("action") or "").strip().lower().replace("-", "_")
    if not action:
        if default:
            return default
        raise ValidationError("action is required")
    return action


def _seq(value: Any) -> List[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [v for v in value if v not in (None, "")]
    return [value]


def _as_str_list(value: Any) -> List[str]:
    return [str(v) for v in _seq(value)]


def _int(value: Any, name: str, *, required: bool = True) -> Optional[int]:
    if value is None or value == "":
        if required:
            raise ValidationError(f"{name} required")
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be an integer") from exc


def _raw_id(value: Any, name: str, *, required: bool = True) -> Optional[Any]:
    """Folder ids are ints; file-storage file ids are GUIDs."""
    if value is None or value == "":
        if required:
            raise ValidationError(f"{name} required")
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return int(text)
    return text


def _vf_obj(data: Dict[str, Any]) -> Any:
    return data.get("virtualFolder") or data.get("folder") or data



def _bool(value: Any, default: Optional[bool] = None) -> Optional[bool]:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    key = str(value).strip().lower()
    if key in ("1", "true", "yes", "on"):
        return True
    if key in ("0", "false", "no", "off"):
        return False
    return bool(value)


def _ids_from(kwargs: Dict[str, Any], *names: str) -> List[int]:
    out: List[int] = []
    for name in names:
        for item in _seq(kwargs.get(name)):
            out.append(_int(item, name))
    # de-dupe, keep order
    seen = set()
    uniq: List[int] = []
    for i in out:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    return uniq


def _access_users(value: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in _seq(value):
        if isinstance(item, dict):
            username = (
                item.get("username")
                or item.get("userName")
                or item.get("email")
                or item.get("emailAddress")
                or ""
            )
            access = item.get("access", _DEFAULT_SHARE_ACCESS)
        else:
            username = str(item)
            access = _DEFAULT_SHARE_ACCESS
        if not username:
            continue
        rows.append({"username": str(username), "access": _int(access, "access")})
    return rows


def _access_groups(value: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in _seq(value):
        if isinstance(item, dict):
            group = item.get("group") or item.get("name") or ""
            access = item.get("access", _DEFAULT_SHARE_ACCESS)
        else:
            group = str(item)
            access = _DEFAULT_SHARE_ACCESS
        if not group:
            continue
        rows.append({"access": _int(access, "access"), "group": str(group)})
    return rows


def _brief_vf(folder: Dict[str, Any]) -> Dict[str, Any]:
    return {k: folder.get(k) for k in _VF_BRIEF_KEYS if k in folder}


#: The file-storage tree walk lives in src/smartermail/filestorage.py so
#: add_attachment(storage_file_id=...) rides the same wire.
_walk_fs = walk_folders


def _fs_compact(folder: Dict[str, Any]) -> Dict[str, Any]:
    body = dict(folder)
    body["files"] = None
    body["subFolders"] = None
    return body


def _fs_brief(node: Dict[str, Any]) -> Dict[str, Any]:
    files = node.get("files") or []
    nfiles = len(files) if isinstance(files, list) else 0
    count = node.get("count")
    if count is None:
        count = nfiles
    return {
        "id": node.get("id") or node.get("folderId"),
        "name": node.get("name") or node.get("folderName"),
        "path": node.get("path"),
        "parentId": node.get("parentId"),
        "type": node.get("type"),
        "size": node.get("size"),
        "file_count": count,
        "isRoot": node.get("isRoot") or node.get("path") == "/",
        "isAllFiles": node.get("isAllFiles"),
        "isShared": node.get("isShared"),
        "published": node.get("published"),
    }


def _file_brief(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": item.get("id"),
        "fileName": item.get("fileName") or item.get("name"),
        "folderId": item.get("folderId"),
        "folderPath": item.get("folderPath"),
        "size": item.get("size"),
        "type": item.get("type"),
        "published": item.get("published"),
        "isShared": item.get("isShared"),
        "attachedFile": item.get("attachedFile"),
        "cloudProvider": item.get("cloudProvider"),
        "publicDownloadLink": item.get("publicDownloadLink"),
    }


def _upload_ident(filename: str, size: int) -> str:
    stem = "".join(ch for ch in filename if ch.isalnum() or ch in "-_") or "file"
    return f"{size}-{stem}"


def _file_bytes(kwargs: Dict[str, Any]) -> bytes:
    raw_b64 = kwargs.get("content_base64")
    if raw_b64 not in (None, ""):
        try:
            return base64.b64decode(str(raw_b64))
        except Exception as exc:
            raise ValidationError("content_base64 is not valid base64") from exc
    raw = kwargs.get("content")
    if raw in (None, ""):
        raise ValidationError("content or content_base64 required")
    if isinstance(raw, bytes):
        return raw
    return str(raw).encode("utf-8")


def _rss_feeds(hier: Dict[str, Any]) -> List[Dict[str, Any]]:
    feeds: List[Dict[str, Any]] = []

    def walk(folders: List[Dict[str, Any]]) -> None:
        for folder in folders or []:
            for feed in folder.get("feeds") or []:
                row = dict(feed)
                row.setdefault("folderId", folder.get("folderId"))
                row["folderName"] = folder.get("displayName")
                feeds.append(row)
            walk(folder.get("children") or [])

    walk(hier.get("folders") or [])
    return feeds


def _rss_folders(hier: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    def walk(folders: List[Dict[str, Any]]) -> None:
        for folder in folders or []:
            out.append(
                {
                    "folderId": folder.get("folderId"),
                    "guid": folder.get("guid"),
                    "displayName": folder.get("displayName"),
                    "folderIdParent": folder.get("folderIdParent"),
                    "feed_count": len(folder.get("feeds") or []),
                }
            )
            walk(folder.get("children") or [])

    walk(hier.get("folders") or [])
    return out


def _item_brief(item: Dict[str, Any]) -> Dict[str, Any]:
    desc = item.get("plainTextDescription") or item.get("description") or ""
    if isinstance(desc, str) and len(desc) > 280:
        desc = desc[:277] + "..."
    return {
        "title": item.get("title"),
        "link": item.get("link"),
        "pubDate": item.get("pubDate") or item.get("published"),
        "author": item.get("author"),
        "crc": item.get("crc"),
        "feedId": item.get("feedId"),
        "read": item.get("read"),
        "guid": item.get("guid"),
        "description": desc,
    }


def _services(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = data.get("connectedService")
    if raw is None:
        raw = data.get("connectedServices")
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [x for x in raw if x]
    return []


def _window(kwargs: Dict[str, Any]) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    end = kwargs.get("end") or now.strftime("%Y-%m-%dT%H:%M:%SZ")
    start = kwargs.get("start") or (now - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00Z")
    return str(start), str(end)


def _parse_report_blob(data: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(data)
    for key in ("report", "jsonString"):
        raw = out.get(key)
        if isinstance(raw, str):
            try:
                out[key] = json.loads(raw)
            except json.JSONDecodeError:
                pass
    return out


class _WorkspaceTool(SmarterMailTool):
    """SmarterMailTool that runs the blocking HTTP hop off the event loop."""

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    async def execute(self, **kwargs) -> Dict[str, Any]:
        return await asyncio.to_thread(self._run, kwargs)

    def _run(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        client = self.client(kwargs.get("target_mailbox"))
        return self._dispatch(client, kwargs)

    def _download(self, client, method: str, path: str, **kw) -> bytes:
        try:
            return client.download(method, path, **kw)
        except SmarterMailError as exc:
            raise ToolExecutionError(str(exc)) from exc

    def _get_or_post(self, client, path: str, json: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Webmail uses GET for some folder fetches; assignment lists them POST-only."""
        try:
            return self.call(client, "GET", path)
        except ToolExecutionError as exc:
            if "405" not in str(exc) and "404" not in str(exc):
                raise
            return self.call(client, "POST", path, json=json or {})

    def _fs_root(self, client) -> Dict[str, Any]:
        data = self.call(client, "GET", "/api/v1/filestorage/folders")
        return data.get("folder") or {}

    def _fs_find(self, root: Dict[str, Any], kwargs: Dict[str, Any], *, required: bool = True) -> Optional[Dict[str, Any]]:
        folder_id = _int(kwargs.get("folder_id"), "folder_id", required=False)
        path = (kwargs.get("path") or kwargs.get("folder_path") or "") or None
        name = (kwargs.get("folder_name") or kwargs.get("name") or "") or None
        nodes = _walk_fs([root] if root else [])
        if folder_id is not None:
            for node in nodes:
                if node.get("id") == folder_id:
                    return node
            if required:
                raise ValidationError(f"file-storage folder id {folder_id} not found")
            return None
        if path:
            want = path if str(path).startswith("/") else "/" + str(path).lstrip("/")
            want_noslash = want.rstrip("/") or "/"
            want_slash = want_noslash if want_noslash == "/" else want_noslash + "/"
            for node in nodes:
                p = node.get("path") or ""
                p_noslash = p.rstrip("/") or "/"
                if p in (want, path, want_slash, want_noslash) or p_noslash == want_noslash:
                    return node
            if required:
                raise ValidationError(f"file-storage path {path!r} not found")
            return None
        if name:
            hits = [n for n in nodes if n.get("name") == name and not n.get("isRoot") and not n.get("isAllFiles")]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise ValidationError(f"multiple file-storage folders named {name!r}; pass folder_id")
            if required:
                raise ValidationError(f"file-storage folder {name!r} not found")
            return None
        if required:
            raise ValidationError("folder_id, path, or folder_name required")
        return None

    def _rss_hier(self, client) -> Dict[str, Any]:
        return self.call(client, "GET", "/api/v1/rss/feed-hierarchy")


# ---------------------------------------------------------------------------
# Virtual folders (saved / shared folder views)
# ---------------------------------------------------------------------------


class ManageVirtualFoldersTool(_WorkspaceTool):
    """Saved cross-folder searches / shared folder views."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_virtual_folders",
            (
                "List, share, colour, attach, detach, or stop-sharing SmarterMail "
                "virtual folders (shared folder views). Creating a virtual folder "
                "means sharing an existing owned folder (notes/contacts/calendar/"
                "tasks/mail) via set-owned-virtual-folder-properties."
            ),
            {
                "action": {
                    "type": "string",
                    "enum": [
                        "list",
                        "list_owned",
                        "list_unshared",
                        "get",
                        "get_owned",
                        "share",
                        "create",
                        "update_share",
                        "set_color",
                        "attach",
                        "detach",
                        "stop_sharing",
                        "delete",
                    ],
                    "description": (
                        "list = folders shared with me; list_owned = folders I share; "
                        "list_unshared = owned folders not yet shared; get / get_owned "
                        "fetch one; share/create/update_share set permissions; "
                        "set_color sets calendar colour/webcal; attach/detach map a "
                        "shared folder onto this mailbox; stop_sharing/delete unshares."
                    ),
                },
                "folder_id": {
                    "type": "integer",
                    "description": "Virtual / owned folder id.",
                },
                "folder_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Folder ids for attach, detach, or stop_sharing.",
                },
                "access_users": {
                    "type": "array",
                    "description": (
                        "Share targets: emails or {username, access}. access is an "
                        "int; 4 is the webmail default. Empty list shares with nobody "
                        "(still creates the virtual-folder record)."
                    ),
                },
                "access_groups": {
                    "type": "array",
                    "description": "Group share targets: names or {group, access}.",
                },
                "include_subfolders": {
                    "type": "boolean",
                    "description": "Include subfolders when sharing. Default false.",
                },
                "color": {
                    "type": "string",
                    "description": "Calendar colour for set_color (e.g. '#ff0000').",
                },
                "allow_webcal": {
                    "type": "boolean",
                    "description": "Allow webcal subscription (set_color).",
                },
            },
            required=["action"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs)
        if action == "list":
            data = self.call(
                client, "POST", "/api/v1/folders/list-all-virtual-folders", json={}
            )
            folders = [_brief_vf(f) for f in (data.get("virtualFolders") or [])]
            return format_success_response(
                f"{len(folders)} virtual folder(s) shared with this mailbox",
                folders=folders,
                count=len(folders),
            )
        if action == "list_owned":
            data = self.call(
                client, "POST", "/api/v1/folders/list-owned-virtual-folders", json={}
            )
            folders = [_brief_vf(f) for f in (data.get("virtualFolders") or [])]
            return format_success_response(
                f"{len(folders)} owned virtual folder(s)",
                folders=folders,
                count=len(folders),
            )
        if action == "list_unshared":
            data = self.call(
                client, "POST", "/api/v1/folders/list-owned-unshared-folders", json={}
            )
            folders = [_brief_vf(f) for f in (data.get("virtualFolders") or [])]
            return format_success_response(
                f"{len(folders)} unshared owned folder(s)",
                folders=folders,
                count=len(folders),
            )
        if action == "get":
            folder_id = _int(kwargs.get("folder_id"), "folder_id")
            data = self._get_or_post(
                client,
                f"/api/v1/folders/get-virtual-folder/{folder_id}",
                json={"folderId": folder_id},
            )
            return format_success_response(
                f"virtual folder {folder_id}",
                folder=_vf_obj(data),
            )
        if action == "get_owned":
            folder_id = _int(kwargs.get("folder_id"), "folder_id")
            data = self._get_or_post(
                client,
                f"/api/v1/folders/get-owned-virtual-folder/{folder_id}",
                json={"folderId": folder_id},
            )
            return format_success_response(
                f"owned virtual folder {folder_id}",
                folder=_vf_obj(data),
            )
        if action in ("share", "create", "update_share"):
            folder_id = _int(kwargs.get("folder_id"), "folder_id")
            users = _access_users(kwargs.get("access_users") or kwargs.get("users"))
            groups = _access_groups(kwargs.get("access_groups") or kwargs.get("groups"))
            include = _bool(kwargs.get("include_subfolders"), False)
            self.call(
                client,
                "POST",
                "/api/v1/folders/set-owned-virtual-folder-properties",
                json={
                    "folderId": folder_id,
                    "accessUsers": users,
                    "accessGroups": groups,
                    "includeSubfolders": bool(include),
                },
            )
            owned = self.call(
                client, "POST", "/api/v1/folders/list-owned-virtual-folders", json={}
            )
            match = [
                _brief_vf(f)
                for f in (owned.get("virtualFolders") or [])
                if f.get("folderId") == folder_id
            ]
            return format_success_response(
                f"shared folder {folder_id} with {len(users)} user(s), {len(groups)} group(s)",
                folder_id=folder_id,
                access_users=users,
                access_groups=groups,
                include_subfolders=bool(include),
                folder=match[0] if match else None,
            )
        if action == "set_color":
            folder_id = _int(kwargs.get("folder_id"), "folder_id")
            color = kwargs.get("color")
            if color in (None, ""):
                raise ValidationError("color required")
            allow = _bool(kwargs.get("allow_webcal"), False)
            self.call(
                client,
                "POST",
                "/api/v1/folders/set-virtual-folder-properties",
                json={
                    "folderId": folder_id,
                    "color": str(color),
                    "allowWebcalSubscription": bool(allow),
                },
            )
            return format_success_response(
                f"updated colour on folder {folder_id}",
                folder_id=folder_id,
                color=str(color),
                allow_webcal=bool(allow),
            )
        if action in ("attach", "detach"):
            ids = _ids_from(kwargs, "folder_ids", "folder_id")
            if not ids:
                raise ValidationError("folder_id or folder_ids required")
            path = (
                "/api/v1/folders/attach-to-virtual-folders"
                if action == "attach"
                else "/api/v1/folders/detach-from-virtual-folders"
            )
            self.call(client, "POST", path, json={"folderIds": ids})
            verb = "attached" if action == "attach" else "detached"
            return format_success_response(f"{verb} {len(ids)} folder(s)", folder_ids=ids)
        if action in ("stop_sharing", "delete"):
            ids = _ids_from(kwargs, "folder_ids", "folder_id")
            if not ids:
                raise ValidationError("folder_id or folder_ids required")
            self.call(
                client,
                "POST",
                "/api/v1/folders/stop-sharing-folders",
                json={"folderIds": ids},
            )
            return format_success_response(
                f"stopped sharing {len(ids)} folder(s)",
                folder_ids=ids,
            )
        raise ValidationError(f"unknown action: {action}")


# ---------------------------------------------------------------------------
# Folder sharing + mailbox delegation
# ---------------------------------------------------------------------------


class ManageFolderSharingTool(_WorkspaceTool):
    """Email-folder shares, owned shared folders, and mailbox delegates."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_folder_sharing",
            (
                "Inspect email-folder shares and mailbox delegation, list owned "
                "shared folders (notes/contacts/calendar/tasks), stop sharing, "
                "remove delegates, or disconnect from a share mapped onto this mailbox."
            ),
            {
                "action": {
                    "type": "string",
                    "enum": [
                        "list_shares",
                        "list_email_shares",
                        "list_owned",
                        "list_unshared",
                        "stop_sharing",
                        "list_delegates",
                        "remove_delegates",
                        "disconnect",
                    ],
                    "description": (
                        "list_shares lists folders this mailbox shares (pass email to "
                        "inspect another user); list_owned uses list-owned-folders/2-5; "
                        "list_delegates reads settings/delegation; remove_delegates and "
                        "disconnect mutate; stop_sharing takes folder ids."
                    ),
                },
                "email": {
                    "type": "string",
                    "description": "Mailbox whose shares to list (list_shares). Default: this mailbox.",
                },
                "kind": {
                    "type": "string",
                    "description": (
                        "Owned-folder kind for list_owned: notes, contacts, calendar, "
                        "tasks, or 2-5. Omit / 'all' to fetch every kind."
                    ),
                },
                "folder_id": {"type": "integer"},
                "folder_ids": {"type": "array", "items": {"type": "integer"}},
                "delegates": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Delegate email(s) to remove.",
                },
                "delegate": {
                    "type": "string",
                    "description": "Single delegate email to remove.",
                },
                "share": {
                    "type": "object",
                    "description": "Share-connector object for disconnect (from list).",
                },
                "shares": {
                    "type": "array",
                    "description": "Share-connector objects for disconnect.",
                },
            },
            required=["action"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs)
        if action in ("list_shares", "list_email_shares"):
            email = (kwargs.get("email") or client.email or "").strip()
            if not email:
                raise ValidationError("email required")
            data = self.call(
                client,
                "POST",
                "/api/v1/folders/list-email-folder-shares",
                json={"email": email},
            )
            folders = data.get("folderList") or []
            return format_success_response(
                f"{len(folders)} shared folder(s) for {email}",
                email=email,
                folders=folders,
                count=data.get("count", len(folders)),
            )
        if action == "list_owned":
            kind = str(kwargs.get("kind") or "all").strip().lower()
            kinds = (
                ["2", "3", "4", "5"]
                if kind in ("", "all", "*")
                else [_OWNED_FOLDER_KIND.get(kind, "")]
            )
            if not kinds or not kinds[0]:
                raise ValidationError("kind must be notes, contacts, calendar, tasks, 2-5, or all")
            grouped: Dict[str, Any] = {}
            total = 0
            labels = {"2": "notes", "3": "contacts", "4": "calendar", "5": "tasks"}
            for code in kinds:
                data = self.call(client, "GET", f"/api/v1/folders/list-owned-folders/{code}")
                folders = data.get("folders") or data.get("virtualFolders") or data.get("folderList") or []
                grouped[labels.get(code, code)] = folders
                total += len(folders)
            return format_success_response(
                f"{total} owned folder(s)",
                folders=grouped,
                count=total,
            )
        if action == "list_unshared":
            data = self.call(
                client, "POST", "/api/v1/folders/list-owned-unshared-folders", json={}
            )
            folders = [_brief_vf(f) for f in (data.get("virtualFolders") or [])]
            return format_success_response(
                f"{len(folders)} unshared owned folder(s)",
                folders=folders,
                count=len(folders),
            )
        if action == "stop_sharing":
            ids = _ids_from(kwargs, "folder_ids", "folder_id")
            if not ids:
                raise ValidationError("folder_id or folder_ids required")
            self.call(
                client,
                "POST",
                "/api/v1/folders/stop-sharing-folders",
                json={"folderIds": ids},
            )
            return format_success_response(
                f"stopped sharing {len(ids)} folder(s)",
                folder_ids=ids,
            )
        if action == "list_delegates":
            data = self.call(client, "GET", "/api/v1/settings/delegation")
            delegates = data.get("delegateAccounts") or data.get("delegates") or data.get("delegateList") or []
            return format_success_response(
                f"{len(delegates)} delegate(s)",
                delegates=delegates,
                count=len(delegates),
            )
        if action == "remove_delegates":
            emails = _as_str_list(kwargs.get("delegates"))
            if kwargs.get("delegate"):
                emails.append(str(kwargs["delegate"]))
            emails = [e for e in emails if e]
            if not emails:
                raise ValidationError("delegate or delegates required")
            self.call(
                client,
                "POST",
                "/api/v1/settings/delegation-remove",
                json={"input": emails},
            )
            return format_success_response(
                f"removed {len(emails)} delegate(s)",
                delegates=emails,
            )
        if action == "disconnect":
            connectors = _seq(kwargs.get("shares"))
            if kwargs.get("share"):
                connectors.append(kwargs["share"])
            if not connectors:
                raise ValidationError("share or shares required")
            self.call(
                client,
                "POST",
                "/api/v1/settings/disconnect-from-share",
                json={"shareConnectorData": connectors},
            )
            return format_success_response(
                f"disconnected {len(connectors)} share(s)",
                count=len(connectors),
            )
        raise ValidationError(f"unknown action: {action}")


# ---------------------------------------------------------------------------
# Personal file storage
# ---------------------------------------------------------------------------


class ManageFileStorageTool(_WorkspaceTool):
    """Personal SmarterMail file-storage drive (folders, files, upload, download)."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_file_storage",
            (
                "List, create, rename, move, or delete folders in the personal "
                "SmarterMail drive; upload/list/delete/move/download files; read "
                "disk usage. Upload uses the webmail resumable.js handshake "
                "(POST /api/upload, multipart body, context=file-storage).\n"
                "This drive is not mail. To send a stored file in one mail-tool "
                "call, pass files=[{storage_file_id: <GUID>}] to send_email, "
                "reply_email or forward_email with the same target_mailbox. "
                "Draft creators also accept files; add_attachment(storage_file_id=...) "
                "is for follow-on changes before send_email(draft_id=...). "
                "For a file too big to attach, use attach_file_link instead and "
                "put the download URL in the body."
            ),
            {
                "action": {
                    "type": "string",
                    "enum": [
                        "list",
                        "list_folders",
                        "list_files",
                        "create_folder",
                        "rename_folder",
                        "move_folder",
                        "delete_folder",
                        "upload",
                        "delete_files",
                        "move_files",
                        "download",
                        "download_bulk",
                        "disk_usage",
                    ],
                },
                "folder_id": {"type": "integer", "description": "File-storage folder id."},
                "folder_name": {"type": "string", "description": "Folder name (create / lookup)."},
                "name": {"type": "string", "description": "New name for rename_folder / create_folder."},
                "path": {"type": "string", "description": "Folder path, e.g. /Reports/."},
                "parent_id": {
                    "type": "integer",
                    "description": "Parent folder id for create_folder / move_folder. 0 = root.",
                },
                "parent_path": {
                    "type": "string",
                    "description": "Parent folder path for create_folder / move_folder.",
                },
                "file_id": {
                    "type": "string",
                    "description": "File GUID (delete/move/download).",
                },
                "file_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "File GUIDs for delete_files / move_files / download_bulk.",
                },
                "destination_folder_id": {
                    "type": "integer",
                    "description": "Destination folder id for move_files / move_folder.",
                },
                "destination_path": {
                    "type": "string",
                    "description": "Destination folder path for move_files / move_folder.",
                },
                "filename": {
                    "type": "string",
                    "description": "Filename for upload.",
                },
                "content": {
                    "type": "string",
                    "description": "UTF-8 file body for upload (use content_base64 for binary).",
                },
                "content_base64": {
                    "type": "string",
                    "description": "Base64-encoded file body for upload.",
                },
                "content_type": {
                    "type": "string",
                    "description": "MIME type for upload. Default application/octet-stream.",
                },
            },
            required=["action"],
        )

    def _upload_file(
        self,
        client,
        folder_path: str,
        filename: str,
        body: bytes,
        content_type: str,
    ) -> Dict[str, Any]:
        n = len(body)
        ident = _upload_ident(filename, n)
        form = {
            "context": "file-storage",
            "contextData": json.dumps({"folder": folder_path}),
            "resumableChunkNumber": "1",
            "resumableChunkSize": "2097152",
            "resumableCurrentChunkSize": str(n),
            "resumableTotalSize": str(n),
            "resumableType": content_type,
            "resumableIdentifier": ident,
            "resumableFilename": filename,
            "resumableRelativePath": filename,
            "resumableTotalChunks": "1",
        }
        headers = {"Authorization": f"Bearer {client._token()}"}
        try:
            resp = client._client.post(
                "/api/upload",
                headers=headers,
                data=form,
                files={"file": (filename, body, content_type)},
            )
        except Exception as exc:
            raise ToolExecutionError(f"file-storage upload failed: {exc}") from exc
        text = (resp.text or "")[:500]
        if resp.status_code >= 400:
            raise ToolExecutionError(f"file-storage upload HTTP {resp.status_code}: {text}")
        try:
            payload = resp.json()
        except Exception as exc:
            raise ToolExecutionError(f"file-storage upload returned non-JSON: {text}") from exc
        if not isinstance(payload, dict):
            raise ToolExecutionError(f"file-storage upload returned unexpected payload: {payload!r}")
        if payload.get("success") is False or payload.get("message") == "MAX_SIZE_EXCEEDED":
            raise ToolExecutionError(
                f"file-storage upload failed: {payload.get('message') or payload}"
            )
        return payload

    def _files_in(self, node: Dict[str, Any]) -> List[Dict[str, Any]]:
        return files_in(node)

    def _find_file(self, root: Dict[str, Any], file_id: Any) -> Optional[Dict[str, Any]]:
        return find_file(root, file_id)

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs)
        if action in ("list", "list_folders"):
            root = self._fs_root(client)
            folders = [_fs_brief(n) for n in _walk_fs([root] if root else [])]
            return format_success_response(
                f"{len(folders)} file-storage folder(s)",
                folders=folders,
                count=len(folders),
            )
        if action == "list_files":
            root = self._fs_root(client)
            folder = self._fs_find(root, kwargs, required=False)
            if folder is None:
                files: List[Dict[str, Any]] = []
                for node in _walk_fs([root] if root else []):
                    for item in self._files_in(node):
                        row = _file_brief(item)
                        row["folderPath"] = node.get("path")
                        files.append(row)
                return format_success_response(
                    f"{len(files)} file(s) across file storage",
                    files=files,
                    count=len(files),
                )
            files = [_file_brief(f) for f in self._files_in(folder)]
            return format_success_response(
                f"{len(files)} file(s) in {folder.get('path') or folder.get('name')}",
                folder=_fs_brief(folder),
                files=files,
                count=len(files),
            )
        if action == "create_folder":
            name = (kwargs.get("name") or kwargs.get("folder_name") or "").strip()
            if not name:
                raise ValidationError("name required")
            root = self._fs_root(client)
            parent_path = "/"
            parent_id = _int(kwargs.get("parent_id"), "parent_id", required=False)
            if parent_id:
                parent = self._fs_find(root, {"folder_id": parent_id}, required=True)
                parent_path = (parent or {}).get("path") or "/"
            elif kwargs.get("path") or kwargs.get("parent_path"):
                parent_path = str(kwargs.get("parent_path") or kwargs.get("path"))
            if not parent_path:
                parent_path = "/"
            data = self.call(
                client,
                "POST",
                "/api/v1/filestorage/folder-put",
                json={"folder": name, "parentFolder": parent_path},
            )
            created = data.get("folder") or data
            created_path = created.get("path") if isinstance(created, dict) else None
            if created_path:
                found = self._fs_find(self._fs_root(client), {"path": created_path}, required=False)
                if found:
                    created = found
            return format_success_response(
                f"created file-storage folder {name}",
                folder=_fs_brief(created) if isinstance(created, dict) else {"name": name, "path": created},
                path=created_path,
            )
        if action == "rename_folder":
            new_name = (kwargs.get("name") or kwargs.get("folder_name") or "").strip()
            if not new_name:
                raise ValidationError("name required")
            root = self._fs_root(client)
            folder = self._fs_find(root, kwargs, required=True)
            old_path = folder.get("path") or ""
            if old_path in ("", "/"):
                raise ValidationError("refusing to rename the file-storage root")
            self.call(
                client,
                "POST",
                "/api/v1/filestorage/folder-patch",
                json={"folder": old_path, "newFolderName": new_name},
            )
            parent = old_path.rsplit("/", 1)[0] or ""
            new_path = f"{parent}/{new_name}" if parent not in ("", "/") else f"/{new_name}"
            return format_success_response(
                f"renamed folder to {new_name}",
                folder={"name": new_name, "path": new_path, "id": folder.get("id")},
            )
        if action == "move_folder":
            root = self._fs_root(client)
            folder = self._fs_find(root, kwargs, required=True)
            dest_parent = _int(
                kwargs.get("parent_id") or kwargs.get("destination_folder_id"),
                "parent_id",
                required=False,
            )
            if dest_parent:
                parent = self._fs_find(root, {"folder_id": dest_parent}, required=True)
                dest_path = (parent or {}).get("path") or "/"
            else:
                dest_path = str(kwargs.get("parent_path") or "/") or "/"
            name = folder.get("name") or ""
            old_path = folder.get("path") or ""
            if old_path in ("", "/"):
                raise ValidationError("refusing to move the file-storage root")
            self.call(
                client,
                "POST",
                "/api/v1/filestorage/folder-patch",
                json={
                    "folder": old_path,
                    "newFolderName": name,
                    "parentFolder": "",
                    "newParentFolder": dest_path,
                },
            )
            return format_success_response(
                f"moved folder {name} under {dest_path}",
                folder={"name": name, "from": old_path, "parent": dest_path},
            )
        if action == "delete_folder":
            root = self._fs_root(client)
            folder = self._fs_find(root, kwargs, required=True)
            path = folder.get("path") or ""
            if path in ("", "/") or folder.get("isRoot") or folder.get("isAllFiles"):
                raise ValidationError("refusing to delete the file-storage root")
            self.call(
                client,
                "POST",
                "/api/v1/filestorage/delete-folder",
                json={"folder": path},
            )
            return format_success_response(
                f"deleted file-storage folder {folder.get('name')}",
                folder_id=folder.get("id"),
                path=path,
            )
        if action == "delete_files":
            ids = [_raw_id(v, "file_id") for v in _seq(kwargs.get("file_ids")) + _seq(kwargs.get("file_id"))]
            if not ids:
                raise ValidationError("file_id or file_ids required")
            self.call(
                client, "POST", "/api/v1/filestorage/delete-files", json={"fileIDs": ids}
            )
            return format_success_response(
                f"deleted {len(ids)} file(s)",
                file_ids=ids,
            )
        if action == "move_files":
            ids = [_raw_id(v, "file_id") for v in _seq(kwargs.get("file_ids")) + _seq(kwargs.get("file_id"))]
            if not ids:
                raise ValidationError("file_id or file_ids required")
            dest_id = _int(
                kwargs.get("destination_folder_id"),
                "destination_folder_id",
                required=False,
            )
            root = self._fs_root(client)
            if dest_id:
                dest = self._fs_find(root, {"folder_id": dest_id}, required=True)
                dest_path = dest.get("path")
            else:
                dest_path = kwargs.get("path") or kwargs.get("destination_path")
                if not dest_path:
                    dest = self._fs_find(root, kwargs, required=True)
                    dest_path = dest.get("path")
            self.call(
                client,
                "POST",
                "/api/v1/filestorage/move-files",
                json={"fileIDs": ids, "newFolder": dest_path},
            )
            return format_success_response(
                f"moved {len(ids)} file(s) to {dest_path}",
                file_ids=ids,
                destination_path=dest_path,
            )
        if action == "download":
            file_id = _raw_id(kwargs.get("file_id"), "file_id")
            try:
                blob, name = download_storage_file(client, file_id)
            except SmarterMailError as exc:
                raise ToolExecutionError(str(exc)) from exc
            filename = name or f"{file_id}.bin"
            return format_success_response(
                f"downloaded {filename} ({len(blob)} bytes)",
                file_id=file_id,
                filename=filename,
                size=len(blob),
                content_base64=base64.b64encode(blob).decode("ascii"),
            )
        if action == "download_bulk":
            ids = [_raw_id(v, "file_id") for v in _seq(kwargs.get("file_ids")) + _seq(kwargs.get("file_id"))]
            if not ids:
                raise ValidationError("file_id or file_ids required")
            blob = self._download(
                client,
                "POST",
                "/api/v1/filestorage/download-bulk",
                json={"fileIDs": ids},
            )
            return format_success_response(
                f"bulk-downloaded {len(ids)} file(s) ({len(blob)} bytes)",
                file_ids=ids,
                size=len(blob),
                filename="filestorage-bulk-download.zip",
                content_base64=base64.b64encode(blob).decode("ascii"),
            )
        if action == "upload":
            filename = (kwargs.get("filename") or kwargs.get("file_name") or "").strip()
            filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
            if not filename or filename in (".", ".."):
                raise ValidationError("filename required")
            body = _file_bytes(kwargs)
            content_type = (
                (kwargs.get("content_type") or "application/octet-stream").strip()
                or "application/octet-stream"
            )
            root = self._fs_root(client)
            if kwargs.get("folder_id") or kwargs.get("path") or kwargs.get("folder_name"):
                folder = self._fs_find(root, kwargs, required=True)
            else:
                folder = root
            folder_path = (folder or {}).get("path") or "/"
            uploaded = self._upload_file(client, folder_path, filename, body, content_type)
            return format_success_response(
                f"uploaded {filename} ({len(body)} bytes) to {folder_path}",
                file=_file_brief(uploaded) if isinstance(uploaded, dict) else {"fileName": filename},
                folder_path=folder_path,
                size=len(body),
            )
        if action == "disk_usage":
            data = self.call(client, "GET", "/api/v1/filestorage/disk-usage-summary")
            usage = data.get("diskUsage") or data
            return format_success_response("file-storage disk usage", disk_usage=usage)
        raise ValidationError(f"unknown action: {action}")


# ---------------------------------------------------------------------------
# File-storage links used from compose / chat
# ---------------------------------------------------------------------------


class AttachFileLinkTool(_WorkspaceTool):
    """Generate a share link for a stored file (not a MIME attachment)."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "attach_file_link",
            (
                "Generate a public download link for a file already in SmarterMail "
                "storage (filestorage/{id}/getlink), browse connected cloud folders "
                "via mail/filestorage, or move/remove files that were attached to a "
                "compose draft. This is a storage link, not a MIME attachment - "
                "nothing is added to the message and the recipient needs network "
                "access to the link; the compose UI posts the same getlink URL into "
                "the message body. Use it for files that are too large to attach "
                "(or that the recipient should fetch later); for a real attachment "
                "use add_attachment(storage_file_id=<same GUID>) on a draft, which "
                "embeds the bytes in the message. SmarterMail mailboxes only."
            ),
            {
                "action": {
                    "type": "string",
                    "enum": [
                        "generate_link",
                        "mail_link",
                        "browse",
                        "move_attached",
                        "remove_attached",
                    ],
                    "description": (
                        "generate_link = native file getlink; mail_link = cloud "
                        "filestorage-link/{type}/{id}; browse = mail/filestorage tree; "
                        "move_attached / remove_attached operate on draft-attached files."
                    ),
                },
                "file_id": {
                    "type": "string",
                    "description": "Native file GUID (getlink) or cloud file id (mail_link).",
                },
                "type": {
                    "type": "string",
                    "description": "Cloud provider typeString for mail_link / browse (e.g. DROPBOX, ONEDRIVE).",
                },
                "folder": {
                    "type": "string",
                    "description": "Cloud folder key for browse.",
                },
                "old_owner": {"type": "string"},
                "new_owner": {"type": "string"},
                "old_folder_id": {"type": "integer"},
                "new_folder_id": {"type": "integer"},
                "old_item_id": {"type": "string"},
                "new_item_id": {"type": "string"},
                "folder_id": {"type": "integer", "description": "Draft folder id for remove_attached."},
                "owner": {"type": "string", "description": "Draft owner for remove_attached."},
                "remove_file_references": {
                    "type": "boolean",
                    "description": "Also drop compose file references. Default true.",
                },
            },
            required=["action"],
        )

    def _link_fields(self, data: Dict[str, Any]) -> Dict[str, Any]:
        blob = data.get("result") if isinstance(data.get("result"), dict) else data
        if not isinstance(blob, dict):
            blob = data
        link = (
            blob.get("link")
            or blob.get("url")
            or blob.get("publicLink")
            or blob.get("downloadUrl")
            or blob.get("href")
            or data.get("link")
            or data.get("publicLink")
        )
        return {
            "link": link,
            "short_link": blob.get("shortLink") or blob.get("short_link"),
            "raw": data,
        }

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs)
        if action == "generate_link":
            file_id = _raw_id(kwargs.get("file_id"), "file_id")
            data = self.call(client, "GET", f"/api/v1/filestorage/{file_id}/getlink")
            extra = self._link_fields(data)
            return format_success_response(
                f"share link for file {file_id}",
                file_id=file_id,
                link=extra["link"],
                short_link=extra["short_link"],
                result=extra["raw"],
            )
        if action == "mail_link":
            file_id = _raw_id(kwargs.get("file_id"), "file_id")
            type_string = (kwargs.get("type") or "").strip()
            if not type_string:
                raise ValidationError("type required")
            # Webmail: POST /mail/filestorage-link/{typeString} {folder, id}
            payload = {
                "folder": kwargs.get("folder") or "",
                "id": file_id,
            }
            data = self.call(
                client,
                "POST",
                f"/api/v1/mail/filestorage-link/{type_string}",
                json=payload,
            )
            extra = self._link_fields(data)
            return format_success_response(
                f"cloud share link for {type_string}/{file_id}",
                file_id=file_id,
                type=type_string,
                link=extra["link"] or data.get("publicLink"),
                result=extra["raw"],
            )
        if action == "browse":
            type_string = (kwargs.get("type") or "").strip()
            if not type_string:
                raise ValidationError("type required")
            folder = kwargs.get("folder")
            path = f"/api/v1/mail/filestorage/{type_string}"
            if folder not in (None, ""):
                path = f"{path}/{folder}"
            data = self.call(client, "GET", path)
            return format_success_response(
                f"listed {type_string} file-storage",
                type=type_string,
                folder=folder,
                result=data,
            )
        if action == "move_attached":
            payload = {
                "oldOwner": kwargs.get("old_owner") or "",
                "newOwner": kwargs.get("new_owner") or "",
                "oldFolderId": _int(kwargs.get("old_folder_id"), "old_folder_id"),
                "oldItemId": kwargs.get("old_item_id") or "",
                "newFolderId": _int(kwargs.get("new_folder_id"), "new_folder_id"),
                "newItemId": kwargs.get("new_item_id") or "",
            }
            data = self.call(
                client, "POST", "/api/v1/filestorage/move-attached-files", json=payload
            )
            return format_success_response("moved attached files", result=data)
        if action == "remove_attached":
            payload = {
                "folderId": _int(kwargs.get("folder_id"), "folder_id"),
                "owner": kwargs.get("owner") or client.email or "",
                "removeFileReferences": bool(
                    _bool(kwargs.get("remove_file_references"), True)
                ),
            }
            data = self.call(
                client, "POST", "/api/v1/filestorage/remove-attached-file", json=payload
            )
            return format_success_response("removed attached file", result=data)
        raise ValidationError(f"unknown action: {action}")


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------


class ManageRssFeedsTool(_WorkspaceTool):
    """RSS feed hierarchy, items, and folder CRUD."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_rss_feeds",
            (
                "List, add, edit, refresh, mark, or delete SmarterMail RSS feeds and "
                "feed folders; fetch items; validate a feed URL."
            ),
            {
                "action": {
                    "type": "string",
                    "enum": [
                        "list",
                        "add_feed",
                        "edit_feed",
                        "delete_feed",
                        "add_folder",
                        "edit_folder",
                        "delete_folder",
                        "get_items",
                        "refresh",
                        "mark_feed",
                        "mark_item",
                        "validate",
                    ],
                },
                "feed_id": {"type": "integer"},
                "folder_id": {
                    "type": "integer",
                    "description": "RSS folder id. Default for add_feed is All Feeds (30006).",
                },
                "parent_id": {
                    "type": "integer",
                    "description": "Parent RSS folder id for add_folder. Default All Feeds.",
                },
                "name": {"type": "string", "description": "Feed or folder display name."},
                "url": {"type": "string", "description": "Feed URL (add_feed / edit_feed / validate)."},
                "unread": {
                    "type": "boolean",
                    "description": "Mark feed/item unread (true) or read (false).",
                },
                "crc": {"type": "integer", "description": "Item crc from get_items (mark_item)."},
                "ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Feed ids for get_items. Omit to fetch every subscribed feed.",
                },
            },
            required=["action"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs)
        if action == "list":
            hier = self._rss_hier(client)
            feeds = _rss_feeds(hier)
            folders = _rss_folders(hier)
            return format_success_response(
                f"{len(feeds)} feed(s) in {len(folders)} folder(s)",
                feeds=feeds,
                folders=folders,
                count=len(feeds),
            )
        if action == "add_feed":
            name = (kwargs.get("name") or "").strip()
            url = (kwargs.get("url") or "").strip()
            if not name:
                raise ValidationError("name required")
            if not url:
                raise ValidationError("url required")
            folder_id = (
                _int(kwargs.get("folder_id"), "folder_id", required=False) or _RSS_ROOT_FOLDER_ID
            )
            data = self.call(
                client,
                "POST",
                "/api/v1/rss/feed-put",
                json={"name": name, "url": url, "folderId": folder_id},
            )
            return format_success_response(
                f"added RSS feed {name}",
                feed_id=data.get("id"),
                name=name,
                url=url,
                folder_id=folder_id,
            )
        if action == "edit_feed":
            feed_id = _int(kwargs.get("feed_id"), "feed_id")
            name = (kwargs.get("name") or "").strip()
            url = (kwargs.get("url") or "").strip()
            folder_id = _int(kwargs.get("folder_id"), "folder_id", required=False)
            if not name or not url:
                # fill from hierarchy
                feeds = _rss_feeds(self._rss_hier(client))
                current = next((f for f in feeds if f.get("id") == feed_id), None)
                if current is None:
                    raise ValidationError(f"feed {feed_id} not found")
                name = name or current.get("displayName") or ""
                url = url or current.get("feedUrl") or ""
                if folder_id is None:
                    folder_id = current.get("folderId")
            if folder_id is None:
                folder_id = _RSS_ROOT_FOLDER_ID
            self.call(
                client,
                "POST",
                f"/api/v1/rss/feed-patch/{feed_id}",
                json={
                    "feedId": feed_id,
                    "name": name,
                    "url": url,
                    "folderId": folder_id,
                },
            )
            return format_success_response(
                f"updated RSS feed {feed_id}",
                feed_id=feed_id,
                name=name,
                url=url,
                folder_id=folder_id,
            )
        if action == "delete_feed":
            feed_id = _int(kwargs.get("feed_id"), "feed_id")
            self.call(client, "POST", f"/api/v1/rss/feed-delete/{feed_id}")
            return format_success_response(f"deleted RSS feed {feed_id}", feed_id=feed_id)
        if action == "add_folder":
            name = (kwargs.get("name") or "").strip()
            if not name:
                raise ValidationError("name required")
            parent_id = (
                _int(kwargs.get("parent_id") or kwargs.get("folder_id"), "parent_id", required=False)
                or _RSS_ROOT_FOLDER_ID
            )
            data = self.call(
                client,
                "POST",
                "/api/v1/rss/folder-put",
                json={"parentFolderId": parent_id, "name": name},
            )
            created = data.get("folder") or data
            created_id = None
            if isinstance(created, dict):
                created_id = created.get("folderId") or created.get("id")
            return format_success_response(
                f"created RSS folder {name}",
                folder=created,
                folder_id=created_id,
                name=name,
                parent_id=parent_id,
            )
        if action == "edit_folder":
            folder_id = _int(kwargs.get("folder_id"), "folder_id")
            name = (kwargs.get("name") or "").strip()
            if not name:
                raise ValidationError("name required")
            parent_id = _int(kwargs.get("parent_id"), "parent_id", required=False)
            if parent_id is None:
                folders = _rss_folders(self._rss_hier(client))
                current = next((f for f in folders if f.get("folderId") == folder_id), None)
                parent_id = (current or {}).get("folderIdParent") or _RSS_ROOT_FOLDER_ID
            self.call(
                client,
                "POST",
                "/api/v1/rss/folder-patch",
                json={"folderId": folder_id, "parentFolderId": parent_id, "name": name},
            )
            return format_success_response(
                f"renamed RSS folder {folder_id} to {name}",
                folder_id=folder_id,
                name=name,
                parent_id=parent_id,
            )
        if action == "delete_folder":
            folder_id = _int(kwargs.get("folder_id"), "folder_id")
            if folder_id == _RSS_ROOT_FOLDER_ID:
                raise ValidationError("refusing to delete the All Feeds root")
            self.call(
                client,
                "POST",
                "/api/v1/rss/delete-folder",
                json={"folderId": folder_id},
            )
            return format_success_response(f"deleted RSS folder {folder_id}", folder_id=folder_id)
        if action == "get_items":
            ids = _ids_from(kwargs, "ids", "feed_id")
            if not ids:
                ids = [f.get("id") for f in _rss_feeds(self._rss_hier(client)) if f.get("id") is not None]
            if not ids:
                return format_success_response("no RSS feeds subscribed", items=[], count=0)
            data = self.call(
                client, "POST", "/api/v1/rss/get-rss-data", json={"ids": ids}
            )
            items = [_item_brief(x) for x in (data.get("data") or data.get("items") or [])]
            return format_success_response(
                f"{len(items)} RSS item(s)",
                feed=data.get("rssFeed"),
                items=items,
                count=len(items),
                feed_ids=ids,
            )
        if action == "refresh":
            ids = _ids_from(kwargs, "ids", "feed_id")
            if not ids:
                raise ValidationError("feed_id or ids required")
            data = self.call(
                client, "POST", "/api/v1/rss/refresh-feed", json={"ids": ids}
            )
            return format_success_response(
                f"refreshed {len(ids)} RSS feed(s)",
                feed_ids=ids,
                result=data,
            )
        if action == "mark_feed":
            ids = _ids_from(kwargs, "ids", "feed_id")
            if not ids:
                raise ValidationError("feed_id or ids required")
            unread = _bool(kwargs.get("unread"), False)
            mark_read = not unread
            data = self.call(
                client,
                "POST",
                f"/api/v1/rss/mark-feed/{str(mark_read).lower()}",
                json={"feedIds": ids},
            )
            state = "unread" if unread else "read"
            return format_success_response(
                f"marked {len(ids)} feed(s) {state}",
                feed_ids=ids,
                unread=bool(unread),
                counts=data.get("counts"),
            )
        if action == "mark_item":
            feed_id = _int(kwargs.get("feed_id"), "feed_id")
            crc = _int(kwargs.get("crc"), "crc")
            unread = _bool(kwargs.get("unread"), False)
            mark_read = not unread
            self.call(
                client,
                "POST",
                f"/api/v1/rss/mark-feed-item/{str(mark_read).lower()}",
                json={"feedId": feed_id, "crc": crc},
            )
            state = "unread" if unread else "read"
            return format_success_response(
                f"marked item crc={crc} {state}",
                feed_id=feed_id,
                crc=crc,
                unread=bool(unread),
            )
        if action == "validate":
            url = (kwargs.get("url") or "").strip()
            if not url:
                raise ValidationError("url required")
            data = self.call(
                client,
                "POST",
                "/api/v1/rss/validate-link-exists",
                json={"input": url},
            )
            return format_success_response(
                f"validated {url}",
                url=url,
                result=data,
            )
        raise ValidationError(f"unknown action: {action}")


# ---------------------------------------------------------------------------
# Chat history
# ---------------------------------------------------------------------------


class SearchChatHistoryTool(_WorkspaceTool):
    """Search, fetch, or download SmarterMail chat history."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "search_chat_history",
            (
                "Search SmarterMail chat history, fetch a transcript, or download the "
                "transcript zip. Search is asynchronous: this tool starts the job and "
                "polls until it finishes (or 12s elapse)."
            ),
            {
                "action": {
                    "type": "string",
                    "enum": ["search", "transcript", "download"],
                    "description": "Default search.",
                },
                "text": {"type": "string", "description": "Free-text query."},
                "from": {"type": "string", "description": "Sender / from JID filter."},
                "to": {"type": "string", "description": "Recipient / to JID filter."},
                "start": {
                    "type": "string",
                    "description": "Start datetime ISO-8601 UTC. Default 7 days ago.",
                },
                "end": {
                    "type": "string",
                    "description": "End datetime ISO-8601 UTC. Default now.",
                },
                "search_id": {
                    "type": "string",
                    "description": "Existing search id (skip starting a new search).",
                },
                "username": {
                    "type": "string",
                    "description": "Chat user for transcript (path segment).",
                },
                "chat_id": {
                    "type": "string",
                    "description": "Chat id for transcript (path segment).",
                },
                "jid": {
                    "type": "string",
                    "description": "Contact JID for download-transcript (user@domain).",
                },
                "language": {
                    "type": "string",
                    "description": "Transcript language code. Default en.",
                },
                "start_index": {
                    "type": "integer",
                    "description": "Search result offset. Default 0.",
                },
                "count": {
                    "type": "integer",
                    "description": "Search page size. Default 25.",
                },
            },
        )

    def _chat_window(self, kwargs: Dict[str, Any]) -> tuple[str, str]:
        now = datetime.now(timezone.utc)
        end = kwargs.get("end") or now.strftime("%Y-%m-%dT%H:%M:%SZ")
        start = kwargs.get("start") or (now - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00Z")
        return str(start), str(end)

    def _poll(self, client, search_id: str, start_index: int, count: int) -> Dict[str, Any]:
        deadline = time.time() + 12
        data: Dict[str, Any] = {}
        while time.time() < deadline:
            data = self.call(
                client,
                "GET",
                f"/api/v1/chat/history-search/{search_id}/{start_index}/{count}",
            )
            results = data.get("searchResults") or data
            percent = results.get("percentProgress")
            if percent is None or int(percent) >= 100:
                break
            time.sleep(0.4)
        return data

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs, default="search")
        start, end = self._chat_window(kwargs)
        if action == "search":
            search_id = kwargs.get("search_id") or kwargs.get("searchID")
            if not search_id:
                payload = {
                    "searchArgs": {
                        "from": kwargs.get("from") or "",
                        "to": kwargs.get("to") or "",
                        "text": kwargs.get("text") or "",
                        "startDateUtc": start,
                        "endDateUtc": end,
                    }
                }
                started = self.call(
                    client, "POST", "/api/v1/chat/history-search", json=payload
                )
                search_id = (
                    started.get("result") or started.get("searchID") or started.get("id")
                )
            if not search_id:
                raise ToolExecutionError("chat search did not return an id")
            start_index = int(kwargs.get("start_index") or 0)
            count = int(kwargs.get("count") or 25)
            data = self._poll(client, str(search_id), start_index, count)
            results = data.get("searchResults") or data
            chats = results.get("results") or results.get("chats") or []
            return format_success_response(
                f"{len(chats)} chat hit(s)",
                search_id=str(search_id),
                percent=results.get("percentProgress"),
                total=results.get("totalItems"),
                chats=chats,
                count=len(chats),
            )
        if action == "transcript":
            username = (kwargs.get("username") or kwargs.get("from") or "").strip()
            chat_id = str(kwargs.get("chat_id") or kwargs.get("id") or "").strip()
            if not username or not chat_id:
                raise ValidationError("username and chat_id required")
            data = self.call(
                client,
                "POST",
                f"/api/v1/chat/history-transcript/{username}/{chat_id}",
                json={"startDateUtc": start, "endDateUtc": end},
            )
            transcript = data.get("result") or data.get("chatTranscript") or data
            n = len(transcript) if isinstance(transcript, list) else 1
            return format_success_response(
                f"transcript {username}/{chat_id} ({n} line(s))",
                username=username,
                chat_id=chat_id,
                transcript=transcript,
            )
        if action == "download":
            jid = (kwargs.get("jid") or kwargs.get("username") or "").strip()
            if not jid:
                raise ValidationError("jid required")
            language = (kwargs.get("language") or "en").strip() or "en"
            blob = self._download(
                client,
                "GET",
                f"/api/v1/chat/download-transcript/{jid}/{language}",
            )
            return format_success_response(
                f"downloaded transcript for {jid} ({len(blob)} bytes)",
                jid=jid,
                language=language,
                size=len(blob),
                filename="chat-transcript.zip",
                content_base64=base64.b64encode(blob).decode("ascii"),
            )
        raise ValidationError(f"unknown action: {action}")


# ---------------------------------------------------------------------------
# Usage / quota
# ---------------------------------------------------------------------------


class MailboxUsageReportTool(_WorkspaceTool):
    """Mailbox + file-storage disk usage and optional traffic reports."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "mailbox_usage_report",
            (
                "Return mailbox / file-storage / quota numbers from "
                "filestorage/disk-usage-summary, report/disk-usage, report/disks, "
                "and optionally report/summary + report/details (DISK_USAGE)."
            ),
            {
                "include_summary": {
                    "type": "boolean",
                    "description": "Also POST report/summary/user. Default false.",
                },
                "include_details": {
                    "type": "boolean",
                    "description": "Also POST report/details/user/DISK_USAGE. Default true.",
                },
                "report": {
                    "type": "string",
                    "description": "Details report name. Default DISK_USAGE.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["user", "domain"],
                    "description": "Report scope. Default user.",
                },
                "start": {"type": "string", "description": "ISO-8601 start. Default 30 days ago."},
                "end": {"type": "string", "description": "ISO-8601 end. Default now (UTC)."},
            },
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        summary = self.call(client, "GET", "/api/v1/filestorage/disk-usage-summary")
        disk_usage = summary.get("diskUsage") or summary
        report_disk = None
        try:
            report_disk = self.call(client, "GET", "/api/v1/report/disk-usage")
        except ToolExecutionError:
            report_disk = None
        disks = None
        try:
            disks = self.call(client, "GET", "/api/v1/report/disks")
        except ToolExecutionError:
            disks = None

        extras: Dict[str, Any] = {}
        if _bool(kwargs.get("include_summary"), False):
            start, end = _window(kwargs)
            scope = (kwargs.get("scope") or "user").strip().lower()
            try:
                extras["summary"] = self.call(
                    client,
                    "POST",
                    f"/api/v1/report/summary/{scope}",
                    json={"startDate": start, "endDate": end},
                )
            except ToolExecutionError as exc:
                extras["summary_error"] = str(exc)
        if _bool(kwargs.get("include_details"), True):
            start, end = _window(kwargs)
            scope = (kwargs.get("scope") or "user").strip().lower()
            report = (kwargs.get("report") or "DISK_USAGE").strip()
            try:
                details = self.call(
                    client,
                    "POST",
                    f"/api/v1/report/details/{scope}/{report}/",
                    json={
                        "startDate": start,
                        "endDate": end,
                        "step": 0,
                        "groupBy": 0,
                    },
                )
                extras["details"] = _parse_report_blob(details)
            except ToolExecutionError as exc:
                extras["details_error"] = str(exc)

        used = disk_usage.get("used") if isinstance(disk_usage, dict) else None
        allowed = disk_usage.get("allowed") if isinstance(disk_usage, dict) else None
        fs_used = disk_usage.get("fileStorageUsed") if isinstance(disk_usage, dict) else None
        mailbox_used = disk_usage.get("mailboxUsed") if isinstance(disk_usage, dict) else None
        quota = "unlimited" if allowed in (0, None) else allowed
        return format_success_response(
            f"mailbox used={mailbox_used} file_storage={fs_used} total={used} quota={quota}",
            disk_usage=disk_usage,
            report_disk_usage=report_disk,
            disks=disks,
            used=used,
            allowed=allowed,
            mailbox_used=mailbox_used,
            file_storage_used=fs_used,
            **extras,
        )


# ---------------------------------------------------------------------------
# Connected cloud services (read / rename / delete — no OAuth)
# ---------------------------------------------------------------------------


class ManageConnectedServicesTool(_WorkspaceTool):
    """Dropbox / OneDrive / Google / ownCloud links already connected to the mailbox."""

    def get_schema(self) -> Dict[str, Any]:
        return self.schema(
            "manage_connected_services",
            (
                "List, rename, or delete connected cloud services (Dropbox, OneDrive, "
                "Google, ownCloud). Does not start OAuth — linking a new service has "
                "to happen in the webmail UI."
            ),
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "rename", "delete"],
                },
                "id": {"type": "integer", "description": "Connected-service id."},
                "ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Service ids for delete.",
                },
                "name": {
                    "type": "string",
                    "description": "New friendly name (rename).",
                },
            },
            required=["action"],
        )

    def _dispatch(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        action = _action(kwargs)
        if action == "list":
            data = self.call(client, "GET", "/api/v1/settings/connected-services")
            services = _services(data)
            return format_success_response(
                f"{len(services)} connected service(s)",
                services=services,
                count=len(services),
            )
        if action == "rename":
            service_id = _int(kwargs.get("id"), "id")
            name = (kwargs.get("name") or "").strip()
            if not name:
                raise ValidationError("name required")
            self.call(
                client,
                "POST",
                "/api/v1/settings/connected-service-rename",
                json={"id": service_id, "name": name},
            )
            return format_success_response(
                f"renamed connected service {service_id} to {name}",
                id=service_id,
                name=name,
            )
        if action == "delete":
            ids = _ids_from(kwargs, "ids", "id")
            if not ids:
                raise ValidationError("id or ids required")
            self.call(
                client,
                "POST",
                "/api/v1/settings/connected-services-delete",
                json={"serviceIDs": ids},
            )
            return format_success_response(
                f"deleted {len(ids)} connected service(s)",
                ids=ids,
            )
        raise ValidationError(f"unknown action: {action}")


SMARTERMAIL_TOOLS = [
    ManageVirtualFoldersTool,
    ManageFolderSharingTool,
    ManageFileStorageTool,
    AttachFileLinkTool,
    ManageRssFeedsTool,
    SearchChatHistoryTool,
    MailboxUsageReportTool,
    ManageConnectedServicesTool,
]
