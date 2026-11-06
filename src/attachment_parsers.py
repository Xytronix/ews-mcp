"""Attachment text extraction beyond Liteparse document parsing."""

from __future__ import annotations

import io
import json
import logging
import os
import plistlib
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from email import policy
from email.parser import BytesParser
from typing import List, Optional, Tuple

from .attachment_sniff import sniff_attachment_type
from .document_parser import parse_document_bytes, supports_liteparse

_log = logging.getLogger(__name__)

ARCHIVE_MAX_FILES = 25
ARCHIVE_MAX_FILE_BYTES = 15 * 1024 * 1024
NESTED_MAX_EXTRACT = 8
NESTED_MAX_DEPTH = 2

TEXT_EXTENSIONS = frozenset({
    "txt", "log", "json", "xml", "md", "csv", "tsv", "yaml", "yml", "svg",
    "ini", "env", "properties", "cnf", "conf", "tex", "bib", "iif", "iff",
    "ndjson", "jsonl",
})

STRUCTURED_EXTENSIONS = frozenset({
    "ics", "vcs", "vcf", "qfx", "ofx", "mt940", "sta", "940",
})

IWORK_EXTENSIONS = frozenset({"pages", "numbers", "key"})

ARCHIVE_EXTENSIONS = frozenset({"zip", "7z", "rar"})

TAR_EXTENSIONS = frozenset({
    "tar", "tgz", "tbz2", "tbz", "txz",
    "tar.gz", "tar.bz2", "tar.xz",
})

BOOK_EXTENSIONS = frozenset({"epub"})

WALLET_EXTENSIONS = frozenset({"pkpass"})

XPS_EXTENSIONS = frozenset({"xps", "oxps"})

CERT_EXTENSIONS = frozenset({"pem", "crt", "cer", "der"})

SIGNATURE_EXTENSIONS = frozenset({"p7s", "p7m", "asc"})

CAD_EXTENSIONS = frozenset({"dwg", "dxf"})

GEO_EXTENSIONS = frozenset({"kml", "gpx", "kmz"})

DATA_EXTENSIONS = frozenset({"parquet", "avro"})

ONENOTE_EXTENSIONS = frozenset({"one"})

DATABASE_EXTENSIONS = frozenset({"mdb", "accdb"})

EMAIL_ALIASES = frozenset({"rfc822", "mime"})

CONFIG_TOML_EXTENSIONS = frozenset({"toml"})

# Apple iWork bundles are ZIPs; preview assets vary by export version.
_IWORK_PREVIEW_SUFFIXES = (
    "preview.pdf",
    "quicklook/preview.pdf",
    "preview.jpg",
    "preview-micro.jpg",
    "preview-web.jpg",
    "quicklook/thumbnail.jpg",
    "quicklook/preview.jpg",
)

EMAIL_CONTAINER_EXTENSIONS = frozenset({"msg", "eml"})

WEB_EXTENSIONS = frozenset({"html", "htm", "mht", "mhtml"})


def supported_extensions_summary() -> str:
    return (
        "Office/PDF/ODF/images/HEIC, ICS/VCS, VCF, QFX/OFX/MT940, Apple iWork, "
        "ZIP/7Z/TAR, EPUB, PKPass, XPS, certs/PGP/S-MIME, DWG/DXF, KML/GPX, Parquet/Avro, "
        "OneNote (metadata), RTF, MSG/EML/MIME, HTML/MHTML, config/text formats; magic-byte sniffing"
    )


def extract_attachment_text(
    content: bytes,
    file_ext: str,
    *,
    max_pages: int = 50,
    extract_tables: bool = False,
    password: Optional[str] = None,
    signed_content: Optional[bytes] = None,
    depth: int = 0,
) -> str:
    """Dispatch attachment bytes to the appropriate extractor."""
    declared = (file_ext or "").lower().lstrip(".")
    ext = sniff_attachment_type(content, declared)
    if not content:
        raise RuntimeError("Attachment content is empty")

    if ext in DATABASE_EXTENSIONS:
        from .attachment_formats_extra import parse_database_binary
        return parse_database_binary(ext)

    if ext in ONENOTE_EXTENSIONS:
        from .attachment_formats_extra import parse_onenote
        return parse_onenote(content)

    if ext in DATA_EXTENSIONS:
        from .attachment_formats_extra import parse_avro, parse_parquet
        return parse_parquet(content) if ext == "parquet" else parse_avro(content)

    if ext in GEO_EXTENSIONS:
        from .attachment_formats_extra import parse_gpx, parse_kml, parse_kmz
        if ext == "gpx":
            return parse_gpx(content)
        if ext == "kmz":
            return parse_kmz(content, _html_to_markdown)
        return parse_kml(content)

    if ext in CAD_EXTENSIONS:
        from .attachment_formats_extra import parse_dwg, parse_dxf
        return parse_dxf(content) if ext == "dxf" else parse_dwg(content)

    if ext in SIGNATURE_EXTENSIONS:
        from .attachment_formats_extra import parse_pgp_clearsign, parse_pkcs7_signature
        if ext == "asc":
            return parse_pgp_clearsign(content)
        return parse_pkcs7_signature(
            content,
            signed_content=signed_content,
            html_to_md=_html_to_markdown,
        )

    if ext in CERT_EXTENSIONS:
        from .attachment_formats_extra import parse_certificate
        return parse_certificate(content, ext)

    if ext in XPS_EXTENSIONS:
        from .attachment_formats_extra import parse_xps
        return parse_xps(content)

    if ext in BOOK_EXTENSIONS:
        from .attachment_formats_extra import parse_epub
        return parse_epub(content, _html_to_markdown)

    if ext in WALLET_EXTENSIONS:
        from .attachment_formats_extra import parse_pkpass
        return parse_pkpass(content)

    if ext in TAR_EXTENSIONS or (declared in TAR_EXTENSIONS):
        from .attachment_formats_extra import parse_tar_archive
        return parse_tar_archive(
            content,
            extract_inner=extract_attachment_text,
            max_pages=max_pages,
            extract_tables=extract_tables,
            password=password,
        )

    if ext in CONFIG_TOML_EXTENSIONS:
        from .attachment_formats_extra import parse_toml_text
        return parse_toml_text(content)

    if ext in EMAIL_ALIASES:
        ext = "eml"

    if supports_liteparse(ext) or ext in ("heic", "heif"):
        return parse_document_bytes(
            content,
            "jpeg" if ext in ("heic", "heif", "jfif") else ext,
            max_pages=max_pages,
            extract_tables=extract_tables,
            password=password,
            source_ext=ext,
        )

    if ext in STRUCTURED_EXTENSIONS:
        if ext in ("ics", "vcs"):
            return parse_calendar_invite(content)
        if ext == "vcf":
            return parse_vcard(content)
        if ext in ("mt940", "sta", "940"):
            from .attachment_formats_extra import parse_mt940
            return parse_mt940(content)
        return parse_financial_exchange(content)

    if ext in IWORK_EXTENSIONS:
        return parse_iwork_bundle(
            content,
            ext,
            max_pages=max_pages,
            extract_tables=extract_tables,
            password=password,
        )

    if ext in ARCHIVE_EXTENSIONS:
        return parse_archive(content, ext, max_pages=max_pages, extract_tables=extract_tables, password=password)

    if ext == "rtf":
        return parse_rtf(content)

    if ext in EMAIL_CONTAINER_EXTENSIONS:
        if ext == "msg":
            return parse_msg(content, depth=depth, max_pages=max_pages, extract_tables=extract_tables, password=password)
        return parse_eml(content, depth=depth, max_pages=max_pages, extract_tables=extract_tables, password=password)

    if ext in WEB_EXTENSIONS:
        return parse_html_attachment(content, ext)

    if ext in TEXT_EXTENSIONS:
        if ext in ("ndjson", "jsonl"):
            from .attachment_formats_extra import parse_ndjson
            return parse_ndjson(content)
        return decode_text(content)

    raise RuntimeError(
        f"Unsupported file type: {ext or '(none)'}. Supported: {supported_extensions_summary()}"
    )


def decode_text(content: bytes) -> str:
    if not content:
        return ""
    if content[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return content.decode("utf-16")
        except UnicodeDecodeError:
            pass
    if content.startswith(b"\xef\xbb\xbf"):
        return content[3:].decode("utf-8", errors="replace")
    return content.decode("utf-8", errors="replace")


def parse_rtf(content: bytes) -> str:
    try:
        from striprtf.striprtf import rtf_to_text
    except ImportError as exc:
        raise RuntimeError("striprtf not installed. Run: pip install striprtf") from exc
    text = rtf_to_text(decode_text(content))
    if not text.strip():
        raise RuntimeError("RTF contained no extractable text")
    return text.strip()


def parse_calendar_invite(content: bytes) -> str:
    try:
        from icalendar import Calendar
    except ImportError as exc:
        raise RuntimeError("icalendar not installed. Run: pip install icalendar") from exc

    raw = decode_text(content)
    cal = Calendar.from_ical(raw.encode("utf-8", errors="replace"))
    events: List[dict] = []

    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        attendees = []
        for key in ("ATTENDEE",):
            for att in component.get(key, []):
                if hasattr(att, "to_ical"):
                    attendees.append(att.to_ical().decode("utf-8", errors="replace"))
                else:
                    attendees.append(str(att))

        def _field(name: str) -> Optional[str]:
            val = component.get(name)
            if val is None:
                return None
            if hasattr(val, "dt"):
                return str(val.dt)
            if hasattr(val, "to_ical"):
                return val.to_ical().decode("utf-8", errors="replace")
            return str(val)

        events.append({
            "summary": _field("SUMMARY"),
            "start": _field("DTSTART"),
            "end": _field("DTEND"),
            "location": _field("LOCATION"),
            "description": (_field("DESCRIPTION") or "")[:4000],
            "organizer": _field("ORGANIZER"),
            "attendees": attendees,
            "uid": _field("UID"),
            "status": _field("STATUS"),
        })

    if not events:
        raise RuntimeError("No calendar events found in ICS/VCS file")

    lines = [f"--- Calendar invite ({len(events)} event(s)) ---"]
    for i, ev in enumerate(events, 1):
        lines.append(f"\n## Event {i}")
        for key, val in ev.items():
            if val is None or val == "" or val == []:
                continue
            if isinstance(val, list):
                lines.append(f"{key}: {', '.join(val)}")
            else:
                lines.append(f"{key}: {val}")
    return "\n".join(lines).strip()


def parse_vcard(content: bytes) -> str:
    try:
        import vobject
    except ImportError as exc:
        raise RuntimeError("vobject not installed. Run: pip install vobject") from exc

    raw = decode_text(content)
    cards = []
    for card in vobject.readComponents(raw):
        if (card.name or "").lower() != "vcard":
            continue
        entry = {
            "name": _vcard_field(card, "fn"),
            "org": _vcard_field(card, "org"),
            "title": _vcard_field(card, "title"),
            "emails": _vcard_multi(card, "email"),
            "phones": _vcard_multi(card, "tel"),
            "urls": _vcard_multi(card, "url"),
        }
        cards.append({k: v for k, v in entry.items() if v})

    if not cards:
        raise RuntimeError("No vCard contacts found")

    return json.dumps({"contacts": cards}, indent=2, ensure_ascii=False)


def parse_financial_exchange(content: bytes) -> str:
    """Parse Quicken QFX / OFX bank export files."""
    raw = decode_text(content)
    if not raw.strip():
        raise RuntimeError("QFX/OFX file is empty")

    # Strip the ASCII OFX header block (OFX 1.x SGML exports).
    body = raw
    if raw.lstrip().upper().startswith("OFXHEADER"):
        parts = re.split(r"(?is)<OFX>|<ofx>", raw, maxsplit=1)
        if len(parts) == 2:
            body = "<OFX>" + parts[1]
        else:
            body = re.sub(r"(?im)^[^\n<]*\n", "", raw, count=20)

    transactions: List[dict] = []
    account_info: dict = {}
    balance: Optional[str] = None

    # OFX 2.x XML
    if "<" in body and re.search(r"(?i)<ofx\b", body):
        try:
            root = ET.fromstring(body.encode("utf-8", errors="replace"))
            for elem in root.iter():
                tag = elem.tag.split("}")[-1].upper() if "}" in elem.tag else elem.tag.upper()
                text = (elem.text or "").strip()
                if tag in ("ACCTID", "BANKID", "BRANCHID", "ACCTTYPE") and text:
                    account_info[tag.lower()] = text
                if tag in ("BALAMT", "LEDGERBAL") and text and balance is None:
                    balance = text
                if tag == "STMTTRN":
                    tx = _ofx_xml_transaction(elem)
                    if tx:
                        transactions.append(tx)
        except ET.ParseError:
            pass

    # OFX 1.x SGML fallback
    if not transactions:
        for block in re.findall(r"(?is)<STMTTRN>(.*?)</STMTTRN>", body):
            tx = _ofx_sgml_transaction(block)
            if tx:
                transactions.append(tx)
        for key, tag in (
            ("acctid", "ACCTID"),
            ("bankid", "BANKID"),
            ("accttype", "ACCTTYPE"),
        ):
            match = re.search(rf"(?is)<{tag}>([^<\n]+)", body)
            if match:
                account_info[key] = match.group(1).strip()
        if balance is None:
            match = re.search(r"(?is)<BALAMT>([^<\n]+)", body)
            if match:
                balance = match.group(1).strip()

    if not transactions and not account_info:
        raise RuntimeError("No parseable transactions or account data in QFX/OFX file")

    lines = ["--- Financial export (QFX/OFX) ---"]
    if account_info:
        lines.append("Account:")
        for key, val in account_info.items():
            lines.append(f"  {key}: {val}")
    if balance is not None:
        lines.append(f"balance: {balance}")
    if transactions:
        lines.append(f"\nTransactions ({len(transactions)} total, showing up to 150):")
        for i, tx in enumerate(transactions[:150], 1):
            lines.append(f"\n## Transaction {i}")
            for key, val in tx.items():
                if val:
                    lines.append(f"{key}: {val}")
        if len(transactions) > 150:
            lines.append(f"\n... ({len(transactions) - 150} more transactions omitted)")
    return "\n".join(lines).strip()


def _ofx_tag_value(block: str, tag: str) -> Optional[str]:
    match = re.search(rf"(?is)<{tag}>([^<\n]+)", block)
    return match.group(1).strip() if match else None


def _ofx_sgml_transaction(block: str) -> Optional[dict]:
    tx = {
        "date": _ofx_tag_value(block, "DTPOSTED") or _ofx_tag_value(block, "DTUSER"),
        "amount": _ofx_tag_value(block, "TRNAMT"),
        "type": _ofx_tag_value(block, "TRNTYPE"),
        "name": _ofx_tag_value(block, "NAME"),
        "memo": _ofx_tag_value(block, "MEMO"),
        "fitid": _ofx_tag_value(block, "FITID"),
    }
    return tx if any(tx.values()) else None


def _ofx_xml_transaction(elem: ET.Element) -> Optional[dict]:
    def child(tag: str) -> Optional[str]:
        for child_elem in elem:
            ctag = child_elem.tag.split("}")[-1].upper() if "}" in child_elem.tag else child_elem.tag.upper()
            if ctag == tag:
                return (child_elem.text or "").strip()
        return None

    tx = {
        "date": child("DTPOSTED") or child("DTUSER"),
        "amount": child("TRNAMT"),
        "type": child("TRNTYPE"),
        "name": child("NAME"),
        "memo": child("MEMO"),
        "fitid": child("FITID"),
    }
    return tx if any(tx.values()) else None


def parse_iwork_bundle(
    content: bytes,
    ext: str,
    *,
    max_pages: int,
    extract_tables: bool,
    password: Optional[str],
) -> str:
    """Parse Apple Pages (.pages) or Numbers (.numbers) iWork bundles."""
    entries = _read_zip_entries(content)
    if not entries:
        raise RuntimeError(
            f"Could not read {ext.upper()} bundle as ZIP — file may be corrupt or an unsupported export"
        )

    by_path = {name.lower(): (name, data) for name, data in entries}
    lines = [f"--- Apple {ext.upper()} bundle ({len(entries)} file(s)) ---"]

    preview_data: Optional[Tuple[str, bytes]] = None
    for suffix in _IWORK_PREVIEW_SUFFIXES:
        if suffix in by_path:
            preview_data = by_path[suffix]
            break
    if preview_data is None:
        for path, item in by_path.items():
            if path.endswith(".pdf") and "preview" in path:
                preview_data = item
                break
    if preview_data is None:
        for path, item in by_path:
            if path.endswith((".jpg", ".jpeg", ".png")) and (
                "preview" in path or "quicklook" in path or "thumbnail" in path
            ):
                preview_data = item
                break

    if preview_data:
        name, data = preview_data
        inner_ext = name.rsplit(".", 1)[-1].lower()
        lines.append(f"\n### Embedded preview: {name}")
        try:
            preview_text = extract_attachment_text(
                data,
                inner_ext,
                max_pages=min(max_pages, 20),
                extract_tables=extract_tables,
                password=password,
                depth=1,
            )
            lines.append(preview_text[:15000])
        except Exception as exc:
            lines.append(f"[preview parse failed: {exc}]")

    metadata_lines: List[str] = []
    for path, (orig_name, data) in by_path.items():
        if not (
            path.endswith(".plist")
            or path.endswith(".xml")
            or path.endswith("metadata.properties")
        ):
            continue
        if "metadata" not in path and not path.endswith(".plist"):
            continue
        try:
            if path.endswith(".plist"):
                parsed = plistlib.loads(data)
                metadata_lines.append(f"{orig_name}: {json.dumps(parsed, default=str)[:2000]}")
            else:
                text = decode_text(data)
                if text.strip():
                    metadata_lines.append(f"{orig_name}:\n{text[:2000]}")
        except Exception as exc:
            metadata_lines.append(f"{orig_name}: [metadata unreadable: {exc}]")

    if metadata_lines:
        lines.append("\n### Bundle metadata")
        lines.extend(metadata_lines[:5])

    if len(lines) == 1:
        names = sorted(orig for orig, _ in entries)[:20]
        lines.append("\nNo embedded preview found. Bundle contents:")
        for name in names:
            lines.append(f"  - {name}")
        lines.append(
            "\nTip: re-export from Pages/Numbers as PDF or DOCX for full text extraction."
        )

    return "\n".join(lines).strip()


def _vcard_field(card, name: str) -> Optional[str]:
    if not hasattr(card, name):
        return None
    val = getattr(card, name)
    return str(val.value) if val else None


def _vcard_multi(card, name: str) -> List[str]:
    if not hasattr(card, name):
        return []
    contents = getattr(card, name)
    if not isinstance(contents, list):
        contents = [contents]
    return [str(c.value) for c in contents if c]


def parse_archive(
    content: bytes,
    ext: str,
    *,
    max_pages: int,
    extract_tables: bool,
    password: Optional[str],
) -> str:
    entries: List[Tuple[str, bytes]] = []
    if ext == "zip":
        entries = _read_zip_entries(content, password=password)
    elif ext == "7z":
        entries = _read_7z_entries(content, password=password)
    elif ext == "rar":
        entries = _read_rar_entries(content, password=password)
    else:
        raise RuntimeError(f"Unsupported archive type: {ext}")

    if not entries:
        raise RuntimeError("Archive is empty")

    lines = [f"--- Archive ({len(entries)} file(s)) ---"]
    parsed = 0
    for name, data in entries:
        lines.append(f"\n### {name} ({len(data)} bytes)")
        inner_ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if _is_parseable_inner(inner_ext):
            try:
                inner_text = extract_attachment_text(
                    data,
                    inner_ext,
                    max_pages=min(max_pages, 20),
                    extract_tables=extract_tables,
                    password=password,
                    depth=1,
                )
                lines.append(inner_text[:12000])
                parsed += 1
            except Exception as exc:
                lines.append(f"[could not parse: {exc}]")
        else:
            lines.append("[skipped — unsupported inner type]")
        if parsed >= ARCHIVE_MAX_FILES:
            lines.append("\n... (truncated: archive file parse limit reached)")
            break
    return "\n".join(lines).strip()


def _is_parseable_inner(ext: str) -> bool:
    return ext in _all_supported_extensions()


def _all_supported_extensions() -> frozenset:
    groups = (
        TEXT_EXTENSIONS,
        STRUCTURED_EXTENSIONS,
        IWORK_EXTENSIONS,
        ARCHIVE_EXTENSIONS,
        TAR_EXTENSIONS,
        BOOK_EXTENSIONS,
        WALLET_EXTENSIONS,
        XPS_EXTENSIONS,
        CERT_EXTENSIONS,
        SIGNATURE_EXTENSIONS,
        GEO_EXTENSIONS,
        CAD_EXTENSIONS,
        DATA_EXTENSIONS,
        ONENOTE_EXTENSIONS,
        DATABASE_EXTENSIONS,
        EMAIL_CONTAINER_EXTENSIONS,
        EMAIL_ALIASES,
        WEB_EXTENSIONS,
        CONFIG_TOML_EXTENSIONS,
        frozenset({
            "rtf", "heic", "heif",
            "pdf", "doc", "docx", "docm", "xls", "xlsx", "xlsm",
            "ppt", "pptx", "pptm", "odt", "ods", "odp",
            "png", "jpg", "jpeg", "jfif", "tif", "tiff", "webp", "bmp", "gif",
        }),
    )
    out: set[str] = set()
    for group in groups:
        out |= set(group)
    return frozenset(out)


def _read_zip_entries(content: bytes, *, password: Optional[str] = None) -> List[Tuple[str, bytes]]:
    out: List[Tuple[str, bytes]] = []
    pwd = password.encode("utf-8") if password else None
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if len(out) >= ARCHIVE_MAX_FILES:
                break
            if info.file_size > ARCHIVE_MAX_FILE_BYTES:
                _log.warning("Skipping large zip member %s (%s bytes)", info.filename, info.file_size)
                continue
            name = info.filename.replace("\\", "/").lstrip("/")
            if ".." in name.split("/"):
                continue
            try:
                data = zf.read(info, pwd=pwd)
            except RuntimeError as exc:
                if "password" in str(exc).lower() or "encrypted" in str(exc).lower():
                    raise RuntimeError(
                        "ZIP is password-protected — pass the `password` parameter"
                    ) from exc
                _log.debug("Skipped zip member %s: %s", name, exc)
                continue
            except Exception as exc:
                _log.debug("Skipped zip member %s: %s", name, exc)
                continue
            out.append((name, data))
    return out


def _read_7z_entries(content: bytes, *, password: Optional[str] = None) -> List[Tuple[str, bytes]]:
    try:
        import py7zr
        from py7zr.io import BufferOverflow, BytesIOFactory
    except ImportError as exc:
        raise RuntimeError("py7zr not installed. Run: pip install py7zr") from exc

    out: List[Tuple[str, bytes]] = []
    try:
        archive = py7zr.SevenZipFile(
            io.BytesIO(content), mode="r", password=password
        )
    except Exception as exc:
        msg = str(exc).lower()
        if "password" in msg or "encrypted" in msg or "protected" in msg:
            if password:
                raise RuntimeError(f"7z open failed (wrong password?): {exc}") from exc
            raise RuntimeError(
                "7z is password-protected — pass the `password` parameter"
            ) from exc
        if password:
            raise RuntimeError(f"7z open failed (wrong password?): {exc}") from exc
        raise
    with archive:
        # Pick members from the archive header FIRST. py7zr decompresses
        # whatever it is told to extract, so the file-count and per-file-size
        # limits have to be applied to the declared sizes before any data is
        # materialised — otherwise a 7z bomb expands in memory before the
        # limits are ever consulted (same order as the zip path above).
        targets: List[Tuple[str, str]] = []
        for info in archive.list():
            if len(targets) >= ARCHIVE_MAX_FILES:
                break
            if info.is_directory:
                continue
            declared = info.uncompressed or 0
            if declared > ARCHIVE_MAX_FILE_BYTES:
                _log.warning(
                    "Skipping large 7z member %s (%s bytes)", info.filename, declared
                )
                continue
            safe_name = str(info.filename).replace("\\", "/").lstrip("/")
            if ".." in safe_name.split("/"):
                continue
            targets.append((info.filename, safe_name))
        if not targets:
            return out
        # BytesIOFactory caps each member at the same byte limit, so a lying
        # header cannot push more than that into memory per file.
        factory = BytesIOFactory(limit=ARCHIVE_MAX_FILE_BYTES)
        try:
            archive.extract(targets=[native for native, _ in targets], factory=factory)
        except BufferOverflow as exc:
            # Header under-reported a member; the factory stopped it at the cap.
            raise RuntimeError(
                f"7z member exceeds the {ARCHIVE_MAX_FILE_BYTES} byte extraction limit"
            ) from exc
        except Exception as exc:
            # Encrypted payload with a plaintext header: the password only
            # fails here, not at open time.
            msg = str(exc).lower()
            if password:
                raise RuntimeError(f"7z extract failed (wrong password?): {exc}") from exc
            if "password" in msg or "encrypted" in msg or "protected" in msg:
                raise RuntimeError(
                    "7z is password-protected — pass the `password` parameter"
                ) from exc
            raise
        for native, safe_name in targets:
            try:
                bio = factory.get(native)
            except KeyError:
                continue
            try:
                bio.seek(0)
                data = bio.read()
            except Exception as exc:
                _log.debug("Skipped 7z member %s: %s", native, exc)
                continue
            if len(data) > ARCHIVE_MAX_FILE_BYTES:
                continue
            out.append((safe_name, data))
    return out


def _read_rar_entries(content: bytes, *, password: Optional[str] = None) -> List[Tuple[str, bytes]]:
    try:
        import rarfile
    except ImportError as exc:
        raise RuntimeError("rarfile not installed. Run: pip install rarfile") from exc

    out: List[Tuple[str, bytes]] = []
    with rarfile.RarFile(io.BytesIO(content)) as rf:
        rf.setpassword(password)
        for info in rf.infolist():
            if info.is_dir():
                continue
            if len(out) >= ARCHIVE_MAX_FILES:
                break
            if info.file_size > ARCHIVE_MAX_FILE_BYTES:
                continue
            name = info.filename.replace("\\", "/").lstrip("/")
            if ".." in name.split("/"):
                continue
            try:
                data = rf.read(info)
            except Exception as exc:
                if "password" in str(exc).lower():
                    raise RuntimeError(
                        "RAR is password-protected — pass the `password` parameter"
                    ) from exc
                _log.debug("Skipped rar member %s: %s", name, exc)
                continue
            out.append((name, data))
    return out


def parse_html_attachment(content: bytes, ext: str) -> str:
    if ext in ("mht", "mhtml"):
        html = _extract_html_from_mhtml(content)
        if html:
            return _html_to_markdown(html)
    return _html_to_markdown(decode_text(content))


def _extract_html_from_mhtml(content: bytes) -> str:
    try:
        msg = BytesParser(policy=policy.default).parsebytes(content)
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    return part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode("utf-8", errors="replace")
    except Exception:
        pass
    text = decode_text(content)
    match = re.search(r"(?is)<html[\s>].*?</html>", text)
    return match.group(0) if match else text


def _html_to_markdown(html: str) -> str:
    from .body_format import render_body
    md, _ = render_body(html=html, plain="", fmt="markdown")
    return md.strip()


def parse_msg(
    content: bytes,
    *,
    depth: int,
    max_pages: int,
    extract_tables: bool,
    password: Optional[str],
) -> str:
    try:
        import extract_msg
    except ImportError as exc:
        raise RuntimeError("extract-msg not installed. Run: pip install extract-msg") from exc

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".msg", delete=False) as tf:
            tf.write(content)
            tmp_path = tf.name
        msg = extract_msg.openMsg(tmp_path)
        parts = _email_envelope_lines(
            subject=(msg.subject or "").strip(),
            sender=(msg.sender or "").strip(),
            to=getattr(msg, "to", None),
            cc=getattr(msg, "cc", None),
            bcc=getattr(msg, "bcc", None),
            date=getattr(msg, "date", None) or getattr(msg, "delivered_at", None),
        )
        parts.append("")
        parts.append(_msg_body(msg))

        nested = list(getattr(msg, "attachments", []) or [])
        if nested:
            parts.append("")
            parts.extend(
                _extract_nested_attachments(
                    [
                        (
                            getattr(att, "longFilename", None)
                            or getattr(att, "shortFilename", None)
                            or f"attachment_{i}",
                            getattr(att, "data", b"") or b"",
                        )
                        for i, att in enumerate(nested, 1)
                    ],
                    depth=depth,
                    max_pages=max_pages,
                    extract_tables=extract_tables,
                    password=password,
                )
            )
        msg.close()
        return "\n".join(parts).strip()
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _msg_body(msg) -> str:
    html_body = getattr(msg, "htmlBody", None) or getattr(msg, "bodyHTML", None)
    text_body = getattr(msg, "body", None) or ""
    rtf_body = getattr(msg, "rtfBody", None)

    if html_body:
        if isinstance(html_body, bytes):
            html_body = html_body.decode("utf-8", errors="replace")
        return _html_to_markdown(html_body)
    if text_body:
        return text_body.strip() if isinstance(text_body, str) else text_body.decode("utf-8", errors="replace").strip()
    if rtf_body:
        if isinstance(rtf_body, bytes):
            return parse_rtf(rtf_body)
        return parse_rtf(rtf_body.encode("utf-8", errors="replace"))
    return ""


def parse_eml(
    content: bytes,
    *,
    depth: int,
    max_pages: int,
    extract_tables: bool,
    password: Optional[str],
) -> str:
    try:
        msg = BytesParser(policy=policy.default).parsebytes(content)
    except Exception as exc:
        raise RuntimeError(f"Failed to parse EML: {exc}") from exc

    parts = _email_envelope_lines(
        subject=msg.get("Subject", ""),
        sender=msg.get("From", ""),
        to=msg.get("To", ""),
        cc=msg.get("Cc"),
        date=msg.get("Date"),
    )
    parts.append("")

    html_body = ""
    text_body = ""
    nested: List[Tuple[str, bytes]] = []
    smime_signatures: List[bytes] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        disp = (part.get("Content-Disposition", "") or "").lower()
        if ctype in ("application/pkcs7-signature", "application/x-pkcs7-signature"):
            smime_signatures.append(part.get_payload(decode=True) or b"")
            continue
        if "attachment" in disp:
            fn = part.get_filename() or "(unnamed)"
            payload = part.get_payload(decode=True) or b""
            nested.append((fn, payload))
            continue
        if ctype == "text/html" and not html_body:
            try:
                html_body = part.get_content()
            except Exception:
                html_body = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
        elif ctype == "text/plain" and not text_body:
            try:
                text_body = part.get_content()
            except Exception:
                text_body = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")

    if html_body:
        parts.append(_html_to_markdown(html_body))
    elif text_body:
        parts.append(text_body.strip())

    if smime_signatures:
        from .attachment_formats_extra import parse_pkcs7_signature
        signed_bytes = None
        if text_body:
            signed_bytes = text_body.encode("utf-8")
        elif html_body:
            signed_bytes = html_body.encode("utf-8")
        parts.append("")
        parts.append(
            parse_pkcs7_signature(
                smime_signatures[0],
                signed_content=signed_bytes,
                html_to_md=_html_to_markdown,
            )
        )

    if nested:
        parts.append("")
        parts.extend(
            _extract_nested_attachments(
                nested,
                depth=depth,
                max_pages=max_pages,
                extract_tables=extract_tables,
                password=password,
            )
        )
    return "\n".join(parts).strip()


def _email_envelope_lines(
    *,
    subject: str,
    sender: str,
    to=None,
    cc=None,
    bcc=None,
    date=None,
) -> List[str]:
    lines = [
        f"Subject: {subject}",
        f"From: {sender}",
    ]
    if to:
        lines.append(f"To: {to}")
    if cc:
        lines.append(f"Cc: {cc}")
    if bcc:
        lines.append(f"Bcc: {bcc}")
    if date:
        lines.append(f"Date: {date}")
    return lines


def _extract_nested_attachments(
    items: List[Tuple[str, bytes]],
    *,
    depth: int,
    max_pages: int,
    extract_tables: bool,
    password: Optional[str],
) -> List[str]:
    lines = [f"--- Nested attachments ({len(items)}) ---"]
    extracted = 0
    for i, (name, data) in enumerate(items, 1):
        lines.append(f"\n#### {i}. {name} ({len(data)} bytes)")
        if depth >= NESTED_MAX_DEPTH or extracted >= NESTED_MAX_EXTRACT:
            lines.append("[skipped — nested depth/limit reached]")
            continue
        inner_ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if not data:
            lines.append("[empty]")
            continue
        try:
            inner = extract_attachment_text(
                data,
                inner_ext,
                max_pages=min(max_pages, 15),
                extract_tables=extract_tables,
                password=password,
                depth=depth + 1,
            )
            lines.append(inner[:10000])
            extracted += 1
        except Exception as exc:
            lines.append(f"[not extracted: {exc}]")
    return lines