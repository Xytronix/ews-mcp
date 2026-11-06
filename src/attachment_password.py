"""Heuristics to guess archive passwords from email body text."""

from __future__ import annotations

import re
from typing import List, Optional

_PASSWORD_ERROR_MARKERS = (
    "password-protected",
    "pass the `password`",
    "pass the password",
    "wrong password",
    "encrypted",
)

_PWD_VALUE = r'["\']?([^\s"\'<>,;]{3,64})'
_PWD_SEP = r"(?:is\s*[:=]|was\s*[:=]|[:=])"

_LABEL_PATTERNS = (
    re.compile(
        rf"(?i)(?:zip|7z|rar|archive)\s+password\s*{_PWD_SEP}\s*{_PWD_VALUE}"
    ),
    re.compile(
        rf"(?i)(?:file|attachment|entpack(?:en|ung)?|"
        rf"passwort|password|pwd|kennwort|code|pin)\s*{_PWD_SEP}\s*{_PWD_VALUE}"
    ),
    re.compile(
        rf"(?i)password\s+for\s+[^:\n]{{0,80}}:\s*{_PWD_VALUE}"
    ),
    re.compile(
        rf"(?i)(?:the\s+)?(?:zip\s+)?password\s+(?:is|was)\s+{_PWD_VALUE}"
    ),
    re.compile(
        rf"(?i)use\s+(?:the\s+)?password\s+{_PWD_VALUE}"
    ),
    re.compile(
        rf"(?i)(?:protected|encrypted)\s+(?:with|by)\s+password\s+{_PWD_VALUE}"
    ),
    re.compile(
        rf"(?i)uses\s+password\s+{_PWD_VALUE}"
    ),
)

_QUOTED_ON_LINE = re.compile(r'["\']([^"\']{3,64})["\']')


def is_password_protected_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _PASSWORD_ERROR_MARKERS)


def guess_passwords_from_email(
    body: str,
    attachment_name: str = "",
    *,
    max_candidates: int = 8,
) -> List[str]:
    """Return ordered unique password candidates found in email body text."""
    if not body or not body.strip():
        return []

    text = body.replace("\r\n", "\n")
    candidates: List[str] = []
    seen: set[str] = set()

    def _add(value: Optional[str]) -> None:
        if not value:
            return
        cleaned = value.strip().strip("\"'`,.;:")
        if len(cleaned) < 3 or len(cleaned) > 64:
            return
        lower = cleaned.lower()
        if lower in seen:
            return
        if lower in {"password", "passwort", "none", "n/a", "unknown", "zip", "archive"}:
            return
        seen.add(lower)
        candidates.append(cleaned)

    for pattern in _LABEL_PATTERNS:
        for match in pattern.finditer(text):
            _add(match.group(1))
            if len(candidates) >= max_candidates:
                return candidates

    basename = ""
    if attachment_name:
        basename = attachment_name.rsplit("/", 1)[-1]
        stem = basename.rsplit(".", 1)[0] if "." in basename else basename
        for line in text.splitlines():
            line_lower = line.lower()
            if basename.lower() not in line_lower and stem.lower() not in line_lower:
                continue
            for pattern in _LABEL_PATTERNS:
                match = pattern.search(line)
                if match:
                    _add(match.group(1))
            for quoted in _QUOTED_ON_LINE.findall(line):
                _add(quoted)
            if len(candidates) >= max_candidates:
                return candidates

    for line in text.splitlines():
        line_lower = line.lower()
        if not any(
            token in line_lower
            for token in ("password", "passwort", "zip", "archive", "7z", "rar", "entpack")
        ):
            continue
        for quoted in _QUOTED_ON_LINE.findall(line):
            _add(quoted)
        if len(candidates) >= max_candidates:
            break

    return candidates[:max_candidates]