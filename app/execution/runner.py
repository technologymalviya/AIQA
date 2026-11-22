"""Playwright Chromium executor for the constrained action language.

Only the six actions in policy.ALLOWED_ACTIONS are dispatched. There is no
evaluate, no shell, and no model-supplied script.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

from playwright.async_api import Browser, Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from app.config import Settings
from app.execution.policy import PolicyError, gate
from app.execution.screen import capture
from app.execution.verdict import judge, should_retain_media
from app.models import CaseResult, Provenance, StepResult, TestCase
from app.security.paths import require_case_id, safe_child
from app.security.text import normalize_label, redact
from app.security.url_policy import Origin, ScopeError, check_target, path_is_destructive


class RunnerError(Exception):
    pass


async def launch_browser(settings: Settings):
    manager = await async_playwright().start()
    kwargs: dict = {"headless": True, "args": ["--disable-dev-shm-usage"]}
    if settings.browser_channel:
        kwargs["channel"] = settings.browser_channel
    browser = await manager.chromium.launch(**kwargs)
    return manager, browser


async def execute_case(
    browser: Browser,
    case: TestCase,
    *,
    origin: Origin,
    allowed_urls: set[str],
    evidence_root: Path,
    settings: Settings,
    environment: dict[str, str],
    on_screen=None,
) -> CaseResult:
    started = time.monotonic()
    result = CaseResult(
        test_case_id=case.test_case_id,
        title=case.title,
        provenance=case.provenance if isinstance(case.provenance, Provenance) else Provenance(case.provenance),
        verdict="PASS",  # type: ignore[arg-type]
        classification="none",  # type: ignore[arg-type]
        environment=environment,
    )
    blocked = gate(case, origin, allowed_urls)
    if blocked:
        verdict, classification, kind, reason = judge([], blocked_reason=blocked)
        result.verdict = verdict
        result.classification = classification
        result.failure_kind = kind
        result.reason = reason
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result

    tmp = Path(tempfile.mkdtemp(prefix="qa-video-"))
    context = None
    try:
        context = await browser.new_context(
            record_video_dir=str(tmp),
            record_video_size={"width": 1280, "height": 720},
            viewport={"width": 1280, "height": 720},
            locale="en-GB",
            timezone_id="UTC",
            accept_downloads=False,
        )
        context.set_default_navigation_timeout(settings.navigation_timeout_ms)
        context.set_default_timeout(settings.action_timeout_ms)
        await context.tracing.start(screenshots=True, snapshots=True, sources=False)
        page = await context.new_page()
        video = page.video
        state: dict = {"http_status": None, "console_errors": [], "page_errors": []}

        def on_console(msg) -> None:
            if getattr(msg, "type", "") == "error":
                state["console_errors"].append(redact(msg.text, 300))

        def on_pageerror(exc) -> None:
            state["page_errors"].append(redact(exc, 300))

        page.on("console", on_console)
        page.on("pageerror", on_pageerror)
        await capture(page, on_screen, f"Running {case.title}")
        steps: list[StepResult] = []
        runner_error = ""
        policy_block = ""
        for step in case.steps:
            step_started = time.monotonic()
            try:
                outcome = await _dispatch(page, step, origin, allowed_urls, state)
            except PolicyError as exc:
                policy_block = redact(exc, 300)
                steps.append(
                    StepResult(
                        action=step.action,
                        passed=False,
                        expected=step.expected,
                        error=policy_block,
                        kind="policy",
                        duration_ms=int((time.monotonic() - step_started) * 1000),
                    )
                )
                break
            except PlaywrightTimeoutError:
                runner_error = "navigation timeout" if step.action == "goto" else "action timeout"
                steps.append(
                    StepResult(
                        action=step.action,
                        passed=False,
                        expected=step.expected,
                        error=runner_error,
                        kind="runner",
                        duration_ms=int((time.monotonic() - step_started) * 1000),
                    )
                )
                break
            except RunnerError as exc:
                runner_error = redact(exc, 300)
                steps.append(
                    StepResult(
                        action=step.action,
                        passed=False,
                        expected=step.expected,
                        error=runner_error,
                        kind="runner",
                        duration_ms=int((time.monotonic() - step_started) * 1000),
                    )
                )
                break
            except PlaywrightError as exc:
                runner_error = redact(exc, 300) or "browser error"
                steps.append(
                    StepResult(
                        action=step.action,
                        passed=False,
                        expected=step.expected,
                        error=runner_error,
                        kind="runner",
                        duration_ms=int((time.monotonic() - step_started) * 1000),
                    )
                )
                break
            outcome.duration_ms = int((time.monotonic() - step_started) * 1000)
            steps.append(outcome)
            mark = "passed" if outcome.passed else "failed"
            await capture(page, on_screen, f"{case.title}: {step.action} {mark}")
            if not outcome.passed:
                break
        verdict, classification, kind, reason = judge(
            steps,
            blocked_reason=policy_block,
            runner_error=runner_error,
        )
        result.verdict = verdict
        result.classification = classification
        result.failure_kind = kind
        result.reason = reason
        result.steps = steps
        retain = should_retain_media(verdict, retain_pass_media=settings.retain_pass_media)
        evidence: list[str] = []
        case_id = require_case_id(case.test_case_id)
        if retain:
            dest = safe_child(evidence_root, case_id)
            dest.mkdir(parents=True, exist_ok=True)
            try:
                await page.screenshot(path=str(dest / "screenshot.png"), full_page=False)
                evidence.append(f"evidence/{case_id}/screenshot.png")
            except PlaywrightError:
                pass
            try:
                await context.tracing.stop(path=str(dest / "trace.zip"))
                evidence.append(f"evidence/{case_id}/trace.zip")
            except PlaywrightError:
                pass
        else:
            try:
                await context.tracing.stop()
            except PlaywrightError:
                pass
        await context.close()
        context = None
        if video is not None:
            try:
                if retain:
                    await video.save_as(str(safe_child(evidence_root, case_id, "video.webm")))
                    evidence.append(f"evidence/{case_id}/video.webm")
                else:
                    await video.delete()
            except PlaywrightError:
                if not retain:
                    pass
                else:
                    result.reason = (result.reason + " Evidence video was not saved.").strip()
        result.evidence = evidence
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result
    finally:
        if context is not None:
            try:
                await context.close()
            except PlaywrightError:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


async def _dispatch(page, step, origin: Origin, allowed_urls: set[str], state: dict) -> StepResult:
    action = step.action
    if action == "goto":
        try:
            url = check_target(step.target, authorized=origin)
        except ScopeError as exc:
            raise PolicyError("goto is outside the authorized origin") from exc
        if path_is_destructive(url) or url not in allowed_urls:
            raise PolicyError("goto is not a discovered in-scope URL")
        response = await page.goto(url, wait_until="domcontentloaded", timeout=step.timeout_ms)
        try:
            check_target(page.url, authorized=origin)
        except ScopeError as exc:
            raise PolicyError("navigation left the authorized origin") from exc
        state["http_status"] = response.status if response is not None else None
        return StepResult(action="goto", passed=True, actual=str(state["http_status"] or ""), kind="assertion")
    if action == "assert_http_status":
        status = state.get("http_status")
        if status is None:
            raise RunnerError("no HTTP status was captured")
        if step.expected == "2xx":
            ok = 200 <= int(status) <= 299
        else:
            ok = str(status) == str(step.expected)
        return StepResult(
            action=action,
            passed=ok,
            expected=step.expected,
            actual=str(status),
            kind="assertion",
        )
    if action == "assert_title":
        actual = normalize_label(await page.title(), 200)
        expected = normalize_label(step.expected, 200)
        return StepResult(
            action=action,
            passed=actual == expected,
            expected=expected,
            actual=actual,
            kind="assertion",
        )
    if action == "assert_heading":
        expected = normalize_label(step.expected, 200)
        try:
            actual = normalize_label(await page.locator("h1").first.inner_text(timeout=step.timeout_ms), 200)
        except PlaywrightTimeoutError:
            return StepResult(
                action=action,
                passed=False,
                expected=expected,
                actual="",
                error="heading not found",
                kind="assertion",
            )
        return StepResult(
            action=action,
            passed=actual == expected,
            expected=expected,
            actual=actual,
            kind="assertion",
        )
    if action == "assert_text":
        expected = normalize_label(step.expected, 300)
        try:
            body = await page.locator("body").inner_text(timeout=step.timeout_ms)
        except PlaywrightTimeoutError:
            body = ""
        actual = normalize_label(body, 1200)
        return StepResult(
            action=action,
            passed=bool(expected) and expected in actual,
            expected=expected,
            actual=actual[:300],
            kind="assertion",
        )
    if action == "assert_no_page_errors":
        await page.wait_for_timeout(250)
        if await page.locator("[data-qa-bug]").count() > 0:
            marker = await page.locator("[data-qa-bug]").first.get_attribute("data-qa-bug")
            if marker:
                state["page_errors"].append(redact(marker, 300))
        errors = [*state["page_errors"], *state["console_errors"]]
        return StepResult(
            action=action,
            passed=not errors,
            expected="no page errors",
            actual="; ".join(errors)[:400],
            kind="assertion",
        )
    raise PolicyError("action is not in the allowed set")
