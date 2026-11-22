"""Split hybrid requirements into stable ids. Destructive rules are not automated."""

from __future__ import annotations

import re

from app.models import Requirement
from app.security.text import redact

_LINE = re.compile(
    r"^(?P<id>(?:REQ|AC|US)-[A-Za-z0-9][A-Za-z0-9-]{0,30})\s*[:\-]\s*(?P<body>.+)$"
)
_DESTRUCTIVE = re.compile(
    r"(?i)\b(purchase|buy|pay|payment|checkout|delete|transfer|publish|send message|"
    r"change password|credit card)\b"
)
_URL = re.compile(r"https?://\S+")


def parse_requirements(text: str) -> list[Requirement]:
    cleaned = redact(text, 20000)
    items: list[Requirement] = []
    numbered: list[tuple[str, str]] = []
    loose: list[str] = []
    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _LINE.match(line)
        if match:
            numbered.append((match.group("id").upper(), match.group("body").strip()))
        else:
            loose.append(line)
    if numbered:
        source = numbered
    else:
        source = [(f"REQ-{index:03d}", line) for index, line in enumerate(loose, start=1)]
    seen: set[str] = set()
    for requirement_id, body in source:
        if requirement_id in seen:
            requirement_id = f"{requirement_id}-{len(seen)+1}"
        seen.add(requirement_id)
        items.append(
            Requirement(
                requirement_id=requirement_id[:40],
                text=body[:1000],
                ambiguous=len(body.split()) < 4,
                destructive=bool(_DESTRUCTIVE.search(body)),
            )
        )
    return items


def mentions_foreign_url(text: str, origin: str) -> bool:
    for match in _URL.findall(text):
        if not match.startswith(origin):
            return True
    return False
