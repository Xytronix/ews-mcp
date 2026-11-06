"""Document text extraction via Liteparse (PDF, Office, images)."""

from __future__ import annotations

import io
import logging
import subprocess
import tempfile
from typing import Optional

_log = logging.getLogger(__name__)

LITEPARSE_EXTENSIONS = frozenset({
    "pdf",
    "doc", "docx", "docm",
    "xls", "xlsx", "xlsm",
    "ppt", "pptx", "pptm",
    "odt", "ods", "odp",
    "png", "jpg", "jpeg", "jfif", "tif", "tiff", "webp", "bmp", "gif",
})

HEIC_EXTENSIONS = frozenset({"heic", "heif"})


def supports_liteparse(file_ext: str) -> bool:
    ext = file_ext.lower().lstrip(".")
    return ext in LITEPARSE_EXTENSIONS or ext in HEIC_EXTENSIONS


def _convert_heic_to_jpeg(content: bytes) -> bytes:
    try:
        from PIL import Image
        import pillow_heif
    except ImportError as exc:
        raise RuntimeError(
            "pillow-heif not installed for HEIC support. Run: pip install pillow pillow-heif"
        ) from exc

    pillow_heif.register_heif_opener()
    with Image.open(io.BytesIO(content)) as img:
        rgb = img.convert("RGB")
        out = io.BytesIO()
        rgb.save(out, format="JPEG", quality=90)
        return out.getvalue()


def _convert_heic_via_imagemagick(content: bytes) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".heic", delete=False) as src:
        src.write(content)
        src_path = src.name
    dst_path = src_path + ".jpg"
    try:
        subprocess.run(
            ["convert", src_path, dst_path],
            check=True,
            capture_output=True,
            timeout=120,
        )
        with open(dst_path, "rb") as f:
            return f.read()
    finally:
        for path in (src_path, dst_path):
            try:
                import os
                os.unlink(path)
            except OSError:
                pass


def prepare_liteparse_input(content: bytes, source_ext: str) -> tuple[bytes, str]:
    ext = source_ext.lower().lstrip(".")
    if ext in HEIC_EXTENSIONS:
        try:
            return _convert_heic_to_jpeg(content), "jpeg"
        except Exception as exc:
            _log.debug("pillow-heif failed (%s), trying ImageMagick", exc)
            return _convert_heic_via_imagemagick(content), "jpeg"
    return content, ext


def parse_document_bytes(
    content: bytes,
    file_ext: str,
    *,
    max_pages: int = 50,
    extract_tables: bool = False,
    password: Optional[str] = None,
    source_ext: Optional[str] = None,
) -> str:
    """Parse attachment bytes to markdown/text using Liteparse."""
    try:
        from liteparse import LiteParse
    except ImportError as exc:
        raise RuntimeError(
            "liteparse not installed. Run: pip install liteparse"
        ) from exc

    src_ext = (source_ext or file_ext).lower().lstrip(".")
    payload, ext = prepare_liteparse_input(content, src_ext)

    target_pages: Optional[str] = None
    if ext == "pdf" and max_pages > 0:
        target_pages = f"1-{max_pages}"

    output_format = (
        "markdown"
        if extract_tables or ext in {
            "xlsx", "xls", "xlsm", "docx", "doc", "docm", "pptx", "ppt", "pptm",
        }
        else "text"
    )

    parser = LiteParse(
        output_format=output_format,
        ocr_enabled=True,
        ocr_language="eng",
        max_pages=max_pages,
        target_pages=target_pages,
        quiet=True,
        num_workers=2,
        image_mode="off",
        extract_links=True,
        password=password,
    )

    result = parser.parse(payload)
    text = (result.text or "").strip()
    if not text:
        hint = " (wrong password?)" if password and ext == "pdf" else ""
        raise RuntimeError(f"Liteparse returned no extractable text{hint}")
    return text