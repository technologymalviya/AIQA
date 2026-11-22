"""Phase 0 pipeline. Verdicts are computed after execution, never taken from a model."""

from __future__ import annotations

import asyncio
import json
import platform
import time
from importlib.metadata import version

from pydantic import ValidationError

from app import __version__
from app.config import Settings
from app.discovery.crawler import crawl
from app.execution.agent import run_generated_cases
from app.execution.runner import launch_browser
from app.generation.design import design_suite
from app.generation.heuristic import case_id
from app.mind.advisor import advise, apply_advice, choose_nav
from app.mind.follow import session_seconds
from app.mind.hands import look_around
from app.mind.journey import run_journey
from app.mind.memory import load_memory, save_memory, scrub
from app.mind.plan import (
    decide,
    goal_allows_account,
    goal_refuses_account,
    goal_wants_end_to_end,
    goal_wants_fresh_account,
    pages_for_focus,
)
from app.models import CaseResult, CostLedger, Discovery, TestCase, Verdict
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


def queue_continuation(store: RunStore, run_id: str, prompt: str) -> dict:
    """Keep this run and carry out another prompt when the current one has finished."""
    text = redact(prompt, 20000).strip()
    if not text:
        raise ValueError("Write a prompt to continue this session.")
    record = store.load(run_id)
    if record["status"] in {"queued", "running"}:
        raise RuntimeError("This session is still running.")
    if record["status"] not in {"completed", "failed", "cancelled"}:
        raise RuntimeError("This session cannot continue yet.")
    request = dict(record.get("request") or {})
    previous = str(request.get("requirements_text") or "")
    request["requirements_text"] = text
    prompts = [item for item in (record.get("prompts") or []) if isinstance(item, str)]
    if previous and previous not in prompts:
        prompts.append(previous)
    prompts.append(text)
    return store.update(
        run_id,
        status="queued",
        phase="continue",
        error="",
        cancel_requested=False,
        request=request,
        prompts=prompts[-12:],
    )


async def continue_run(store: RunStore, run_id: str, settings: Settings, llm_complete=None) -> None:
    async with _RUN_LOCK:
        record = store.load(run_id)
        discovery = _saved_discovery(store.directory(run_id))
        if discovery is None or not discovery.pages:
            await _execute(store, run_id, settings, llm_complete)
            return
        await _continue(store, run_id, settings, record, discovery, llm_complete)


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
        memory = load_memory(settings.runs_dir, origin.origin)
        decision = decide(discovery.pages, memory)
        goal = request.get("requirements_text", "")
        payload, _source = await advise(settings, discovery.pages, memory, goal)
        decision = apply_advice(decision, payload, memory, goal)
        mind_note = decision.summary
        if decision.mind.startswith("huggingface"):
            used = decision.mind.split(":", 1)[-1]
            mind_note = f"Hugging Face {used}: {decision.summary}"
        store.event(run_id, "mind", {"note": redact(scrub(mind_note, memory), 300)})
        source_pages = decision.unique_pages or discovery.pages
        focused_pages = source_pages if goal_wants_end_to_end(goal) else pages_for_focus(source_pages, decision.focus)
        focused = discovery.model_copy(update={"pages": focused_pages})
        generated = await design_suite(
            focused,
            mode=request["mode"],
            requirements_text=request.get("requirements_text", ""),
            settings=settings,
            llm_complete=llm_complete,
        )
        generated.open_questions.extend(discovery.coverage_notes)
        generated.open_questions.append(decision.summary)
        for page in discovery.pages:
            if page.redirect_escape:
                generated.open_questions.append(
                    f"{page.url} redirected outside the authorized origin. Body text was discarded."
                )
        if generated.provenance.value == "llm":
            planner = (
                f"{generated.model or settings.llm_model} wrote {len(generated.cases)} test cases "
                "from the crawled screens."
            )
        else:
            planner = generated.fallback_reason or (
                f"{generated.model or settings.llm_model} planned {len(generated.cases)} checks"
            )
        store.event(
            run_id,
            "design",
            {"provenance": generated.provenance.value, "cases": len(generated.cases), "note": planner[:300]},
        )
        for case in generated.cases:
            if case.blocked_reason:
                continue
            store.event(
                run_id,
                "mind",
                {"note": redact(scrub(f"Test case {case.test_case_id}: {case.title}. {case.summary}", memory), 300)},
            )
        environment = _environment(settings, request["target_url"], discovery)
        store.update(run_id, phase="execute")

        def _note(message: str) -> None:
            store.event(run_id, "mind", {"note": redact(scrub(message, memory), 180)})

        journey_results, _handled, memory = await run_journey(
            browser,
            decision=decision,
            memory=memory,
            settings=settings,
            environment=environment,
            on_screen=publish,
            on_note=_note,
            cancel_check=lambda: store.cancelled(run_id),
            choose_nav=(lambda labels, checked: choose_nav(settings, labels, checked))
            if settings.hf_token or settings.hf_local
            else None,
            goal=request.get("requirements_text", ""),
        )
        save_memory(settings.runs_dir, memory)
        allowed = {page.url for page in discovery.pages if not page.redirect_escape}
        evidence_root = run_dir / "evidence"
        results: list[CaseResult] = list(journey_results)
        asked = request.get("requirements_text", "").strip()
        followed_request = bool(asked) and goal_allows_account(asked)
        if (
            not followed_request
            and not store.cancelled(run_id)
            and time.monotonic() < started + settings.run_timeout_s
        ):
            looked = await look_around(
                browser,
                start_url=request["target_url"],
                origin=origin.origin,
                settings=settings,
                environment=environment,
                on_screen=publish,
                on_note=_note,
                cancel_check=lambda: store.cancelled(run_id),
                choose=(lambda labels, checked: choose_nav(settings, labels, checked))
                if settings.hf_token or settings.hf_local
                else None,
                goal=request.get("requirements_text", ""),
            )
            results.extend(looked)
        deadline = started + session_seconds(request.get("requirements_text", ""), settings.run_timeout_s)
        ran = await run_generated_cases(
            browser,
            generated.cases,
            origin=origin,
            allowed_urls=allowed,
            evidence_root=evidence_root,
            settings=settings,
            environment=environment,
            deadline=deadline,
            cancelled=lambda: store.cancelled(run_id),
            on_screen=publish,
            on_note=_note,
            on_verdict=lambda outcome: store.event(
                run_id,
                "verdict",
                {
                    "test_case_id": outcome.test_case_id,
                    "verdict": outcome.verdict.value,
                    "classification": outcome.classification.value,
                },
            ),
        )
        results.extend(ran)
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


async def _continue(store: RunStore, run_id: str, settings: Settings, record: dict, discovery: Discovery, llm_complete) -> None:
    """Follow a new prompt in the session that already knows this site."""
    started = time.monotonic()
    manager = None
    browser = None
    try:
        request = record["request"]
        prompt = str(request.get("requirements_text") or "")
        store.update(run_id, status="running", phase="execute", error="")
        store.event(
            run_id,
            "mind",
            {"note": redact(f"Continuing this session. Next prompt: {prompt}", 300)},
        )
        origin = origin_of(request["target_url"])
        run_dir = store.directory(run_id)
        publish = _frame_publisher(store, run_id, run_dir)
        manager, browser = await launch_browser(settings)
        memory = load_memory(settings.runs_dir, origin.origin)
        decision = decide(discovery.pages, memory)
        payload, _source = await advise(settings, discovery.pages, memory, prompt)
        decision = apply_advice(decision, payload, memory, prompt)
        decision = _keep_signed_in(decision, memory, prompt)
        mind_note = decision.summary
        if decision.mind.startswith("huggingface"):
            used = decision.mind.split(":", 1)[-1]
            mind_note = f"Hugging Face {used}: {decision.summary}"
        store.event(run_id, "mind", {"note": redact(scrub(mind_note, memory), 300)})
        environment = _environment(settings, request["target_url"], discovery)

        def _note(message: str) -> None:
            store.event(run_id, "mind", {"note": redact(scrub(message, memory), 180)})

        journey_results, _handled, memory = await run_journey(
            browser,
            decision=decision,
            memory=memory,
            settings=settings,
            environment=environment,
            on_screen=publish,
            on_note=_note,
            cancel_check=lambda: store.cancelled(run_id),
            choose_nav=(lambda labels, checked: choose_nav(settings, labels, checked))
            if settings.hf_token or settings.hf_local
            else None,
            goal=prompt,
        )
        save_memory(settings.runs_dir, memory)
        fresh: list[CaseResult] = []
        for index, item in enumerate(journey_results):
            item.test_case_id = case_id(origin.origin, "continue", prompt[:80], str(index))
            item.title = "Continue: " + redact(scrub(prompt, memory), 80)
            fresh.append(item)
        inside = decision.sign_in or decision.create_account
        if (
            not inside
            and not store.cancelled(run_id)
            and time.monotonic() < started + settings.run_timeout_s
        ):
            looked = await look_around(
                browser,
                start_url=request["target_url"],
                origin=origin.origin,
                settings=settings,
                environment=environment,
                on_screen=publish,
                on_note=_note,
                cancel_check=lambda: store.cancelled(run_id),
                choose=(lambda labels, checked: choose_nav(settings, labels, checked))
                if settings.hf_token or settings.hf_local
                else None,
                goal=prompt,
            )
            fresh.extend(looked)
        if goal_wants_end_to_end(prompt) and not store.cancelled(run_id):
            generated = await design_suite(
                discovery,
                mode=request.get("mode") or "hybrid",
                requirements_text=prompt,
                settings=settings,
                llm_complete=llm_complete,
            )
            kept = _saved_cases(run_dir)
            write_cases(run_dir, kept + generated.cases)
            for case in generated.cases:
                if case.blocked_reason:
                    continue
                store.event(
                    run_id,
                    "mind",
                    {"note": redact(scrub(f"Test case {case.test_case_id}: {case.title}. {case.summary}", memory), 300)},
                )
            deadline = started + session_seconds(prompt, settings.run_timeout_s)
            ran = await run_generated_cases(
                browser,
                generated.cases,
                origin=origin,
                allowed_urls={page.url for page in discovery.pages if not page.redirect_escape},
                evidence_root=run_dir / "evidence",
                settings=settings,
                environment=environment,
                deadline=deadline,
                cancelled=lambda: store.cancelled(run_id),
                on_screen=publish,
                on_note=_note,
                on_verdict=lambda outcome: store.event(
                    run_id,
                    "verdict",
                    {
                        "test_case_id": outcome.test_case_id,
                        "verdict": outcome.verdict.value,
                        "classification": outcome.classification.value,
                    },
                ),
            )
            fresh.extend(ran)
        results = _saved_results(run_dir) + fresh
        write_results(run_dir, results, environment)
        write_junit(run_dir, results)
        counts = {verdict.value: sum(1 for item in results if item.verdict == verdict) for verdict in Verdict}
        store.update(run_id, status="completed", phase="completed", summary=counts, error="")
        store.event(run_id, "completed", counts)
    except Exception as exc:
        store.update(run_id, status="failed", phase="failed", error=redact(exc, 500))
        store.event(run_id, "failed", {"error": redact(exc, 300)})
    finally:
        if browser is not None:
            await browser.close()
        if manager is not None:
            await manager.stop()


def _keep_signed_in(decision, memory: dict, prompt: str):
    """A follow-up prompt stays in the account this session already opened."""
    if (
        memory.get("signup_done")
        and memory.get("username")
        and not goal_wants_fresh_account(prompt)
        and not goal_refuses_account(prompt)
    ):
        decision.sign_in = True
        decision.create_account = False
    return decision


def _saved_discovery(run_dir) -> Discovery | None:
    path = run_dir / "discovery.json"
    if not path.is_file():
        return None
    try:
        return Discovery.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValidationError):
        return None


def _saved_results(run_dir) -> list[CaseResult]:
    path = run_dir / "results.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    found: list[CaseResult] = []
    for row in payload.get("results") or []:
        try:
            found.append(CaseResult.model_validate(row))
        except ValidationError:
            continue
    return found


def _saved_cases(run_dir) -> list[TestCase]:
    path = run_dir / "test-cases.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    found: list[TestCase] = []
    for row in payload:
        try:
            found.append(TestCase.model_validate(row))
        except ValidationError:
            continue
    return found


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
