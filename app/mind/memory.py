"""Remember one account and the screens already walked for an origin.

The password stays in this file. Reports and events must not copy it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _digest(origin: str) -> str:
    return hashlib.sha256(origin.encode("utf-8")).hexdigest()[:20]


def memory_file(runs_dir: Path, origin: str) -> Path:
    return runs_dir / "memory" / f"{_digest(origin)}.json"


def storage_file(runs_dir: Path, origin: str) -> Path:
    return runs_dir / "memory" / f"{_digest(origin)}.storage.json"


def load_memory(runs_dir: Path, origin: str) -> dict:
    path = memory_file(runs_dir, origin)
    if not path.is_file():
        return _empty(origin)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty(origin)
    if not isinstance(data, dict) or data.get("origin") != origin:
        return _empty(origin)
    base = _empty(origin)
    base.update({key: data.get(key, base[key]) for key in base})
    return base


def save_memory(runs_dir: Path, memory: dict) -> None:
    path = memory_file(runs_dir, str(memory.get("origin") or ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(memory, indent=2) + "\n", encoding="utf-8")


def scrub(text: str, memory: dict) -> str:
    secret = str(memory.get("password") or "")
    if secret and secret in text:
        text = text.replace(secret, "[redacted]")
    return text


def _empty(origin: str) -> dict:
    return {
        "origin": origin,
        "username": "",
        "password": "",
        "signup_done": False,
        "signin_done": False,
        "entered_url": "",
        "nav_checked": [],
    }
