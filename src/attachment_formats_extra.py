"""Specialized attachment parsers (epub, certs, geo, tabular, etc.)."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from email import policy
from email.parser import BytesParser
from typing import Callable, List, Optional, Tuple

def _decode_text(content: bytes) -> str:
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


def parse_tar_archive(
    content: bytes,
    *,
    extract_inner,
    max_pages: int,
    extract_tables: bool,
    password: Optional[str],
) -> str:
    """Extract and parse files from tar / tar.gz / tar.bz2 / tar.xz."""
    lines = ["--- TAR archive ---"]
    parsed = 0
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as tf:
        members = [m for m in tf.getmembers() if m.isfile()][:25]
        for member in members:
            if member.size > 15 * 1024 * 1024:
                lines.append(f"\n### {member.name} [skipped — too large]")
                continue
            try:
                f = tf.extractfile(member)
                if f is None:
                    continue
                data = f.read()
            except Exception as exc:
                lines.append(f"\n### {member.name} [read error: {exc}]")
                continue
            inner_ext = member.name.rsplit(".", 1)[-1].lower() if "." in member.name else ""
            lines.append(f"\n### {member.name} ({len(data)} bytes)")
            try:
                inner_text = extract_inner(
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
    if parsed == 0 and len(lines) == 1:
        raise RuntimeError("TAR archive contained no parseable files")
    return "\n".join(lines).strip()


def _collect_pkpass_barcodes(data: dict) -> List[dict]:
    barcodes: List[dict] = []

    def _add_barcode(obj) -> None:
        if not isinstance(obj, dict):
            return
        entry = {
            k: obj.get(k)
            for k in ("format", "message", "altText", "messageEncoding")
            if obj.get(k) is not None
        }
        if entry:
            barcodes.append(entry)

    if isinstance(data.get("barcode"), dict):
        _add_barcode(data["barcode"])
    for item in data.get("barcodes") or []:
        _add_barcode(item)

    for key in ("eventTicket", "boardingPass", "coupon", "storeCard", "generic"):
        section = data.get(key)
        if not isinstance(section, dict):
            continue
        if isinstance(section.get("barcode"), dict):
            _add_barcode(section["barcode"])
        for item in section.get("barcodes") or []:
            _add_barcode(item)

    deduped: List[dict] = []
    seen: set[str] = set()
    for entry in barcodes:
        key = json.dumps(entry, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def parse_pkpass(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        try:
            raw = zf.read("pass.json")
        except KeyError as exc:
            raise RuntimeError("pkpass bundle missing pass.json") from exc
    data = json.loads(raw.decode("utf-8"))
    fields = {
        "description": data.get("description"),
        "organizationName": data.get("organizationName"),
        "passTypeIdentifier": data.get("passTypeIdentifier"),
        "serialNumber": data.get("serialNumber"),
        "teamIdentifier": data.get("teamIdentifier"),
    }
    barcodes = _collect_pkpass_barcodes(data)
    if barcodes:
        fields["barcodes"] = barcodes
    for key in ("eventTicket", "boardingPass", "coupon", "storeCard", "generic"):
        if key in data:
            fields[key] = data[key]
    return json.dumps({k: v for k, v in fields.items() if v}, indent=2, ensure_ascii=False)


def parse_epub(content: bytes, html_to_md) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = zf.namelist()
        opf_path = next((n for n in names if n.endswith(".opf")), None)
        if not opf_path:
            raise RuntimeError("EPUB missing package OPF")
        opf = zf.read(opf_path).decode("utf-8", errors="replace")
        root = ET.fromstring(opf.encode("utf-8", errors="replace"))
        ns = {"opf": "http://www.idpf.org/2007/opf"}
        title_el = root.find(".//{http://purl.org/dc/elements/1.1/}title")
        title = title_el.text if title_el is not None else "EPUB"
        opf_dir = opf_path.rsplit("/", 1)[0]
        spine = root.find(".//opf:spine", ns) or root.find(".//{*}spine")
        if spine is None:
            raise RuntimeError("EPUB missing spine")
        manifest = {
            item.get("id"): item.get("href")
            for item in (root.findall(".//opf:item", ns) or root.findall(".//{*}item"))
            if item.get("href")
        }
        parts = [f"--- EPUB: {title} ---"]
        for i, itemref in enumerate(spine.findall(".//opf:itemref", ns) or spine.findall(".//{*}itemref"), 1):
            href = manifest.get(itemref.get("idref"))
            if not href:
                continue
            path = f"{opf_dir}/{href}".replace("//", "/") if opf_dir else href
            try:
                html = zf.read(path).decode("utf-8", errors="replace")
                md = html_to_md(html)
                parts.append(f"\n## Chapter {i}\n{md[:8000]}")
            except Exception as exc:
                parts.append(f"\n## Chapter {i} [unreadable: {exc}]")
            if i >= 15:
                parts.append("\n... (truncated at 15 chapters)")
                break
    return "\n".join(parts).strip()


def parse_xps(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        pages = sorted(
            n for n in zf.namelist()
            if n.lower().endswith(".fpage") or n.lower().endswith(".fdseq")
        )
        if not pages:
            raise RuntimeError("XPS/OXPS missing page markup")
        parts = [f"--- XPS/OXPS ({len(pages)} markup file(s)) ---"]
        for i, name in enumerate(pages[:30], 1):
            xml = zf.read(name).decode("utf-8", errors="replace")
            text_bits = re.findall(r"<(?:Glyphs|Run)[^>]*>([^<]+)<", xml)
            if not text_bits:
                text_bits = re.findall(r">([^<>]{2,})<", xml)
            text = " ".join(t.strip() for t in text_bits if t.strip())
            parts.append(f"\n## Page {i}\n{text[:4000]}")
    return "\n".join(parts).strip()


def parse_mt940(content: bytes) -> str:
    text = _decode_text(content)
    lines_out = ["--- MT940 bank statement ---"]
    for tag, body in re.findall(r":(\d{2}[A-Z]?):([^\n:]+)", text):
        lines_out.append(f"{tag}: {body.strip()}")
    txns = re.findall(
        r":61:([^\n]+)\n(?::86:([^\n:]+))?",
        text,
    )
    if txns:
        lines_out.append(f"\nTransactions ({len(txns)}):")
        for i, (line61, line86) in enumerate(txns[:150], 1):
            lines_out.append(f"  {i}. {line61.strip()}")
            if line86:
                lines_out.append(f"     {line86.strip()}")
    if len(lines_out) == 1:
        raise RuntimeError("No MT940 fields found")
    return "\n".join(lines_out).strip()


_PRIVATE_KEY_MARKERS = (
    "PRIVATE KEY",
    "RSA PRIVATE KEY",
    "EC PRIVATE KEY",
    "ENCRYPTED PRIVATE KEY",
    "OPENSSH PRIVATE KEY",
)


def _pem_blocks(content: bytes) -> List[tuple[str, str]]:
    text = _decode_text(content)
    blocks = re.findall(
        r"-----BEGIN ([^-]+)-----(.*?)-----END \1-----",
        text,
        re.DOTALL,
    )
    return [(label.strip(), body.strip()) for label, body in blocks]


def parse_certificate(content: bytes, ext: str) -> str:
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
    except ImportError as exc:
        raise RuntimeError("cryptography not installed. Run: pip install cryptography") from exc

    if ext == "der":
        cert = x509.load_der_x509_certificate(content, default_backend())
        info = {
            "subject": cert.subject.rfc4514_string(),
            "issuer": cert.issuer.rfc4514_string(),
            "not_valid_before": str(cert.not_valid_before_utc),
            "not_valid_after": str(cert.not_valid_after_utc),
            "serial_number": str(cert.serial_number),
        }
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            info["san"] = [str(n) for n in san.value]
        except Exception:
            pass
        return json.dumps(info, indent=2)

    blocks = _pem_blocks(content)
    if not blocks:
        raise RuntimeError("No PEM certificate or key blocks found")

    lines = ["--- PEM bundle ---"]
    cert_count = 0
    for label, body in blocks:
        if any(marker in label.upper() for marker in _PRIVATE_KEY_MARKERS):
            lines.append(
                f"\n## {label}\n[REDACTED PRIVATE KEY — {len(body)} bytes; "
                "use download_attachment if you need the file]"
            )
            continue
        if "CERTIFICATE" not in label.upper():
            lines.append(f"\n## {label}\n[unsupported PEM block type]")
            continue
        cert_count += 1
        pem = f"-----BEGIN {label}-----\n{body}\n-----END {label}-----"
        cert = x509.load_pem_x509_certificate(pem.encode(), default_backend())
        lines.append(f"\n## Certificate {cert_count}")
        lines.append(f"subject: {cert.subject.rfc4514_string()}")
        lines.append(f"issuer: {cert.issuer.rfc4514_string()}")
        lines.append(f"not_valid_before: {cert.not_valid_before_utc}")
        lines.append(f"not_valid_after: {cert.not_valid_after_utc}")
        lines.append(f"serial_number: {cert.serial_number}")
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            lines.append(f"san: {', '.join(str(n) for n in san.value)}")
        except Exception:
            pass

    if cert_count == 0:
        raise RuntimeError("PEM file contained no certificates (private keys were redacted)")
    return "\n".join(lines).strip()


def _openssl_cms_verify(
    content: bytes,
    *,
    signed_content: Optional[bytes] = None,
) -> Tuple[Optional[bytes], str]:
    """Extract signed payload from PKCS#7/CMS using OpenSSL.

    Returns (payload_bytes, mode) where mode is embedded, detached, or none.
    """
    tmp_sig = None
    tmp_body = None
    try:
        with tempfile.NamedTemporaryFile(delete=False) as sig_f:
            sig_f.write(content)
            tmp_sig = sig_f.name
        cmd = [
            "openssl", "cms", "-verify",
            "-in", tmp_sig,
            "-noverify",
            "-out", "/dev/stdout",
        ]
        last_proc = None
        for inform in ("SMIME", "PEM", "DER"):
            attempt = [*cmd, "-inform", inform]
            if signed_content is not None:
                with tempfile.NamedTemporaryFile(delete=False) as body_f:
                    body_f.write(signed_content)
                    tmp_body = body_f.name
                attempt.extend(["-content", tmp_body])
            try:
                last_proc = subprocess.run(
                    attempt,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise RuntimeError("openssl not available for S/MIME extraction") from exc
            if last_proc.returncode == 0 and last_proc.stdout:
                mode = "detached" if signed_content is not None else "embedded"
                return last_proc.stdout, mode
            if tmp_body:
                try:
                    os.unlink(tmp_body)
                except OSError:
                    pass
                tmp_body = None
        if last_proc is None:
            return None, "none"
        if b"no content" in last_proc.stderr.lower():
            return None, "detached"
        err = last_proc.stderr.decode("utf-8", errors="replace")[:300]
        if err:
            return None, f"failed: {err.strip()}"
        return None, "none"
    finally:
        for path in (tmp_sig, tmp_body):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


def _format_pkcs7_payload(
    payload: bytes,
    *,
    html_to_md: Optional[Callable[[str], str]] = None,
) -> str:
    if not payload.strip():
        return "[empty signed payload]"

    text = _decode_text(payload)
    if "MIME-Version:" in text[:500] or text.lstrip().startswith("Content-"):
        try:
            msg = BytesParser(policy=policy.default).parsebytes(payload)
            parts = []
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype in ("text/plain", "text/html"):
                    try:
                        body = part.get_content()
                    except Exception:
                        body = (part.get_payload(decode=True) or b"").decode(
                            "utf-8", errors="replace"
                        )
                    if ctype == "text/html" and html_to_md:
                        body = html_to_md(body)
                    parts.append(body.strip())
            if parts:
                return "\n\n".join(parts)
        except Exception:
            pass
    return text.strip()


def _load_pkcs7_certificates(content: bytes):
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.serialization.pkcs7 import (
        load_der_pkcs7_certificates,
        load_pem_pkcs7_certificates,
    )

    try:
        return load_pem_pkcs7_certificates(content)
    except Exception:
        try:
            return load_der_pkcs7_certificates(content)
        except Exception:
            text = _decode_text(content)
            blocks = re.findall(
                r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
                text,
                re.DOTALL,
            )
            return [
                x509.load_pem_x509_certificate(b.encode(), default_backend())
                for b in blocks
            ]


def parse_pkcs7_signature(
    content: bytes,
    *,
    signed_content: Optional[bytes] = None,
    html_to_md: Optional[Callable[[str], str]] = None,
) -> str:
    try:
        from cryptography import x509  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("cryptography not installed. Run: pip install cryptography") from exc

    certs = _load_pkcs7_certificates(content)
    if not certs:
        raise RuntimeError("No certificates found in PKCS#7 / signature file")

    payload, mode = _openssl_cms_verify(content, signed_content=signed_content)
    lines = [f"--- S/MIME PKCS#7 ({len(certs)} certificate(s)) ---"]
    for i, cert in enumerate(certs, 1):
        lines.append(f"\n## Signer certificate {i}")
        lines.append(f"subject: {cert.subject.rfc4514_string()}")
        lines.append(f"issuer: {cert.issuer.rfc4514_string()}")
        lines.append(f"expires: {cert.not_valid_after_utc}")

    if payload:
        lines.append(f"\n### Signed content ({mode})")
        formatted = _format_pkcs7_payload(payload, html_to_md=html_to_md)
        lines.append(formatted[:20000])
    elif mode == "detached":
        lines.append(
            "\n### Signed content\n"
            "[detached signature — signed body is separate from this .p7s file. "
            "Pass the parent email body as signed content, or read the email body directly.]"
        )
    else:
        lines.append(f"\n### Signed content\n[not extracted: {mode}]")

    return "\n".join(lines).strip()


def parse_pgp_clearsign(content: bytes) -> str:
    text = _decode_text(content)
    match = re.search(
        r"-----BEGIN PGP SIGNED MESSAGE-----\n(?:[^\n]+\n)*?\n(.*)\n-----BEGIN PGP SIGNATURE-----",
        text,
        re.DOTALL,
    )
    if match:
        return match.group(1).strip()
    if "-----BEGIN PGP MESSAGE-----" in text:
        return "[encrypted PGP message — cannot extract without private key]"
    return text.strip()


def parse_kml(content: bytes) -> str:
    return _parse_geo_xml(content, "kml")


def parse_gpx(content: bytes) -> str:
    return _parse_geo_xml(content, "gpx")


def _parse_geo_xml(content: bytes, kind: str) -> str:
    root = ET.fromstring(content)
    lines = [f"--- {kind.upper()} ---"]
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag in ("name", "description", "time", "coordinates", "cmt", "type"):
            text = (elem.text or "").strip()
            if text:
                lines.append(f"{tag}: {text[:500]}")
    if len(lines) == 1:
        raise RuntimeError(f"No readable features in {kind.upper()}")
    return "\n".join(lines[:200]).strip()


def parse_kmz(content: bytes, html_to_md) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        kml_name = next((n for n in zf.namelist() if n.lower().endswith(".kml")), None)
        if not kml_name:
            raise RuntimeError("KMZ missing doc.kml")
        return parse_kml(zf.read(kml_name))


def parse_ndjson(content: bytes) -> str:
    rows = []
    for line in _decode_text(content).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"raw": line})
        if len(rows) >= 100:
            break
    if not rows:
        raise RuntimeError("NDJSON/JSONL file empty")
    return json.dumps({"records": rows, "count": len(rows)}, indent=2, ensure_ascii=False)[:50000]


def parse_parquet(content: bytes) -> str:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("pyarrow not installed. Run: pip install pyarrow") from exc
    table = pq.read_table(io.BytesIO(content))
    df = table.to_pandas()
    preview = df.head(100)
    return f"--- Parquet ({len(df)} rows, {len(df.columns)} columns) ---\n{preview.to_csv(index=False)}"


def parse_avro(content: bytes) -> str:
    try:
        import fastavro
    except ImportError as exc:
        raise RuntimeError("fastavro not installed. Run: pip install fastavro") from exc
    reader = fastavro.reader(io.BytesIO(content))
    schema = reader.writer_schema
    records = []
    for i, record in enumerate(reader):
        records.append(record)
        if i >= 99:
            break
    return json.dumps(
        {"schema": schema, "records": records, "preview_count": len(records)},
        indent=2,
        default=str,
        ensure_ascii=False,
    )[:50000]


def parse_onenote(content: bytes) -> str:
    try:
        import olefile
    except ImportError as exc:
        raise RuntimeError("olefile not installed. Run: pip install olefile") from exc
    if not olefile.isOleFile(io.BytesIO(content)):
        raise RuntimeError("Not a valid OneNote OLE file")
    ole = olefile.OleFileIO(io.BytesIO(content))
    streams = ["/".join(s) for s in ole.listdir()]
    ole.close()
    lines = [
        "--- OneNote (.one) ---",
        f"Internal streams ({len(streams)}):",
    ]
    lines.extend(f"  - {s}" for s in streams[:40])
    lines.append(
        "\nOneNote binary format — full text not extractable server-side. "
        "Ask sender to export as PDF or DOCX, or open in OneNote and copy."
    )
    return "\n".join(lines)


def parse_database_binary(ext: str) -> str:
    raise RuntimeError(
        f".{ext} Access database — not supported for text extraction. "
        "Use download_attachment and open in Access/LibreOffice Base."
    )


def parse_toml_text(content: bytes) -> str:
    import tomllib
    data = tomllib.loads(_decode_text(content))
    return json.dumps(data, indent=2, ensure_ascii=False)


_DWG_VERSIONS = {
    "AC1012": "AutoCAD R13",
    "AC1014": "AutoCAD R14",
    "AC1015": "AutoCAD 2000",
    "AC1018": "AutoCAD 2004",
    "AC1021": "AutoCAD 2007",
    "AC1024": "AutoCAD 2010",
    "AC1027": "AutoCAD 2013",
    "AC1032": "AutoCAD 2018",
}


def _cad_text_entities(doc) -> List[str]:
    texts: List[str] = []
    msp = doc.modelspace()
    for entity in msp:
        dxftype = entity.dxftype()
        if dxftype == "TEXT":
            val = getattr(entity.dxf, "text", "")
            if val:
                texts.append(str(val).strip())
        elif dxftype == "MTEXT":
            try:
                val = entity.plain_text()
            except Exception:
                val = getattr(entity.dxf, "text", "")
            if val:
                texts.append(str(val).strip())
        elif dxftype == "DIMENSION":
            val = getattr(entity.dxf, "text", "")
            if val:
                texts.append(f"[dim] {str(val).strip()}")
        elif dxftype == "ATTRIB":
            val = getattr(entity.dxf, "text", "")
            tag = getattr(entity.dxf, "tag", "")
            if val:
                texts.append(f"[{tag}] {str(val).strip()}")
    return texts


def _summarize_cad_doc(doc, *, source_label: str) -> str:
    layers = sorted({layer.dxf.name for layer in doc.layers})
    texts = _cad_text_entities(doc)
    blocks = sorted({blk.name for blk in doc.blocks if not blk.name.startswith("*")})[:40]

    lines = [
        f"--- {source_label} ---",
        f"DXF version: {doc.dxfversion}",
        f"Layers ({len(layers)}): {', '.join(layers[:30])}",
    ]
    if len(layers) > 30:
        lines.append(f"... ({len(layers) - 30} more layers)")
    if blocks:
        lines.append(f"Blocks ({len(blocks)}): {', '.join(blocks[:20])}")
    if texts:
        lines.append(f"\nText entities ({len(texts)}, showing up to 120):")
        for i, text in enumerate(texts[:120], 1):
            lines.append(f"  {i}. {text[:500]}")
        if len(texts) > 120:
            lines.append(f"... ({len(texts) - 120} more text entities)")
    else:
        lines.append("\nNo TEXT/MTEXT/DIMENSION labels found in modelspace.")
    return "\n".join(lines).strip()


def parse_dxf(content: bytes) -> str:
    try:
        import ezdxf
        from ezdxf import recover
    except ImportError as exc:
        raise RuntimeError("ezdxf not installed. Run: pip install ezdxf") from exc

    try:
        doc, _auditor = recover.read(io.BytesIO(content))
    except Exception as exc:
        raise RuntimeError(f"DXF parse failed: {exc}") from exc
    return _summarize_cad_doc(doc, source_label="DXF drawing")


def parse_dwg(content: bytes) -> str:
    try:
        import ezdxf
        from ezdxf import recover
    except ImportError as exc:
        raise RuntimeError("ezdxf not installed. Run: pip install ezdxf") from exc

    header = content[:6].decode("ascii", errors="replace")
    version = _DWG_VERSIONS.get(header, header or "unknown")

    try:
        doc, _auditor = recover.read(io.BytesIO(content))
        return _summarize_cad_doc(doc, source_label=f"DWG drawing ({version})")
    except Exception:
        pass

    lines = [
        f"--- DWG drawing ({version}) ---",
        f"File size: {len(content)} bytes",
        "Binary DWG — full geometry/text extraction needs ODA/LibreDWG conversion.",
        "Metadata from header only; ask sender for DXF/PDF export for full text.",
    ]
    if header.startswith("AC"):
        lines.append(f"DWG version code: {header}")
    return "\n".join(lines).strip()