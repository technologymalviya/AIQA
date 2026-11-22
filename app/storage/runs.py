"""One directory per run. Identifiers are checked before they become paths."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.models import RunRequest
from app.security.paths import ARTEFACT_FILES, require_run_id, safe_child
from app.security.text import redact


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class RunStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def directory(self, run_id: str) -> Path:
        path = safe_child(self.root, require_run_id(run_id))
        if not path.is_dir():
            raise FileNotFoundError(run_id)
        return path

    def create(self, request: RunRequest) -> dict:
        run_id = uuid.uuid4().hex
        path = safe_child(self.root, run_id)
        path.mkdir(parents=False)
        stored = request.model_dump(mode="json")
        stored["requirements_text"] = redact(stored.get("requirements_text", ""), 20000)
        record = {
            "run_id": run_id,
            "status": "queued",
            "phase": "created",
            "request": stored,
            "error": "",
            "cancel_requested": False,
            "created_at": _now(),
            "updated_at": _now(),
            "summary": {},
        }
        self._write(run_id, record)
        self.event(run_id, "run_start", {"mode": request.mode, "target_url": request.target_url})
        return record

    def load(self, run_id: str) -> dict:
        path = safe_child(self.directory(run_id), "run.json")
        return json.loads(path.read_text(encoding="utf-8"))

    def update(self, run_id: str, **fields: object) -> dict:
        with self._lock:
            record = self.load(run_id)
            record.update(fields)
            record["updated_at"] = _now()
            self._write(run_id, record)
            return record

    def event(self, run_id: str, kind: str, detail: dict) -> None:
        path = safe_child(self.directory(run_id), "events.jsonl")
        line = json.dumps({"ts": _now(), "kind": kind, "detail": detail}, ensure_ascii=True)
        with self._lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def events(self, run_id: str) -> list[dict]:
        path = safe_child(self.directory(run_id), "events.jsonl")
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def request_cancel(self, run_id: str) -> dict:
        self.event(run_id, "cancel_requested", {})
        return self.update(run_id, cancel_requested=True)

    def cancelled(self, run_id: str) -> bool:
        return bool(self.load(run_id).get("cancel_requested"))

    def list_runs(self) -> list[dict]:
        found = []
        for child in self.root.iterdir():
            if not child.is_dir():
                continue
            try:
                require_run_id(child.name)
                found.append(self.load(child.name))
            except (ValueError, FileNotFoundError, json.JSONDecodeError, OSError):
                continue
        found.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return found

    def remember_idempotency(self, key: str, run_id: str, body_hash: str) -> None:
        path = self.root / "idempotency.json"
        with self._lock:
            current = {}
            if path.exists():
                current = json.loads(path.read_text(encoding="utf-8"))
            current[key] = {"run_id": run_id, "body_hash": body_hash}
            path.write_text(json.dumps(current), encoding="utf-8")

    def idempotent_run(self, key: str, body_hash: str) -> dict | None:
        path = self.root / "idempotency.json"
        if not path.exists():
            return None
        current = json.loads(path.read_text(encoding="utf-8"))
        found = current.get(key)
        if not found:
            return None
        if found.get("body_hash") != body_hash:
            raise ValueError("idempotency key was reused with a different request")
        return self.load(found["run_id"])

    def read_artefact(self, run_id: str, name: str) -> Path:
        if name not in ARTEFACT_FILES:
            raise ValueError("artefact is not available")
        path = safe_child(self.directory(run_id), name)
        if not path.is_file():
            raise FileNotFoundError(name)
        return path

    def _write(self, run_id: str, record: dict) -> None:
        path = safe_child(safe_child(self.root, require_run_id(run_id)), "run.json")
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
