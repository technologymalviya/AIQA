"""Case ids and filesystem paths. Model output never becomes a path segment."""

from __future__ import annotations

import re
from pathlib import Path

CASE_ID_RE = re.compile(r"TC-[A-F0-9]{8}")
RUN_ID_RE = re.compile(r"[0-9a-f]{32}")
DEFECT_NAME_RE = re.compile(r"DEF-TC-[A-F0-9]{8}\.md")
EVIDENCE_FILES = frozenset({"video.webm", "screenshot.png", "trace.zip"})
ARTEFACT_FILES = frozenset(
    {
        "application-understanding.md",
        "test-cases.md",
        "test-cases.json",
        "results.md",
        "results.json",
        "junit.xml",
        "cost-ledger.md",
        "open-questions.md",
        "traceability.md",
        "discovery.json",
        "run.json",
        "events.jsonl",
    }
)


class PathSafetyError(ValueError):
    pass


def require_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise PathSafetyError("run id is not a 32-character hex value")
    return run_id


def require_case_id(case_id: str) -> str:
    if not isinstance(case_id, str) or not CASE_ID_RE.fullmatch(case_id):
        raise PathSafetyError("case id is not TC- followed by 8 hex characters")
    return case_id


def _reject_part(part: str) -> None:
    if part in {"", ".", ".."}:
        raise PathSafetyError("path segment is empty or a traversal marker")
    if "\x00" in part or "/" in part or "\\" in part or "%" in part:
        raise PathSafetyError("path segment contains a forbidden character")


def safe_child(root: Path, *parts: str) -> Path:
    """Resolve root/parts and refuse anything that escapes root."""
    if not parts:
        raise PathSafetyError("path is empty")
    for part in parts:
        _reject_part(part)
    base = root.resolve()
    candidate = base.joinpath(*parts).resolve()
    if candidate != base and base not in candidate.parents:
        raise PathSafetyError("path escapes the run directory")
    return candidate
