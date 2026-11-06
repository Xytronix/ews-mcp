"""Short-lived server-side copies of outbound files (``stage_outgoing_file``).

Remote agents (via a gateway) share no filesystem with this container, so a
``file_path`` they know is meaningless here. Staging moves the bytes onto the
Email server once — from base64, an HTTP(S) URL or a server path — and hands
back an absolute path inside the container that ``files=[{"file_path": ...}]``
(and ``add_attachment``) can use any number of times until it expires.

Layout under :func:`staging_root` (``EWS_MCP_STAGING_DIR``, default
``/tmp/ews-mcp-staged``)::

    <root>/<staging_id>/<file_name>   the bytes (0600, directory 0700)
    <root>/<staging_id>.json          metadata: name, type, size, expiry

Metadata lives beside the directory, never inside it, so no caller-chosen file
name can collide with it. Expired entries are purged lazily on every stage /
lookup / delete, and a ``file_path`` inside the staging root is refused once
its entry has expired even if the purge has not run yet.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .exceptions import ValidationError

STAGING_DIR_ENV = "EWS_MCP_STAGING_DIR"
DEFAULT_STAGING_DIR = "/tmp/ews-mcp-staged"

DEFAULT_TTL_SECONDS = 3600
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 24 * 3600

_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_MAX_NAME_BYTES = 255


def staging_root() -> Path:
    """Absolute, symlink-resolved staging directory (read per call for tests)."""
    return Path(os.environ.get(STAGING_DIR_ENV) or DEFAULT_STAGING_DIR).resolve()


@dataclass(frozen=True)
class StagedFile:
    staging_id: str
    file_path: Path
    file_name: str
    size_bytes: int
    content_type: str
    created_at: float
    expires_at: float

    def to_dict(self, now: Optional[float] = None) -> Dict[str, Any]:
        current = time.time() if now is None else now
        return {
            "staging_id": self.staging_id,
            "file_path": str(self.file_path),
            "file_name": self.file_name,
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
            "expires_at": _iso(self.expires_at),
            "expires_in_seconds": max(0, int(self.expires_at - current)),
        }


def _iso(timestamp: float) -> str:
    moment = datetime.fromtimestamp(int(timestamp), timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


def validate_file_name(name: Any) -> str:
    """Return ``name`` if it is a bare file name, else raise.

    Rejected rather than silently rewritten: an agent that sends
    ``../../etc/cron.d/x`` has a bug (or worse) that it should hear about.
    """
    text = str(name or "").strip()
    if not text:
        raise ValidationError("file_name is required")
    if (
        "/" in text
        or "\\" in text
        or text in (".", "..")
        or any(not ch.isprintable() for ch in text)
    ):
        raise ValidationError(
            f"file_name {text!r} must be a bare file name: no path separators, "
            "'..' or control characters"
        )
    if len(text.encode("utf-8")) > _MAX_NAME_BYTES:
        raise ValidationError(f"file_name is longer than {_MAX_NAME_BYTES} bytes")
    return text


def clamp_ttl(value: Any) -> int:
    """``ttl_seconds`` bounded to [MIN_TTL_SECONDS, MAX_TTL_SECONDS]."""
    if value in (None, ""):
        return DEFAULT_TTL_SECONDS
    if isinstance(value, bool):
        raise ValidationError("ttl_seconds must be an integer number of seconds")
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        raise ValidationError("ttl_seconds must be an integer number of seconds") from None
    return max(MIN_TTL_SECONDS, min(MAX_TTL_SECONDS, seconds))


def _require_id(staging_id: Any) -> str:
    text = str(staging_id or "").strip().lower()
    if not _ID_PATTERN.match(text):
        raise ValidationError(
            "staging_id must be the 32-character id returned by stage_outgoing_file"
        )
    return text


def _meta_path(root: Path, staging_id: str) -> Path:
    return root / f"{staging_id}.json"


def _remove(root: Path, staging_id: str) -> bool:
    existed = False
    directory = root / staging_id
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)
        existed = True
    meta = _meta_path(root, staging_id)
    if meta.exists():
        meta.unlink(missing_ok=True)
        existed = True
    return existed


def _read_meta(root: Path, staging_id: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(_meta_path(root, staging_id).read_text("utf-8"))
    except (OSError, ValueError):
        return None


def purge_expired(now: Optional[float] = None) -> int:
    """Delete expired or orphaned entries; returns how many were removed."""
    root = staging_root()
    if not root.is_dir():
        return 0
    current = time.time() if now is None else now
    removed = 0
    for entry in root.iterdir():
        staging_id = entry.stem if entry.suffix == ".json" else entry.name
        if not _ID_PATTERN.match(staging_id):
            continue
        meta = _read_meta(root, staging_id)
        if meta is None:
            # Orphan (no metadata): only reap it once it is certainly stale,
            # so a stage in progress is never torn down under its writer.
            try:
                stale = entry.stat().st_mtime < current - MAX_TTL_SECONDS
            except OSError:
                continue
            if stale and _remove(root, staging_id):
                removed += 1
            continue
        if float(meta.get("expires_at", 0)) <= current and _remove(root, staging_id):
            removed += 1
    return removed


def stage_bytes(
    content: bytes,
    file_name: str,
    content_type: str,
    ttl_seconds: int,
    *,
    now: Optional[float] = None,
) -> StagedFile:
    """Write ``content`` to a fresh staging entry and return its description."""
    name = validate_file_name(file_name)
    current = time.time() if now is None else now
    purge_expired(current)

    root = staging_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging_id = uuid.uuid4().hex
    directory = root / staging_id
    directory.mkdir(mode=0o700)
    target = directory / name
    if target.resolve().parent != directory.resolve():
        _remove(root, staging_id)
        raise ValidationError(f"file_name {name!r} escapes the staging directory")

    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)

    staged = StagedFile(
        staging_id=staging_id,
        file_path=target,
        file_name=name,
        size_bytes=len(content),
        content_type=content_type,
        created_at=current,
        expires_at=current + ttl_seconds,
    )
    meta = {
        "file_name": name,
        "content_type": content_type,
        "size_bytes": staged.size_bytes,
        "created_at": staged.created_at,
        "expires_at": staged.expires_at,
    }
    scratch = root / f".{staging_id}.json.tmp"
    scratch.write_text(json.dumps(meta), "utf-8")
    os.replace(scratch, _meta_path(root, staging_id))
    return staged


def get_staged(staging_id: Any, *, now: Optional[float] = None) -> StagedFile:
    """Look up a live entry; expired or missing entries raise ValidationError."""
    sid = _require_id(staging_id)
    root = staging_root()
    current = time.time() if now is None else now
    meta = _read_meta(root, sid)
    if meta is None:
        raise ValidationError(
            f"staged file {sid} does not exist (already deleted or expired); "
            "stage it again with stage_outgoing_file"
        )
    if float(meta.get("expires_at", 0)) <= current:
        _remove(root, sid)
        raise ValidationError(
            f"staged file {sid} expired at {_iso(float(meta['expires_at']))}; "
            "stage it again with stage_outgoing_file"
        )
    path = root / sid / str(meta.get("file_name"))
    if not path.is_file():
        _remove(root, sid)
        raise ValidationError(
            f"staged file {sid} is gone from the server; stage it again with "
            "stage_outgoing_file"
        )
    return StagedFile(
        staging_id=sid,
        file_path=path,
        file_name=str(meta["file_name"]),
        size_bytes=int(meta.get("size_bytes", path.stat().st_size)),
        content_type=str(meta.get("content_type") or "application/octet-stream"),
        created_at=float(meta.get("created_at", 0)),
        expires_at=float(meta["expires_at"]),
    )


def delete_staged(staging_id: Any) -> bool:
    """Remove one entry (and purge expired ones); False if it did not exist."""
    sid = _require_id(staging_id)
    purge_expired()
    return _remove(staging_root(), sid)


def check_staged_path(path: Path, *, now: Optional[float] = None) -> None:
    """Refuse a ``file_path`` inside the staging root once its entry expired.

    Paths outside the staging root are not this module's business.
    """
    root = staging_root()
    try:
        relative = Path(os.path.abspath(path)).resolve().relative_to(root)
    except (ValueError, OSError):
        return
    if not relative.parts:
        raise ValidationError("file_path points at the staging directory, not a staged file")
    get_staged(relative.parts[0], now=now)
