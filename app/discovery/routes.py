"""Public routes named by the app's own script.

The crawl used to follow only <a href>. A single-page app often navigates with
history.pushState and path checks instead, so those screens were never opened.
This reader pulls those paths out of script text. It does not execute the script.
"""

from __future__ import annotations

import re

# Paths joined with || are aliases of one screen. Visit the shortest one.
_CHAIN = re.compile(
    r"""(?:[A-Za-z0-9_.]*===?\s*[`'"]/(?:[a-z][a-z0-9-]{1,40}(?:/[a-z][a-z0-9-]{1,40}){0,3})[`'"]\s*\|\|\s*){1,6}"""
    r"""[A-Za-z0-9_.]*===?\s*[`'"]/(?:[a-z][a-z0-9-]{1,40}(?:/[a-z][a-z0-9-]{1,40}){0,3})[`'"]"""
)
_PATH = re.compile(r"""[`'"](/(?:[a-z][a-z0-9-]{1,40})(?:/[a-z][a-z0-9-]{1,40}){0,3})[`'"]""")
# A path is accepted only when the script compares it or passes it to pushState.
_NAV = re.compile(
    r"""(?:pushState\([^)\n]{0,180}?|[!=]==)\s*[`'"]"""
    r"""(/(?:[a-z][a-z0-9-]{1,40})(?:/[a-z][a-z0-9-]{1,40}){0,3})[`'"]"""
)
# GET of these paths can delete, pay, or send. They are recorded, not opened.
_QUIET = (
    "delete",
    "logout",
    "log-out",
    "signout",
    "sign-out",
    "checkout",
    "payment",
    "purchase",
    "transfer",
    "admin",
    "send",
    "whatsapp",
    "message",
    "approve",
    "reject",
)


def routes_from_script(source: str) -> tuple[list[str], list[str]]:
    """Return (public paths, skipped paths) from one script body."""
    if not source:
        return [], []
    text = source[:3_000_000]
    keep: list[str] = []
    skip: list[str] = []
    seen: set[str] = set()
    for path in _NAV.findall(text):
        if path in seen or path.startswith("/api/") or path == "/api":
            continue
        seen.add(path)
        lowered = path.lower()
        if any(part in lowered for part in _QUIET):
            skip.append(path)
            continue
        keep.append(path)
    drop = _alias_paths(text)
    keep = [path for path in keep if path not in drop]
    return keep, skip


def _alias_paths(source: str) -> set[str]:
    drop: set[str] = set()
    for chain in _CHAIN.findall(source):
        paths = _PATH.findall(chain)
        if len(paths) < 2:
            continue
        keep = min(paths, key=len)
        drop.update(path for path in paths if path != keep)
    return drop
