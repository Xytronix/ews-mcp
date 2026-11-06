"""Magic-byte sniffing for mislabeled email attachments."""

from __future__ import annotations

import base64
import email
import io
import re
import zipfile
from dataclasses import dataclass, replace
from email.parser import BytesHeaderParser
from email.policy import compat32
from typing import Iterator, List, Optional, Tuple

# Extensions that are too generic to trust without sniffing.
COMPOUND_EXTENSIONS = ("tar.gz", "tar.bz2", "tar.xz")


def extension_from_filename(filename: str) -> str:
    """Best-effort extension from attachment filename."""
    lower = (filename or "").lower().strip()
    for compound in COMPOUND_EXTENSIONS:
        if lower.endswith(f".{compound}"):
            return compound
    if "." in lower:
        return lower.rsplit(".", 1)[-1]
    return ""


GENERIC_EXTENSIONS = frozenset({
    "", "bin", "dat", "octet-stream", "unknown", "file", "attachment",
    "download", "tmp", "bak", "part",
})

OFFICE_OPENXML_MARKERS = (
    "[Content_Types].xml",
    "word/",
    "xl/",
    "ppt/",
)


def sniff_attachment_type(content: bytes, declared_ext: str) -> str:
    """Return the best extension for extraction (declared or sniffed)."""
    ext = (declared_ext or "").lower().lstrip(".")
    if ext and ext not in GENERIC_EXTENSIONS:
        return ext
    if not content:
        return ext

    head = content[:16]
    text_head = content[:4096].decode("utf-8", errors="ignore").lstrip()

    if content[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if content.startswith(b"AC10"):
        return "dwg"
    if b"SECTION" in content[:512] and b"\x00" not in content[:128]:
        return "dxf"
    if content.startswith(b"%PDF"):
        return "pdf"
    if content.startswith(b"{\rtf"):
        return "rtf"
    if text_head.startswith("BEGIN:VCALENDAR"):
        return "ics"
    if text_head.startswith("BEGIN:VCARD"):
        return "vcf"
    if text_head.upper().startswith("OFXHEADER") or "<OFX>" in text_head.upper():
        return "qfx"
    if content.startswith(b"\x1f\x8b"):
        return "tgz"
    if content.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if content.startswith(b"Rar!"):
        return "rar"
    if content.startswith(b"PK\x03\x04"):
        inner = _sniff_zip_kind(content)
        if inner:
            return inner
        return "zip"
    if content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return _sniff_ole_kind(content)
    if text_head.startswith("From:") or "MIME-Version:" in text_head[:500]:
        return "eml"
    if "-----BEGIN PGP SIGNED MESSAGE-----" in text_head:
        return "asc"
    if "-----BEGIN" in text_head and "CERTIFICATE" in text_head:
        return "pem"
    if text_head.startswith("<?xml") or text_head.startswith("<"):
        lowered = text_head.lower()
        if "<kml" in lowered or "opengis.net/kml" in lowered:
            return "kml"
        if "<gpx" in lowered:
            return "gpx"
        if "fixeddocumentsequence" in lowered or "fixedpage" in lowered:
            return "oxps"
    if _looks_like_mt940(text_head):
        return "mt940"
    if content[:4] == b"PAR1":
        return "parquet"
    if content[:3] == b"Obj" and b"avro" in content[:64]:
        return "avro"

    return ext


def _sniff_zip_kind(content: bytes) -> Optional[str]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = [n.lower().replace("\\", "/") for n in zf.namelist()]
            joined = " ".join(names)
            if "pass.json" in names:
                return "pkpass"
            if "meta-inf/container.xml" in names and any(
                n.endswith(".opf") for n in names
            ):
                return "epub"
            if "fixeddocumentsequence.fdseq" in names or any(
                n.endswith(".fpage") for n in names
            ):
                return "oxps"
            if "index.apxl" in joined or "index/slide" in joined:
                if any("numbers" in n for n in names):
                    return "numbers"
                return "pages"
            if any(n.endswith(".key") or "/metadata.plist" in n for n in names):
                if "index.apxl" in joined or "quicklook" in joined:
                    return "key"
            if any(marker in joined for marker in OFFICE_OPENXML_MARKERS):
                if "word/" in joined:
                    return "docx"
                if "xl/" in joined:
                    return "xlsx"
                if "ppt/" in joined:
                    return "pptx"
            if "doc.kml" in names:
                return "kmz"
    except Exception:
        return None
    return None


def _sniff_ole_kind(content: bytes) -> str:
    try:
        import olefile
        if olefile.isOleFile(io.BytesIO(content)):
            ole = olefile.OleFileIO(io.BytesIO(content))
            streams = ["/".join(s) for s in ole.listdir()]
            ole.close()
            joined = " ".join(streams).lower()
            if "onenote" in joined or "one" in joined or "notes" in joined:
                return "one"
            if "worddocument" in joined:
                return "doc"
            if "workbook" in joined:
                return "xls"
            if "__substg1.0" in joined:
                return "msg"
    except Exception:
        pass
    return "doc"


def _looks_like_mt940(text: str) -> bool:
    markers = (":20:", ":25:", ":28C:", ":60F:", ":61:", ":86:")
    hits = sum(1 for m in markers if m in text)
    return hits >= 3


# --- S/MIME / CMS -------------------------------------------------------
# ContentInfo content types (RFC 5652, RFC 5083), keyed by the DER body of
# the OID so detection needs no ASN.1 library and no key material.
_CMS_CONTENT_TYPES = {
    bytes.fromhex("2a864886f70d010702"): ("signedData", "1.2.840.113549.1.7.2"),
    bytes.fromhex("2a864886f70d010703"): ("envelopedData", "1.2.840.113549.1.7.3"),
    bytes.fromhex("2a864886f70d0109100117"): (
        "authEnvelopedData", "1.2.840.113549.1.9.16.1.23",
    ),
}

ENCRYPTED_CMS_KINDS = frozenset({"envelopedData", "authEnvelopedData"})

_PEM_CMS = re.compile(
    rb"-----BEGIN (PKCS7|CMS|PKCS #7 SIGNED DATA)-----(.*?)-----END \1-----",
    re.DOTALL,
)
_BASE64_TEXT = re.compile(rb"[A-Za-z0-9+/=\s]+")
_MIME_HEADER_START = re.compile(rb"[A-Za-z][A-Za-z0-9-]*:")
_HEADER_END = re.compile(rb"\r?\n\r?\n")
_LINE_END = re.compile(rb"\r?\n")
_PKCS7_MIME_TYPES = frozenset({
    "application/pkcs7-mime", "application/x-pkcs7-mime",
    "application/pkcs7-signature", "application/x-pkcs7-signature",
})
# Real CMS nests a handful of levels. The caps only stop hostile input.
_BER_MAX_DEPTH = 32
_MIME_MAX_DEPTH = 16


@dataclass(frozen=True)
class CmsInfo:
    """Shape of a CMS / PKCS#7 attachment."""

    kind: str  # signedData, envelopedData or authEnvelopedData
    oid: str
    der: bytes  # the ContentInfo, unwrapped from PEM, base64 or MIME
    detached: bool  # SignedData without encapsulated content
    # Exact signed part (CRLF canonical) when the attachment is itself a
    # multipart/signed entity, which is how Exchange stores clear-signed mail.
    signed_part: Optional[bytes] = None


def sniff_cms(content: bytes) -> Optional[CmsInfo]:
    """Identify a CMS ContentInfo by its content-type OID, else None."""
    if not content:
        return None
    try:
        if content[0] == 0x30:
            return _cms_info(content)
        pem = _PEM_CMS.search(content)
        if pem:
            return _cms_info(_b64decode(pem.group(2)))
        if _MIME_HEADER_START.match(content):
            return _cms_from_mime(content)
        if _BASE64_TEXT.fullmatch(content):
            return _cms_info(_b64decode(content))
    except (ValueError, IndexError, RecursionError):
        # Malformed or truncated input simply is not CMS we can read.
        return None
    return None


def needs_detached_content(content: bytes) -> bool:
    """True for a SignedData whose signed content is not in ``content``."""
    info = sniff_cms(content)
    return (
        info is not None
        and info.kind == "signedData"
        and info.detached
        and info.signed_part is None
    )


def find_signed_part(signature: bytes, raw_mime: Optional[bytes]) -> Optional[bytes]:
    """The exact part of ``raw_mime`` that the detached ``signature`` signs.

    A multipart/signed signature covers the first body part byte for byte,
    headers included, in CRLF canonical form (RFC 1847, RFC 8551 3.5). A
    rendered or re-serialized body never verifies, so this reads the raw MIME
    and returns None when no multipart/signed entity carries ``signature``.
    """
    wanted = sniff_cms(signature)
    if wanted is None or not raw_mime:
        return None
    for signed, signature_part in _signed_entities(raw_mime, 0):
        found = sniff_cms(_decoded_part(signature_part))
        if found is not None and found.der == wanted.der:
            return _LINE_END.sub(b"\r\n", signed)
    return None


def _b64decode(data: bytes) -> bytes:
    return base64.b64decode(re.sub(rb"\s+", b"", data), validate=True)


def _ber_element(
    data: bytes, pos: int, limit: int, depth: int = 0
) -> Tuple[int, int, int, int]:
    """(tag, content start, content end, next offset) of one BER element.

    S/MIME clients emit indefinite lengths, so plain DER parsing is not
    enough. Raises ValueError on malformed or truncated input.
    """
    if depth > _BER_MAX_DEPTH or pos + 2 > limit:
        raise ValueError("malformed BER")
    tag = data[pos]
    if tag & 0x1F == 0x1F:
        raise ValueError("unexpected high tag number")
    first = data[pos + 1]
    start = pos + 2
    if first == 0x80:
        if not tag & 0x20:
            raise ValueError("indefinite length on a primitive element")
        cursor = start
        while True:
            if cursor + 2 > limit:
                raise ValueError("missing end-of-contents")
            if data[cursor] == 0 and data[cursor + 1] == 0:
                return tag, start, cursor, cursor + 2
            cursor = _ber_element(data, cursor, limit, depth + 1)[3]
    if first & 0x80:
        count = first & 0x7F
        if count > 4 or start + count > limit:
            raise ValueError("unsupported length")
        length = int.from_bytes(data[start:start + count], "big")
        start += count
    else:
        length = first
    end = start + length
    if end > limit:
        raise ValueError("truncated element")
    return tag, start, end, end


def _ber_children(data: bytes, start: int, end: int) -> List[Tuple[int, int, int]]:
    children = []
    pos = start
    while pos < end:
        tag, child_start, child_end, pos = _ber_element(data, pos, end)
        children.append((tag, child_start, child_end))
    return children


def _cms_info(der: bytes) -> Optional[CmsInfo]:
    if not der or der[0] != 0x30:
        return None
    _tag, start, end, total = _ber_element(der, 0, len(der))
    # ContentInfo ::= SEQUENCE { contentType OID, content [0] EXPLICIT ANY }
    fields = _ber_children(der, start, end)
    if len(fields) < 2 or fields[0][0] != 0x06 or fields[1][0] != 0xA0:
        return None
    known = _CMS_CONTENT_TYPES.get(der[fields[0][1]:fields[0][2]])
    if known is None:
        return None
    kind, oid = known
    detached = False
    if kind == "signedData":
        explicit = _ber_children(der, fields[1][1], fields[1][2])
        # SignedData ::= SEQUENCE { version, digestAlgorithms,
        #   encapContentInfo SEQUENCE { eContentType, eContent [0] OPTIONAL }, ... }
        signed_data = _ber_children(der, explicit[0][1], explicit[0][2])
        if signed_data[2][0] != 0x30:
            raise ValueError("malformed SignedData")
        encap = _ber_children(der, signed_data[2][1], signed_data[2][2])
        detached = not any(tag == 0xA0 for tag, _start, _end in encap[1:])
    return CmsInfo(kind=kind, oid=oid, der=der[:total], detached=detached)


def _cms_from_mime(content: bytes) -> Optional[CmsInfo]:
    head, body = _split_entity(content)
    headers = BytesHeaderParser(policy=compat32).parsebytes(head)
    content_type = headers.get_content_type()
    if content_type == "multipart/signed":
        parts = _multipart_bodies(body, headers.get_boundary())
        if len(parts) < 2:
            return None
        info = _cms_info(_decoded_part(parts[1]))
        if info is None or info.kind != "signedData":
            return None
        return replace(info, signed_part=_LINE_END.sub(b"\r\n", parts[0]))
    if content_type in _PKCS7_MIME_TYPES:
        return _cms_info(_decoded_part(content))
    return None


def _split_entity(raw: bytes) -> Tuple[bytes, bytes]:
    """Raw header block and body of one MIME entity."""
    if raw.startswith(b"\r\n"):
        return b"", raw[2:]
    if raw.startswith(b"\n"):
        return b"", raw[1:]
    match = _HEADER_END.search(raw)
    if match is None:
        return raw, b""
    return raw[:match.start()], raw[match.end():]


def _multipart_bodies(body: bytes, boundary: Optional[str]) -> List[bytes]:
    """Exact bytes of each body part, delimiters excluded.

    The line break before a delimiter belongs to the delimiter, not to the
    part before it (RFC 2046 5.1.1).
    """
    if not boundary:
        return []
    delimiter = re.compile(
        rb"(?:\A|\r?\n)--"
        + re.escape(boundary.encode("utf-8", "surrogateescape"))
        + rb"(--)?[ \t]*(?:\r?\n|\Z)"
    )
    parts: List[bytes] = []
    start: Optional[int] = None
    for match in delimiter.finditer(body):
        if start is not None:
            parts.append(body[start:match.start()])
        if match.group(1):
            return parts
        start = match.end()
    if start is not None:
        parts.append(body[start:])
    return parts


def _signed_entities(raw: bytes, depth: int) -> Iterator[Tuple[bytes, bytes]]:
    """(signed part, signature part) of each multipart/signed entity in ``raw``."""
    if depth > _MIME_MAX_DEPTH:
        return
    head, body = _split_entity(raw)
    headers = BytesHeaderParser(policy=compat32).parsebytes(head)
    if headers.get_content_maintype() != "multipart":
        return
    parts = _multipart_bodies(body, headers.get_boundary())
    if headers.get_content_subtype() == "signed":
        if len(parts) >= 2:
            yield parts[0], parts[1]
        # The signature part is a leaf. Only the signed part can nest.
        parts = parts[:1]
    for part in parts:
        yield from _signed_entities(part, depth + 1)


def _decoded_part(raw: bytes) -> bytes:
    """Transfer-decoded body of one raw MIME part."""
    part = email.message_from_bytes(raw, policy=compat32)
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else b""