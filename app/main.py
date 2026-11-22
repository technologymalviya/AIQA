"""HTTP entrypoint for the Phase 0 dashboard and API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app import __version__
from app.config import Settings, load_settings
from app.demo import demo_redirect, empty_favicon, router as demo_router
from app.models import RunRequest
from app.orchestrator import execute_run
from app.security.paths import DEFECT_NAME_RE, EVIDENCE_FILES, require_case_id, safe_child
from app.security.url_policy import ScopeError, check_target
from app.storage.runs import RunStore

_APP_DIR = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_APP_DIR / "templates"))
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
_ARTEFACTS = (
    "application-understanding.md",
    "test-cases.md",
    "results.md",
    "cost-ledger.md",
    "open-questions.md",
    "traceability.md",
    "junit.xml",
    "discovery.json",
    "test-cases.json",
    "results.json",
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="Autonomous QA Orchestrator", version=__version__)
    app.state.settings = settings
    app.state.store = RunStore(settings.runs_dir)
    app.mount("/static", StaticFiles(directory=str(_APP_DIR / "static")), name="static")
    app.include_router(demo_router)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        # The demo fixture runs its own inline script. The dashboard does not.
        if not request.url.path.startswith("/demo"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' data:; media-src 'self'; "
                "style-src 'self'; script-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'"
            )
        return response

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "healthy", "service": "qa-orchestrator", "phase": 0, "version": __version__}

    @app.get("/api/v1/capabilities")
    def capabilities() -> dict:
        return {
            "phase": 0,
            "implemented": ["web-chromium", "auto", "hybrid", "heuristic-fallback", "filesystem-artefacts"],
            "not_implemented": [
                {"id": item, "status": "NOT_IMPLEMENTED"}
                for item in (
                    "android",
                    "ios",
                    "android-tv",
                    "tvos",
                    "postgres",
                    "redis",
                    "jira-publish",
                    "github-publish",
                    "rbac",
                )
            ],
        }

    @app.post("/api/v1/defects/{defect_id}/approve-publish")
    def publish_defect(defect_id: str) -> JSONResponse:
        del defect_id
        return JSONResponse(
            status_code=501,
            content={"status": "NOT_IMPLEMENTED", "message": "Jira and GitHub publishing are not part of Phase 0."},
        )

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, error: str = ""):
        return _TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {"runs": app.state.store.list_runs(), "error": error},
        )

    @app.post("/runs")
    async def start_from_form(request: Request):
        form = await request.form()
        confirmed = form.get("authorization_confirmed") == "true"
        text = str(form.get("requirements_text") or "")
        raw_mode = str(form.get("mode") or "").strip().lower()
        if raw_mode not in {"auto", "hybrid"}:
            raw_mode = "hybrid" if text.strip() else "auto"
        raw_pages = str(form.get("max_pages") or "").strip()
        raw_depth = str(form.get("max_depth") or "").strip()
        try:
            payload = RunRequest(
                target_url=str(form.get("target_url") or ""),
                mode=raw_mode,  # type: ignore[arg-type]
                requirements_text=text,
                max_pages=int(raw_pages) if raw_pages else app.state.settings.max_pages_cap,
                max_depth=int(raw_depth) if raw_depth else 3,
                authorization_confirmed=confirmed,
            )
        except (ValidationError, ValueError) as exc:
            return _TEMPLATES.TemplateResponse(
                request,
                "dashboard.html",
                {"runs": app.state.store.list_runs(), "error": _safe_error(exc)},
                status_code=400,
            )
        try:
            record = _accept(app, payload, None)
        except (ScopeError, ValueError) as exc:
            return _TEMPLATES.TemplateResponse(
                request,
                "dashboard.html",
                {"runs": app.state.store.list_runs(), "error": _safe_error(exc)},
                status_code=400,
            )
        return RedirectResponse(url=f"/runs/{record['run_id']}", status_code=303)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: str):
        try:
            run = app.state.store.load(run_id)
        except (FileNotFoundError, ValueError):
            return HTMLResponse("Run not found", status_code=404)
        pages, results, cases = _optional_json(app.state.store, run_id)
        evidence = []
        for item in results:
            if item.get("verdict") not in {"FAIL", "INCONCLUSIVE"}:
                continue
            case_id = item.get("test_case_id", "")
            try:
                require_case_id(case_id)
            except ValueError:
                continue
            base = f"/api/v1/runs/{run_id}/evidence/{case_id}"
            evidence.append(
                {
                    "case_id": case_id,
                    "video": f"{base}/video.webm",
                    "screenshot": f"{base}/screenshot.png",
                    "trace": f"{base}/trace.zip",
                }
            )
        defects = []
        defect_dir = app.state.store.directory(run_id) / "defects"
        if defect_dir.is_dir():
            defects = sorted(path.name for path in defect_dir.glob("DEF-*.md") if DEFECT_NAME_RE.fullmatch(path.name))
        present = []
        for name in _ARTEFACTS:
            try:
                app.state.store.read_artefact(run_id, name)
            except (FileNotFoundError, ValueError):
                continue
            present.append(name)
        response = _TEMPLATES.TemplateResponse(
            request,
            "run.html",
            {
                "run": run,
                "pages": pages,
                "cases": cases,
                "results": results,
                "evidence": evidence,
                "artefacts": present,
                "defects": defects,
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/v1/runs")
    async def create_run(payload: RunRequest, request: Request):
        key = request.headers.get("idempotency-key", "").strip()
        if key and not _IDEMPOTENCY.fullmatch(key):
            return _error(400, "invalid_idempotency_key", "Idempotency-Key is not valid")
        body_hash = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
        if key:
            try:
                existing = app.state.store.idempotent_run(key, body_hash)
            except ValueError as exc:
                return _error(409, "idempotency_conflict", _safe_error(exc))
            if existing:
                return JSONResponse(status_code=200, content=_public_run(existing))
        try:
            record = _accept(app, payload, (key, body_hash) if key else None)
        except ScopeError as exc:
            return _error(400, "invalid_scope", _safe_error(exc))
        return JSONResponse(status_code=202, content=_public_run(record))

    @app.get("/api/v1/runs/{run_id}")
    def get_run(run_id: str):
        try:
            return _public_run(app.state.store.load(run_id))
        except (FileNotFoundError, ValueError):
            return _error(404, "not_found", "Run not found")

    @app.get("/api/v1/runs/{run_id}/results")
    def get_results(run_id: str):
        try:
            path = app.state.store.read_artefact(run_id, "results.json")
        except (FileNotFoundError, ValueError):
            return _error(404, "not_found", "Results are not ready")
        return JSONResponse(json.loads(path.read_text(encoding="utf-8")))

    @app.get("/api/v1/runs/{run_id}/activity")
    def activity(run_id: str):
        try:
            run = app.state.store.load(run_id)
        except (FileNotFoundError, ValueError):
            return _error(404, "not_found", "Run not found")
        rows = []
        for row in app.state.store.events(run_id)[-80:]:
            rows.append(
                {
                    "ts": row.get("ts", ""),
                    "kind": row.get("kind", ""),
                    "caption": _activity_caption(row),
                }
            )
        frame = ""
        try:
            path = safe_child(app.state.store.directory(run_id), "live", "frame.png")
        except (ValueError, FileNotFoundError):
            path = None
        if path is not None and path.is_file():
            frame = f"/api/v1/runs/{run_id}/live/frame.png"
        return {
            "status": run["status"],
            "phase": run["phase"],
            "error": run.get("error", ""),
            "summary": run.get("summary") or {},
            "events": rows,
            "frame": frame,
        }

    @app.get("/api/v1/runs/{run_id}/live/frame.png")
    def live_frame(run_id: str):
        try:
            path = safe_child(app.state.store.directory(run_id), "live", "frame.png")
        except (ValueError, FileNotFoundError):
            return _error(404, "not_found", "Live frame is not available")
        if not path.is_file():
            return _error(404, "not_found", "Live frame is not available")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/runs/{run_id}/events")
    async def events(run_id: str):
        try:
            app.state.store.load(run_id)
        except (FileNotFoundError, ValueError):
            return _error(404, "not_found", "Run not found")

        async def stream():
            sent = 0
            for _ in range(240):
                rows = app.state.store.events(run_id)
                for row in rows[sent:]:
                    yield f"data: {json.dumps(row)}\n\n"
                sent = len(rows)
                status = app.state.store.load(run_id)["status"]
                if status in {"completed", "failed", "cancelled"} and sent >= len(app.state.store.events(run_id)):
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/api/v1/runs/{run_id}/cancel")
    def cancel(run_id: str, request: Request):
        try:
            record = app.state.store.request_cancel(run_id)
        except (FileNotFoundError, ValueError):
            return _error(404, "not_found", "Run not found")
        if "text/html" in request.headers.get("accept", "") or request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            return RedirectResponse(url=f"/runs/{run_id}", status_code=303)
        return _public_run(record)

    @app.get("/api/v1/runs/{run_id}/artefacts/{name}")
    def artefact(run_id: str, name: str):
        try:
            path = app.state.store.read_artefact(run_id, name)
        except ValueError:
            return _error(404, "not_found", "Artefact is not available")
        except FileNotFoundError:
            return _error(404, "not_found", "Artefact is not ready")
        media = "application/json" if name.endswith(".json") else "text/plain; charset=utf-8"
        if name.endswith(".md"):
            media = "text/markdown; charset=utf-8"
        if name.endswith(".xml"):
            media = "application/xml"
        return FileResponse(path, media_type=media, filename=name)

    @app.get("/api/v1/runs/{run_id}/defects/{name}")
    def defect(run_id: str, name: str):
        if not DEFECT_NAME_RE.fullmatch(name):
            return _error(404, "not_found", "Defect draft is not available")
        try:
            path = safe_child(app.state.store.directory(run_id) / "defects", name)
        except (ValueError, FileNotFoundError):
            return _error(404, "not_found", "Defect draft is not available")
        if not path.is_file():
            return _error(404, "not_found", "Defect draft is not available")
        return FileResponse(path, media_type="text/markdown; charset=utf-8", filename=name)

    @app.get("/api/v1/runs/{run_id}/evidence/{case_id}/{name}")
    def evidence(run_id: str, case_id: str, name: str):
        try:
            require_case_id(case_id)
            if name not in EVIDENCE_FILES:
                raise ValueError(name)
            path = safe_child(app.state.store.directory(run_id) / "evidence", case_id, name)
        except (ValueError, FileNotFoundError):
            return _error(404, "not_found", "Evidence is not available")
        if not path.is_file():
            return _error(404, "not_found", "Evidence is not available")
        types = {"video.webm": "video/webm", "screenshot.png": "image/png", "trace.zip": "application/zip"}
        return FileResponse(path, media_type=types[name], filename=name)

    @app.get("/demo")
    def demo_slash():
        return demo_redirect()

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return empty_favicon()

    return app


def _accept(app: FastAPI, payload: RunRequest, idempotency: tuple[str, str] | None) -> dict:
    canonical = check_target(payload.target_url)
    capped = min(payload.max_pages, app.state.settings.max_pages_cap)
    payload = payload.model_copy(update={"target_url": canonical, "max_pages": capped})
    record = app.state.store.create(payload)
    if idempotency:
        app.state.store.remember_idempotency(idempotency[0], record["run_id"], idempotency[1])
    asyncio.create_task(execute_run(app.state.store, record["run_id"], app.state.settings))
    return record


def _public_run(record: dict) -> dict:
    request = dict(record.get("request") or {})
    request.pop("requirements_text", None)
    return {
        "run_id": record["run_id"],
        "status": record["status"],
        "phase": record["phase"],
        "error": record.get("error", ""),
        "summary": record.get("summary") or {},
        "request": request,
        "created_at": record.get("created_at"),
    }


def _optional_json(store: RunStore, run_id: str) -> tuple[list, list, list]:
    pages: list = []
    results: list = []
    cases: list = []
    try:
        discovery = json.loads(store.read_artefact(run_id, "discovery.json").read_text(encoding="utf-8"))
        pages = discovery.get("pages") or []
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        pages = []
    try:
        payload = json.loads(store.read_artefact(run_id, "results.json").read_text(encoding="utf-8"))
        results = payload.get("results") or []
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        results = []
    try:
        payload = json.loads(store.read_artefact(run_id, "test-cases.json").read_text(encoding="utf-8"))
        cases = payload if isinstance(payload, list) else []
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        cases = []
    return pages, results, cases


def _activity_caption(row: dict) -> str:
    detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    kind = str(row.get("kind") or "")
    if detail.get("caption"):
        return str(detail["caption"])[:180]
    if kind == "scope":
        return f"Staying on {detail.get('origin', '')}. Read-only, this site only."
    if kind == "tool":
        return f"Finished opening screens ({detail.get('pages', 0)})"
    if kind == "design":
        return str(detail.get("note") or "Planned the checks")[:300]
    if kind == "mind":
        return str(detail.get("note") or "Decided the next step")[:300]
    if kind == "verdict":
        return f"{detail.get('verdict', '')} {detail.get('test_case_id', '')}".strip()
    if kind == "completed":
        return "Run finished"
    if kind == "failed":
        return "Run failed"
    if kind == "run_start":
        return "Run accepted"
    return kind


def _safe_error(exc: Exception) -> str:
    text = str(exc)
    if "\n" in text:
        text = text.splitlines()[0]
    return text[:300]


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


app = create_app()
