"""Phase 0 pipeline. Verdicts are computed after execution, never taken from a model."""

from __future__ import annotations

import asyncio
import platform
import time
from importlib.metadata import version

from app import __version__
from app.config import Settings
from app.discovery.crawler import crawl
from app.execution.runner import execute_case, launch_browser
from app.generation.design import design_suite
from app.models import CaseResult, CostLedger, TestCase, Verdict
from app.reporting.writer import (
    measured_bytes,
    write_cases,
    write_cost,
    write_defects,
    write_discovery,
    write_junit,
    write_open_questions,
    write_results,
    write_traceability,
    write_understanding,
)
from app.security.paths import safe_child
from app.security.text import redact
from app.security.url_policy import origin_of
from app.storage.runs import RunStore

_RUN_LOCK = asyncio.Lock()


async def execute_run(store: RunStore, run_id: str, settings: Settings, llm_complete=None) -> None:
    async with _RUN_LOCK:
        await _execute(store, run_id, settings, llm_complete)


async def _execute(store: RunStore, run_id: str, settings: Settings, llm_complete) -> None:
    started = time.monotonic()
    manager = None
    browser = None
    try:
        record = store.load(run_id)
        request = record["request"]
        store.update(run_id, status="running", phase="intake")
        origin = origin_of(request["target_url"])
        store.event(
            run_id,
            "scope",
            {"origin": origin.origin, "max_pages": request["max_pages"], "mode": request["mode"]},
        )
        store.update(run_id, phase="explore")
        manager, browser = await launch_browser(settings)
        run_dir = store.directory(run_id)
        publish = _frame_publisher(store, run_id, run_dir)
        discovery = await crawl(
            browser,
            request["target_url"],
            max_pages=int(request["max_pages"]),
            max_depth=int(request["max_depth"]),
            navigation_timeout_ms=settings.navigation_timeout_ms,
            cancel_check=lambda: store.cancelled(run_id),
            on_screen=publish,
        )
        write_discovery(run_dir, discovery)
        store.event(run_id, "tool", {"name": "crawl", "pages": len(discovery.pages)})
        store.update(run_id, phase="design")
        generated = await design_suite(
            discovery,
            mode=request["mode"],
            requirements_text=request.get("requirements_text", ""),
            settings=settings,
            llm_complete=llm_complete,
        )
        generated.open_questions.extend(discovery.coverage_notes)
        generated.open_questions.append(
            "Signed-in screens stay closed. This run does not log in, submit a form, or send a message."
        )
        for page in discovery.pages:
            if page.redirect_escape:
                generated.open_questions.append(
                    f"{page.url} redirected outside the authorized origin. Body text was discarded."
                )
        planner = generated.fallback_reason or (
            f"{generated.model or settings.llm_model} planned {len(generated.cases)} checks"
        )
        store.event(
            run_id,
            "design",
            {"provenance": generated.provenance.value, "cases": len(generated.cases), "note": planner[:300]},
        )
        environment = _environment(settings, request["target_url"], discovery)
        store.update(run_id, phase="execute")
        allowed = {page.url for page in discovery.pages if not page.redirect_escape}
        evidence_root = run_dir / "evidence"
        results: list[CaseResult] = []
        deadline = started + settings.run_timeout_s
        for case in generated.cases:
            if store.cancelled(run_id):
                results.append(_stopped(case, "cancelled before execution", environment))
                continue
            if time.monotonic() > deadline:
                results.append(_stopped(case, "run budget exhausted before execution", environment))
                continue
            outcome = await execute_case(
                browser,
                case,
                origin=origin,
                allowed_urls=allowed,
                evidence_root=evidence_root,
                settings=settings,
                environment=environment,
                on_screen=publish,
            )
            results.append(outcome)
            store.event(
                run_id,
                "verdict",
                {
                    "test_case_id": outcome.test_case_id,
                    "verdict": outcome.verdict.value,
                    "classification": outcome.classification.value,
                },
            )
        store.update(run_id, phase="report")
        _publish(run_dir, discovery, generated, results, environment, request["mode"], started)
        counts = {verdict.value: sum(1 for item in results if item.verdict == verdict) for verdict in Verdict}
        store.update(
            run_id,
            status="completed",
            phase="completed",
            summary=counts,
            error="",
        )
        store.event(run_id, "completed", counts)
    except Exception as exc:
        store.update(run_id, status="failed", phase="failed", error=redact(exc, 500))
        store.event(run_id, "failed", {"error": redact(exc, 300)})
    finally:
        if browser is not None:
            await browser.close()
        if manager is not None:
            await manager.stop()


def _frame_publisher(store: RunStore, run_id: str, run_dir):
    """Overwrite one live frame so the run page can show the browser."""

    def publish(data: bytes, caption: str) -> None:
        if not data.startswith(b"\x89PNG") or len(data) > 2_000_000:
            return
        dest = safe_child(run_dir, "live", "frame.png")
        dest.parent.mkdir(parents=True, exist_ok=True)
        temporary = safe_child(run_dir, "live", "frame.png.tmp")
        temporary.write_bytes(data)
        temporary.replace(dest)
        store.event(run_id, "screen", {"caption": redact(caption, 180)})

    return publish


def _stopped(case: TestCase, reason: str, environment: dict[str, str]) -> CaseResult:
    stopped = case.model_copy(update={"blocked_reason": reason})
    from app.execution.verdict import judge

    verdict, classification, kind, text = judge([], blocked_reason=reason)
    return CaseResult(
        test_case_id=stopped.test_case_id,
        title=stopped.title,
        provenance=stopped.provenance,
        verdict=verdict,
        classification=classification,
        failure_kind=kind,
        reason=text,
        environment=environment,
    )


def _environment(settings: Settings, target: str, discovery) -> dict[str, str]:
    app_version = next((page.app_version for page in discovery.pages if page.app_version), "")
    return {
        "pilot_version": __version__,
        "target_url": target,
        "browser": "chromium",
        "channel": settings.browser_channel or "bundled",
        "viewport": "1280x720",
        "locale": "en-GB",
        "timezone": "UTC",
        "headless": "true",
        "python": platform.python_version(),
        "os": platform.platform(),
        "playwright": version("playwright"),
        "app_version": app_version,
    }


def _publish(run_dir, discovery, generated, results, environment, mode, started: float) -> None:
    write_understanding(run_dir, discovery, mode)
    write_cases(run_dir, generated.cases)
    write_results(run_dir, results, environment)
    write_junit(run_dir, results)
    write_defects(run_dir, generated.cases, results, environment)
    write_traceability(run_dir, generated.requirements, results)
    write_open_questions(run_dir, generated.open_questions, results)
    sessions = sum(1 for item in results if item.steps)
    ledger = CostLedger(
        provider=generated.provider,
        model=generated.model,
        llm_calls=generated.llm_calls,
        input_tokens=generated.input_tokens,
        output_tokens=generated.output_tokens,
        crawl_pages=len(discovery.pages),
        browser_sessions=sessions,
        tool_calls=len(discovery.pages) + sessions + generated.llm_calls,
        wall_clock_seconds=time.monotonic() - started,
    )
    artefact_bytes, evidence_bytes = measured_bytes(run_dir)
    ledger.artefact_bytes = artefact_bytes
    ledger.evidence_bytes = evidence_bytes
    write_cost(run_dir, ledger)
