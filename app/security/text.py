"""Output encoding and secret redaction. Page text is data, never a policy source."""

from __future__ import annotations

import re

# Bidirectional overrides and BOM can hide text in reports and the dashboard.
_BIDI = re.compile("[\u202a-\u202e\u2066-\u2069\u200e\u200f\ufeff]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_SECRET = re.compile(
    r"(?i)(bearer\s+[a-z0-9\-_~\+/]+=*"
    r"|\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*\S+"
    r"|\bsk-[A-Za-z0-9]{8,}\b"
    r"|\bQA_LLM_API_KEY=\S+)"
)


def sanitize_text(value: object, limit: int = 2000) -> str:
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\x00", "")
    text = _BIDI.sub("", text)
    text = _CONTROL.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def normalize_label(value: object, limit: int = 300) -> str:
    text = sanitize_text(value, limit)
    return re.sub(r"\s+", " ", text).strip()


def redact(value: object, limit: int = 2000) -> str:
    return _SECRET.sub("[redacted]", sanitize_text(value, limit))


def markdown_text(value: object, limit: int = 2000) -> str:
    """Neutralise HTML in Markdown that a browser might later render."""
    text = redact(value, limit)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
