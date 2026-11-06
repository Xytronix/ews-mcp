"""stage_outgoing_file / delete_staged_file: put outbound bytes on this server.

Remote agents cannot hand this container a path from their own box. Staging
takes base64, an HTTP(S) URL or a server path once, stores it under the
staging directory (see :mod:`src.attachment_staging`) and returns a
container-local ``file_path`` for ``files=[{"file_path": ...}]`` /
``add_attachment`` — reusable for a dry run and the real send without pushing
the base64 through the model twice.
"""

import asyncio
import os
from pathlib import Path
from typing import Any, Dict

from .base import BaseTool
from .. import attachment_staging as staging
from ..attachment_sources import (
    ALIAS_DESCRIPTION,
    MAX_REMOTE_ATTACHMENT_BYTES,
    REMOTE_SOURCE_PROPERTIES,
    load_attachment_content,
    normalize_source_aliases,
    resolve_content_type,
    selected_source,
)
from ..exceptions import ValidationError
from ..utils import format_success_response

_MAX_MB = MAX_REMOTE_ATTACHMENT_BYTES // (1024 * 1024)
#: base64 inflates by 4/3; allow line breaks / a data: prefix on top.
_MAX_BASE64_CHARS = MAX_REMOTE_ATTACHMENT_BYTES * 4 // 3 + 64 * 1024


def _refuse_oversize(size: int) -> None:
    if size > MAX_REMOTE_ATTACHMENT_BYTES:
        raise ValidationError(
            f"file is {size} bytes; stage_outgoing_file accepts at most {_MAX_MB}MB"
        )


class StageOutgoingFileTool(BaseTool):
    """Store one outbound file on the Email server and return its local path."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "stage_outgoing_file",
            "description": (
                "Upload a file to the Email MCP server for attaching later - the "
                "remote-agent way to get bytes onto this server. Supply exactly one "
                "of file_content (base64, requires file_name), file_url (server "
                "downloads it) or file_path (already on this server). Returns "
                "staging_id and an absolute file_path valid until expires_at: pass "
                "files=[{file_path: <returned file_path>}] to send_email / "
                "reply_email / forward_email / create_*_draft (dry_run=true first "
                "if you want a preview), or file_path to add_attachment. Delete early "
                "with delete_staged_file. " + ALIAS_DESCRIPTION
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file_content": {
                        "type": "string",
                        "description": (
                            "Base64 file content (data: URL prefix allowed); "
                            f"requires file_name. Max {_MAX_MB}MB decoded."
                        ),
                    },
                    "file_url": REMOTE_SOURCE_PROPERTIES["file_url"],
                    "file_path": {
                        "type": "string",
                        "description": (
                            "Path already on the Email server (not the caller's "
                            "machine) to copy into staging."
                        ),
                    },
                    "file_name": {
                        "type": "string",
                        "description": (
                            "Bare file name (no directories), e.g. 'photo.jpg'. "
                            "Required with file_content; otherwise overrides the "
                            "source name."
                        ),
                    },
                    "content_type": {
                        "type": "string",
                        "description": "MIME type; guessed from the file name when omitted.",
                    },
                    "ttl_seconds": {
                        "type": "integer",
                        "default": staging.DEFAULT_TTL_SECONDS,
                        "minimum": staging.MIN_TTL_SECONDS,
                        "maximum": staging.MAX_TTL_SECONDS,
                        "description": (
                            "Lifetime before the staged copy is deleted "
                            f"(default {staging.DEFAULT_TTL_SECONDS}s, clamped to "
                            f"{staging.MIN_TTL_SECONDS}-{staging.MAX_TTL_SECONDS}s)."
                        ),
                    },
                },
                "required": [],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        entry = normalize_source_aliases(kwargs)
        source = selected_source(entry)
        if source == "storage_file_id":
            raise ValidationError(
                "storage_file_id is already server-side: pass it straight to "
                "files=[{storage_file_id: ...}] instead of staging it"
            )
        ttl = staging.clamp_ttl(entry.get("ttl_seconds"))
        # Reject a traversal attempt instead of letting the loader strip it.
        explicit_name = entry.get("file_name")
        if explicit_name not in (None, ""):
            staging.validate_file_name(explicit_name)

        if source == "file_content":
            text = entry["file_content"]
            if isinstance(text, str) and len(text) > _MAX_BASE64_CHARS:
                raise ValidationError(
                    f"file_content is {len(text)} base64 characters; "
                    f"stage_outgoing_file accepts at most {_MAX_MB}MB decoded"
                )
        elif source == "file_path":
            path = Path(str(entry["file_path"]))
            try:
                size = await asyncio.to_thread(os.path.getsize, path)
            except OSError:
                size = None  # the loader reports the missing file with guidance
            if size is not None:
                _refuse_oversize(size)

        loaded = await load_attachment_content(entry, storage_client=None)
        _refuse_oversize(len(loaded.content))
        if not loaded.content:
            raise ValidationError("file is empty; nothing to stage")

        name = staging.validate_file_name(explicit_name or loaded.file_name)
        content_type = resolve_content_type(
            name, entry.get("content_type"), loaded.remote_content_type
        )
        staged = await asyncio.to_thread(
            staging.stage_bytes, loaded.content, name, content_type, ttl
        )
        self.logger.info(
            "Staged %s (%d bytes, %s) as %s for %ds",
            name, staged.size_bytes, loaded.source, staged.staging_id, ttl,
        )
        details = staged.to_dict()
        return format_success_response(
            "File staged on the Email server; attach it with "
            "files=[{file_path: <file_path>}]",
            **details,
            ttl_seconds=ttl,
            source=loaded.source,
            attach_with={"files": [{"file_path": details["file_path"]}]},
        )


class DeleteStagedFileTool(BaseTool):
    """Remove a staged file before it expires."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "delete_staged_file",
            "description": (
                "Delete a file staged with stage_outgoing_file before its expiry "
                "(expired files are removed automatically)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "staging_id": {
                        "type": "string",
                        "description": "staging_id returned by stage_outgoing_file.",
                    },
                },
                "required": ["staging_id"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        staging_id = kwargs.get("staging_id")
        deleted = await asyncio.to_thread(staging.delete_staged, staging_id)
        return format_success_response(
            "Staged file deleted" if deleted else "Nothing to delete (already gone or expired)",
            staging_id=str(staging_id).strip().lower(),
            deleted=deleted,
        )
