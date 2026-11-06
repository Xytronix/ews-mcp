"""Content sources for one-shot outbound files and follow-on attachments.

Agents reach this server over the network (via a gateway), so they share no
filesystem with the container: ``file_path`` only ever sees paths *inside* the
image and ``file_content`` forces the whole payload through the model context.
Remote-friendly sources fix that and are resolved here once for both the
Exchange (exchangelib) and the routed Graph/IMAP implementations:

``file_url``
    An HTTP(S) URL the server fetches itself. Guarded against SSRF: only
    ``http``/``https``, and every hop's host must resolve exclusively to global
    unicast addresses (loopback, link-local — which covers the 169.254.169.254
    cloud metadata endpoint — private RFC1918/ULA, CGNAT, multicast, reserved
    and unspecified addresses are refused). Names are re-validated after each
    redirect, and redirects are capped. Validation happens on the addresses
    returned by DNS just before the request; a rebinding attacker who flips the
    record between our resolution and httpx's own connect could still slip
    through — closing that needs connect-time pinning, which is deliberately
    out of scope for an operator-run mail server.

``storage_file_id``
    A file GUID from the mailbox's personal SmarterMail drive (the ids
    ``manage_file_storage`` lists). The server downloads it server-side, so
    nothing crosses the model context.

Remote fetches are capped at :data:`MAX_REMOTE_ATTACHMENT_BYTES`.

``stage_outgoing_file`` (see :mod:`src.attachment_staging`) copies bytes onto
this server once and returns a container-local ``file_path``; a staged path is
refused here after it expires.

Agents habitually spell the fields differently (``filename``, ``content``,
``url`` ...). :func:`normalize_source_aliases` maps those spellings onto the
canonical names in this one place, and every selection/load path goes through
it.
"""

from __future__ import annotations

import asyncio
import email.message
import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import unquote, urlsplit

import httpx

from .exceptions import ToolExecutionError, ValidationError
from .smartermail import SmarterMailClient, SmarterMailError
from .smartermail.filestorage import download_file as download_storage_file
from . import attachment_staging
from .utils import decode_base64_payload, guess_content_type

#: Ceiling for anything the server fetches on the caller's behalf.
MAX_REMOTE_ATTACHMENT_BYTES = 25 * 1024 * 1024

#: Total budget for one URL fetch (connect + read).
URL_FETCH_TIMEOUT_SECONDS = 30.0

#: Redirect hops followed; each new location is SSRF-validated again.
MAX_URL_REDIRECTS = 3

#: Mutually exclusive ways to supply the bytes.
SOURCE_FIELDS = (
    "file_path",
    "file_content",
    "file_url",
    "storage_file_id",
)

_GENERIC_TYPES = frozenset({"application/octet-stream", "binary/octet-stream"})

STORAGE_UNAVAILABLE_HINT = (
    "storage_file_id needs a SmarterMail mailbox (personal file storage). "
    "Use file_url or file_content instead."
)

FILE_NAME_REQUIRED = (
    "file_name is required when using file_content (alias: filename), e.g. "
    "{file_content: <base64>, file_name: 'photo.jpg'}"
)

#: Returned as the error ``hint`` for every "no source" / "file not found"
#: failure, so an agent that tried a path from its own box learns the
#: remote-friendly alternatives (hints are not length-capped like messages).
REMOTE_AGENT_HINT = (
    "file_path is read on the Email MCP server's own filesystem, never the "
    "caller's. Remote agents: pass files=[{file_content: <base64>, "
    "file_name: 'x.jpg'}], file_url (server downloads it), storage_file_id "
    "(SmarterMail file storage), or call stage_outgoing_file first and use "
    "the file_path it returns."
)

#: Short form that fits inside the 200-character error message itself.
REMOTE_AGENT_POINTER = (
    "Remote agents: use file_content+file_name, file_url or stage_outgoing_file"
)

#: Accepted spellings -> canonical field. Applied only when the canonical
#: field is absent; ``filename`` is a name, never a content source.
SOURCE_ALIASES: Dict[str, str] = {
    "filename": "file_name",
    "content": "file_content",
    "base64": "file_content",
    "data": "file_content",
    "path": "file_path",
    "filepath": "file_path",
    "url": "file_url",
}

ALIAS_DESCRIPTION = (
    "Aliases accepted: filename -> file_name; content / base64 / data -> "
    "file_content; path / filepath -> file_path; url -> file_url."
)

#: Schema fragment shared by every add_attachment surface.
REMOTE_SOURCE_PROPERTIES: Dict[str, Any] = {
    "file_url": {
        "type": "string",
        "description": (
            "HTTP(S) URL the server downloads and attaches (use this instead "
            "of file_path when you have no filesystem in common with the "
            "server). https is strongly preferred; private, loopback and "
            "link-local hosts are refused. Max "
            f"{MAX_REMOTE_ATTACHMENT_BYTES // (1024 * 1024)}MB. The file name "
            "comes from Content-Disposition or the URL path unless file_name "
            "is given."
        ),
    },
    "storage_file_id": {
        "type": "string",
        "description": (
            "File GUID in the mailbox's personal SmarterMail file storage (as "
            "listed by manage_file_storage): the server downloads it and "
            "attaches it, so the bytes never pass through the conversation. "
            "For one-call sending, use files=[{storage_file_id: <GUID>}] with "
            "send_email, reply_email or forward_email. Draft creators also "
            "accept files; use add_attachment for follow-on changes and "
            "send_email(draft_id=...) when a reviewed draft is ready. For "
            "oversized files, explicitly generate a download link with "
            "attach_file_link and put it in the message body; there is no "
            "automatic link fallback. SmarterMail mailboxes only - for "
            "iCloud/IMAP or Outlook/Graph mailboxes use file_url or file_content."
        ),
    },
}


@dataclass(frozen=True)
class RemoteBlob:
    """Bytes fetched from a remote source, plus whatever it said about them."""

    content: bytes
    file_name: Optional[str] = None
    content_type: Optional[str] = None


@dataclass(frozen=True)
class LoadedAttachment:
    """Resolved attachment payload, whichever source supplied it."""

    content: bytes
    file_name: str
    source: str
    #: Content type reported by the source (never the caller's), if any.
    remote_content_type: Optional[str] = None
    #: Human-readable origin for the response payload (url, file id, path).
    source_detail: Optional[str] = None


# --------------------------------------------------------------------------
# source selection
# --------------------------------------------------------------------------
def _present(value: Any) -> bool:
    return value not in (None, "", [], {})


def normalize_source_aliases(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of ``entry`` with alias spellings renamed to canonical fields.

    An alias only fills a canonical field that is absent; the alias key is
    dropped either way so it can never be mistaken for a second source.
    """
    if not isinstance(entry, dict):
        return entry
    normalized = dict(entry)
    for alias, canonical in SOURCE_ALIASES.items():
        if alias not in normalized:
            continue
        value = normalized.pop(alias)
        if not _present(normalized.get(canonical)) and _present(value):
            normalized[canonical] = value
    return normalized


def selected_source(kwargs: Dict[str, Any]) -> str:
    """Name of the single content source in ``kwargs`` (aliases honoured).

    Silently preferring one of several sources hides a caller bug (and can
    attach the wrong file), so anything but exactly one is a ValidationError.
    """
    kwargs = normalize_source_aliases(kwargs)
    present = [f for f in SOURCE_FIELDS if _present(kwargs.get(f))]
    if not present:
        raise ValidationError(
            "one content source is required: "
            + ", ".join(SOURCE_FIELDS)
            + ". "
            + REMOTE_AGENT_POINTER,
            hint=REMOTE_AGENT_HINT,
        )
    if len(present) > 1:
        raise ValidationError(
            f"{', '.join(present)}: exactly one content source is allowed, "
            f"got {len(present)}"
        )
    return present[0]


def resolve_content_type(
    file_name: Optional[str],
    explicit: Optional[str] = None,
    remote: Optional[str] = None,
) -> str:
    """Caller's type, else the file name's, else what the source reported."""
    if explicit:
        return str(explicit)
    guessed = guess_content_type(file_name)
    if guessed not in _GENERIC_TYPES:
        return guessed
    if remote and remote.lower() not in _GENERIC_TYPES:
        return remote
    return guessed


# --------------------------------------------------------------------------
# file_url
# --------------------------------------------------------------------------
def _blocked_reason(ip: Any) -> Optional[str]:
    """Why this address may not be fetched, or ``None`` if it is fine."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    checks = (
        ("loopback", ip.is_loopback),
        ("link-local (includes cloud metadata)", ip.is_link_local),
        ("private", ip.is_private),
        ("multicast", ip.is_multicast),
        ("reserved", ip.is_reserved),
        ("unspecified", ip.is_unspecified),
    )
    for label, hit in checks:
        if hit:
            return label
    if not ip.is_global:
        return "not globally routable"
    return None


def _resolve_host(host: str) -> List[str]:
    """Every address ``host`` resolves to (a literal address resolves to itself)."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ToolExecutionError(f"file_url: cannot resolve host {host!r} ({exc})")
    return [info[4][0] for info in infos]


def _host_addresses(host: str) -> List[str]:
    """Addresses to vet for ``host``: the literal itself, or its DNS answers."""
    try:
        # A literal address must be checked as written; sending it through the
        # resolver would let a stub/hosts trick answer for it.
        return [str(ipaddress.ip_address(host.strip("[]")))]
    except ValueError:
        pass
    addresses = _resolve_host(host)
    if not addresses:
        raise ToolExecutionError(f"file_url: host {host!r} resolved to no address")
    return addresses


def _require_fetchable_url(url: str) -> str:
    """Validate scheme/host and refuse any non-public target."""
    text = str(url or "").strip()
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValidationError(
            f"file_url must be an http(s) URL; got {scheme or 'no'} scheme"
        )
    host = parts.hostname
    if not host:
        raise ValidationError(f"file_url has no host: {text!r}")

    addresses = _host_addresses(host)
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            raise ValidationError(f"file_url: unusable address {address!r} for {host!r}")
        reason = _blocked_reason(ip)
        if reason:
            raise ValidationError(
                f"file_url refused: {host} resolves to {ip} ({reason}). Only "
                "public HTTP(S) hosts may be fetched."
            )
    return text


def _build_client() -> httpx.Client:
    """The HTTP client used for URL fetches (redirects handled by us)."""
    return httpx.Client(
        timeout=httpx.Timeout(URL_FETCH_TIMEOUT_SECONDS),
        follow_redirects=False,
    )


def _safe_file_name(name: Optional[str]) -> Optional[str]:
    """A bare, printable file name — never a path."""
    text = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    text = "".join(ch for ch in text if ch.isprintable() and ch not in '"')
    text = text.strip(" .")
    return text[:255] or None


def _name_from_response(response: httpx.Response, url: str) -> Optional[str]:
    disposition = response.headers.get("content-disposition")
    if disposition:
        # Message.get_filename() understands both filename= and the RFC 2231
        # filename*=utf-8''… encoding the stdlib `cgi` module used to parse.
        holder = email.message.Message()
        holder["Content-Disposition"] = disposition
        name = _safe_file_name(holder.get_filename())
        if name:
            return name
    path = urlsplit(url).path or ""
    return _safe_file_name(unquote(path.rsplit("/", 1)[-1]))


def _reported_type(response: httpx.Response) -> Optional[str]:
    value = (response.headers.get("content-type") or "").split(";", 1)[0].strip()
    return value.lower() or None


def fetch_url_bytes(
    url: str, *, max_bytes: int = MAX_REMOTE_ATTACHMENT_BYTES
) -> RemoteBlob:
    """Download ``url`` into memory, refusing non-public hosts and oversize bodies."""
    target = _require_fetchable_url(url)
    client = _build_client()
    try:
        for _ in range(MAX_URL_REDIRECTS + 1):
            with client.stream("GET", target) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ToolExecutionError(
                            f"file_url {target}: HTTP {response.status_code} "
                            "redirect without a Location header"
                        )
                    target = _require_fetchable_url(
                        str(httpx.URL(target).join(location))
                    )
                    continue
                if response.status_code >= 400:
                    raise ToolExecutionError(
                        f"file_url {target}: HTTP {response.status_code} "
                        f"{response.reason_phrase}".strip()
                    )
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise ToolExecutionError(
                        f"file_url {target}: {int(declared)} bytes exceeds the "
                        f"{max_bytes} byte limit"
                    )
                chunks: List[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ToolExecutionError(
                            f"file_url {target}: body exceeds the {max_bytes} "
                            "byte limit"
                        )
                    chunks.append(chunk)
                if not total:
                    raise ToolExecutionError(f"file_url {target}: empty body")
                return RemoteBlob(
                    content=b"".join(chunks),
                    file_name=_name_from_response(response, target),
                    content_type=_reported_type(response),
                )
        raise ToolExecutionError(
            f"file_url {url}: more than {MAX_URL_REDIRECTS} redirects"
        )
    except httpx.HTTPError as exc:
        raise ToolExecutionError(f"file_url {target}: fetch failed ({exc})") from exc
    finally:
        client.close()


# --------------------------------------------------------------------------
# storage_file_id
# --------------------------------------------------------------------------
def fetch_storage_bytes(
    resolve_client: Callable[[], SmarterMailClient],
    file_id: str,
    *,
    max_bytes: int = MAX_REMOTE_ATTACHMENT_BYTES,
) -> RemoteBlob:
    """Download one personal file-storage file (blocking; run off the loop)."""
    client = resolve_client()
    try:
        content, name = download_storage_file(client, file_id)
    except SmarterMailError as exc:
        raise ToolExecutionError(f"storage_file_id {file_id}: {exc}") from exc
    if not content:
        raise ToolExecutionError(f"storage_file_id {file_id}: file is empty")
    if len(content) > max_bytes:
        raise ToolExecutionError(
            f"storage_file_id {file_id}: {len(content)} bytes exceeds the "
            f"{max_bytes} byte limit"
        )
    return RemoteBlob(content=content, file_name=_safe_file_name(name))


# --------------------------------------------------------------------------
# one entry point for all outbound files and add_attachment implementations
# --------------------------------------------------------------------------
async def load_attachment_content(
    kwargs: Dict[str, Any],
    *,
    storage_client: Optional[Callable[[], SmarterMailClient]] = None,
    storage_unavailable: Optional[str] = None,
    max_bytes: int = MAX_REMOTE_ATTACHMENT_BYTES,
) -> LoadedAttachment:
    """Resolve the caller's single content source into bytes and a file name.

    ``storage_client`` supplies the SmarterMail client lazily (resolving one
    authenticates), and is ``None`` for mailboxes that have no personal file
    storage — then ``storage_file_id`` fails with ``storage_unavailable``.
    """
    kwargs = normalize_source_aliases(kwargs)
    source = selected_source(kwargs)
    override = _safe_file_name(kwargs.get("file_name"))

    if source == "file_path":
        path = Path(str(kwargs["file_path"]))
        await asyncio.to_thread(attachment_staging.check_staged_path, path)
        if not await asyncio.to_thread(path.is_file):
            raise ValidationError(
                f"File not found on the Email MCP server: {path}. {REMOTE_AGENT_POINTER}",
                hint=REMOTE_AGENT_HINT,
            )
        content = await asyncio.to_thread(path.read_bytes)
        return LoadedAttachment(
            content=content,
            file_name=override or path.name,
            source=source,
            source_detail=str(path),
        )

    if source == "file_content":
        if not override:
            raise ValidationError(FILE_NAME_REQUIRED)
        return LoadedAttachment(
            content=decode_base64_payload(kwargs["file_content"]),
            file_name=override,
            source=source,
        )

    if source == "file_url":
        url = str(kwargs["file_url"]).strip()
        blob = await asyncio.to_thread(fetch_url_bytes, url, max_bytes=max_bytes)
        name = override or blob.file_name
        if not name:
            raise ValidationError(
                f"file_url {url}: no file name in Content-Disposition or the "
                "URL path; pass file_name"
            )
        return LoadedAttachment(
            content=blob.content,
            file_name=name,
            source=source,
            remote_content_type=blob.content_type,
            source_detail=url,
        )

    file_id = str(kwargs["storage_file_id"]).strip()
    if storage_client is None:
        raise ValidationError(
            f"storage_file_id: {storage_unavailable or STORAGE_UNAVAILABLE_HINT}"
        )
    blob = await asyncio.to_thread(
        fetch_storage_bytes, storage_client, file_id, max_bytes=max_bytes
    )
    return LoadedAttachment(
        content=blob.content,
        file_name=override or blob.file_name or f"{file_id}.bin",
        source=source,
        source_detail=file_id,
    )
