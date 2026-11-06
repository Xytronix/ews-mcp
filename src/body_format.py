"""Bidirectional email body conversion — v4.0.

Read direction (HTML the LLM consumes):
    Outlook MSO HTML  ->  markdownify  ->  GFM markdown   (~12x token reduction)
                       ->  passthrough ->  HTML           (legacy default)
                       ->  text_body   ->  plain text     (lossy — drops links)

Write direction (the LLM produces a body for send/reply/forward/draft):
    markdown          ->  python-markdown  ->  HTML       (Outlook-friendly)
    text              ->  minimal HTML wrap ->  HTML
    html              ->  passthrough       ->  HTML       (legacy default)

Why two libraries:
    `markdownify` is the dominant HTML->Markdown converter; well-maintained,
    handles MSO Word junk (<o:p>, <v:shape>, namespace declarations) when
    given the right strip list.

    `markdown` (Python-Markdown) is the dominant Markdown->HTML converter;
    GFM-flavoured, ships with `extra` and `tables` extensions.

Signatures: this module ONLY converts the body the caller provides. The EWS
reply/forward path (account.create_reply / create_forward) is what
auto-appends the user's Outlook signature with its inline image (cid:) refs.
We hand HTML to that path, the signature is appended downstream — never lost.
"""
from __future__ import annotations

import logging
import re
from html import escape
from typing import Optional, Tuple

_log = logging.getLogger(__name__)

# ----- markdownify (lazy import so unit tests can import this module without)
try:
    from markdownify import markdownify as _md  # type: ignore
    _MD_AVAILABLE = True
except Exception:  # pragma: no cover
    _md = None  # type: ignore
    _MD_AVAILABLE = False

# ----- python-markdown (lazy import)
try:
    import markdown as _markdown_lib  # type: ignore
    _MARKDOWN_AVAILABLE = True
except Exception:  # pragma: no cover
    _markdown_lib = None  # type: ignore
    _MARKDOWN_AVAILABLE = False


VALID_FORMATS = ("html", "markdown", "text")
DEFAULT_READ_FORMAT = "markdown"  # ~12x cheaper than Outlook HTML; html is opt-in
DEFAULT_WRITE_FORMAT = "html"    # zero behaviour change for v3.4 writers


# Tags markdownify should strip wholesale. The Outlook MSO/Word HTML graveyard
# is full of `<o:p>`, `<v:*>`, `<w:*>`, namespaces, and inline `<style>` /
# `<meta>` blocks that turn into pages of garbage when converted naively.
_MSO_STRIP = [
    "o:p", "o:smarttagtype", "o:OfficeDocumentSettings",
    "v:shape", "v:imagedata", "v:fill", "v:stroke", "v:rect",
    "v:shapetype", "v:textbox",
    "w:wordDocument", "w:font",
    "script", "style", "meta", "xml",
]


# ============================================================================
# READ DIRECTION  (HTML -> markdown / text)
# ============================================================================

def _strip_non_content(html: str) -> str:
    """Drop <head>/<style>/<script>/<title>, comments and CDATA before
    conversion. markdownify's ``strip`` keeps the *text* of stripped tags,
    so CSS (e.g. ``#outlook a {...}``) otherwise leaks into the body."""
    if not html:
        return ""
    out = re.sub(r"(?s)<!--.*?-->", " ", html)
    out = out.replace("<![CDATA[", "").replace("]]>", "")
    out = re.sub(r"(?is)<(head|style|script|title|xml)\b[^>]*>.*?</\1\s*>", " ", out)
    out = re.sub(r"(?is)<(style|script)\b[^>]*/>", " ", out)
    return out


def _is_layout_table(table) -> bool:
    role = (table.get("role") or "").lower()
    if role in ("presentation", "none"):
        return True
    own_rows = [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]
    if any(tr.find("th", recursive=False) for tr in own_rows):
        return False
    if len(own_rows) <= 1:
        return True
    if any(len(tr.find_all(["td", "th"], recursive=False)) <= 1 for tr in own_rows):
        return True
    # Nested tables mean layout scaffolding, not tabular data.
    return table.find("table") is not None or not table.find("th")


def _flatten_layout_tables(html: str) -> str:
    """Unwrap email layout tables (no header cells / role=presentation /
    single-cell rows) into block elements so markdown isn't empty |  | grids."""
    if not html or "<table" not in html.lower():
        return html
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return html
    try:
        soup = BeautifulSoup(html, "html.parser")
        for table in reversed(soup.find_all("table")):  # innermost first
            if not _is_layout_table(table):
                continue
            for cell in table.find_all(["td", "th"]):
                if cell.find_parent("table") is table:
                    cell.name = "div"
                    cell.attrs = {}
            for tag in table.find_all(["tr", "tbody", "thead", "tfoot"]):
                if tag.find_parent("table") is table:
                    tag.name = "div"
                    tag.attrs = {}
            table.name = "div"
            table.attrs = {}
        return str(soup)
    except Exception as exc:
        _log.debug("layout-table flatten failed: %s", exc)
        return html


_EMPTY_TABLE_LINE = re.compile(r"^[\s|:\-]*$")


def _drop_empty_table_lines(md: str) -> str:
    lines = [ln for ln in md.splitlines()
             if not ("|" in ln and _EMPTY_TABLE_LINE.match(ln))]
    out = "\n".join(ln.rstrip() for ln in lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def html_to_text(html: str) -> str:
    """Best-effort HTML -> plain text for servers that omit EWS TextBody."""
    if not html:
        return ""
    import html as _html_mod
    txt = re.sub(r"(?is)<(script|style|head|title)[^>]*>.*?</\1>", " ", html)
    txt = txt.replace("<![CDATA[", "").replace("]]>", "")
    # keep link targets visible (magic links, verification URLs)
    txt = re.sub(r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                 lambda m: f"{m.group(2)} ({m.group(1)})", txt)
    txt = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6]|table)>", "\n", txt)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    txt = _html_mod.unescape(txt).replace("\xa0", " ")
    txt = re.sub(r"[ \t\r\f\v]+", " ", txt)
    txt = re.sub(r" *\n *", "\n", txt)
    return re.sub(r"\n{3,}", "\n\n", txt).strip()


def render_body(
    html: str,
    plain: str,
    fmt: str,
) -> Tuple[str, str]:
    """Convert ``html`` / ``plain`` into ``fmt`` for an outgoing tool response.

    Returns ``(body, actual_format)``. ``actual_format`` matches ``fmt`` on
    success; on failure it falls back to ``"text"`` or ``"text_fallback"``
    so the caller can detect the degradation.

    Cache hits are NOT handled here — the caller (a tool) checks the
    SQLiteCache first; this function only does the conversion work.
    """
    if fmt not in VALID_FORMATS:
        raise ValueError(f"unknown body format: {fmt!r}; expected {VALID_FORMATS}")

    html = html or ""
    plain = plain or ""
    if not plain.strip() and html:
        # Some EWS servers (e.g. SmarterMail) never populate
        # TextBody for HTML-only mail; derive text so callers never get "".
        plain = html_to_text(html)

    if fmt == "html":
        return html, "html"
    if fmt == "text":
        return plain, "text"

    # markdown
    if not _MD_AVAILABLE:
        _log.warning("markdownify not installed; falling back to text")
        return plain, "text_fallback"
    if not html:
        # No HTML at all — surface the plain part as-is, marked text so the
        # caller sees the real shape rather than a confusing 'markdown=""'.
        return plain, "text"
    try:
        md = _md(_flatten_layout_tables(_strip_non_content(html)),
                 strip=_MSO_STRIP, heading_style="ATX")
        md = _drop_empty_table_lines(md)
        # markdownify is greedy on whitespace; collapse 3+ blank lines into 2
        md = re.sub(r"\n{3,}", "\n\n", md).strip()
        return md, "markdown"
    except Exception as exc:
        _log.warning("markdown conversion failed: %s — falling back to text", exc)
        return plain, "text_fallback"


def trim_quoted(markdown_or_html: str) -> str:
    """Optional pass: strip the 'On ..., X wrote:' quoted history that Outlook
    glues into every reply. Conservative — only removes the part AFTER the
    first quoted-thread marker, leaving the new content intact.

    Recognised markers (case-insensitive):
      - "On Mon, Jan 1, 2026 ..., X wrote:"
      - "From: ..." line followed by "Sent:", "To:", "Subject:" headers
      - "-----Original Message-----"
      - Arabic equivalent: "من: ... المرسل:" header block
    """
    if not markdown_or_html:
        return markdown_or_html
    s = markdown_or_html
    patterns = [
        r"\n\s*On\s+[A-Za-z]+,?\s+[A-Za-z0-9 ,:]+\s+at\s+[^\n]+wrote:\s*\n",
        r"\n\s*-{3,}\s*Original Message\s*-{3,}\s*\n",
        r"\n\s*From:\s*[^\n]+\n\s*Sent:\s*[^\n]+\n",
        r"\n\s*From:\s*[^\n]+\n\s*To:\s*[^\n]+\n\s*Subject:\s*",
        r"\n\s*من:\s*[^\n]+\n\s*المرسل:\s*",
    ]
    for pat in patterns:
        m = re.search(pat, s, flags=re.IGNORECASE | re.MULTILINE)
        if m:
            return s[: m.start()].rstrip() + "\n\n_(thread history trimmed)_"
    return s


# ============================================================================
# WRITE DIRECTION  (markdown / text  ->  HTML)
# ============================================================================

def compose_body(
    body: str,
    body_format: str,
) -> Tuple[str, str]:
    """Convert an LLM-supplied ``body`` in ``body_format`` to HTML for EWS.

    Returns ``(html, actual_format)``. ``actual_format`` is ``"html"`` on
    success, ``"html_fallback"`` if the caller asked for markdown but the
    library is missing or conversion failed (in which case we wrap the raw
    body as `<pre>` so it still renders, just less prettily).

    The caller passes the resulting HTML straight to EWS create_reply /
    create_forward / Message(body=...) — Exchange's signature-append step
    runs DOWNSTREAM and is unaffected.
    """
    if body_format not in VALID_FORMATS:
        raise ValueError(
            f"unknown body_format: {body_format!r}; expected {VALID_FORMATS}"
        )
    body = body or ""

    if body_format == "html":
        return body, "html"

    if body_format == "text":
        # Wrap text minimally so Outlook renders newlines.
        # Use <p> per paragraph (double-newline split) and <br> within paragraphs.
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        if not paragraphs:
            return "", "html"
        html = "".join(
            "<p>" + escape(p).replace("\n", "<br>") + "</p>"
            for p in paragraphs
        )
        return html, "html"

    # markdown
    if not _MARKDOWN_AVAILABLE:
        _log.warning("python-markdown not installed; wrapping body as <pre>")
        return f"<pre>{escape(body)}</pre>", "html_fallback"
    try:
        # `extra` enables tables, fenced code, abbreviations, footnotes —
        # the GFM-ish set most LLMs naturally produce.
        # `nl2br` keeps plain newlines visible (matches email expectations).
        html = _markdown_lib.markdown(
            body,
            extensions=["extra", "nl2br", "sane_lists"],
            output_format="html",
        )
        return html, "html"
    except Exception as exc:
        _log.warning("markdown->HTML conversion failed: %s", exc)
        return f"<pre>{escape(body)}</pre>", "html_fallback"


# ============================================================================
# Schema fragments (consumed by tool definitions to keep schemas DRY)
# ============================================================================

READ_FORMAT_SCHEMA = {
    "format": {
        "type": "string",
        "enum": list(VALID_FORMATS),
        "default": DEFAULT_READ_FORMAT,
        "description": (
            "Body representation in the response. 'markdown' (default) "
            "converts the HTML to GFM markdown server-side (~12x fewer tokens "
            "on Outlook MSO bodies; result is cached). 'html' is the escape "
            "hatch for the raw Outlook HTML. 'text' returns plain text only "
            "(lossy — drops links and inline images)."
        ),
    },
    "trim_quoted": {
        "type": "boolean",
        "default": False,
        "description": (
            "When true and format='markdown' or 'text', strip the 'On ..., "
            "X wrote:' / '----Original Message----' history glued onto the "
            "bottom of the body. Massive savings on long threads."
        ),
    },
    "include_body": {
        "type": "boolean",
        "default": True,
        "description": (
            "When false, drop the body field entirely from the response. "
            "Useful for list-style calls where the agent only needs subject "
            "+ from + received_time + snippet."
        ),
    },
}

WRITE_FORMAT_SCHEMA = {
    "body_format": {
        "type": "string",
        "enum": list(VALID_FORMATS),
        "default": DEFAULT_WRITE_FORMAT,
        "description": (
            "Format of the supplied 'body' field. 'html' (default) — body is "
            "raw HTML, passed straight to EWS (v3.4 behaviour). 'markdown' — "
            "body is GFM markdown, converted to HTML server-side before EWS. "
            "'text' — body is plain text, wrapped minimally so Outlook "
            "renders newlines. The user's signature is appended by EWS "
            "downstream regardless of format — it is NOT touched here."
        ),
    },
}
