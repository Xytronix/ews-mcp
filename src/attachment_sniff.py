"""Magic-byte sniffing for mislabeled email attachments."""

from __future__ import annotations

import io
import zipfile
from typing import Optional

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