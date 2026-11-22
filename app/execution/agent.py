"""Playwright agent. It runs every generated use case in Chromium.

A page the journey already opened is still a case. The runner checks it again.
"""

from __future__ import annotations

import time
from pathlib import Path

from playwright.async_api import Browser

from app.config import Settings
from app.execution.runner import execute_case
from app.execution.verdict import judge
from app.models import CaseResult, TestCase, Verdict
from app.security.url_policy import Origin


async def run_generated_cases(
    browser: Browser,
    cases: list[TestCase],
    *,
    origin: Origin,
    allowed_urls: set[str],
    evidence_root: Path,
    settings: Settings,
    environment: dict[str, str],
    deadline: float,
    cancelled,
    on_screen=None,
    on_note=None,
    on_verdict=None,
    execute=execute_case,
) -> list[CaseResult]:
    """Execute the generated suite. A failed case is run once more, then the suite continues."""
    results: list[CaseResult] = []
    if on_note and cases:
        listed = "; ".join(case.title for case in cases[:12])
        extra = f" and {len(cases) - 12} more" if len(cases) > 12 else ""
        on_note(f"Generated {len(cases)} test cases: {listed}{extra}.")
    for case in cases:
        if cancelled():
            results.append(_stopped(case, "cancelled before execution", environment))
            continue
        if time.monotonic() > deadline:
            results.append(_stopped(case, "run budget exhausted before execution", environment))
            continue
        if on_note:
            on_note(f"Playwright is running {case.title}.")
        outcome = await _execute_once(
            execute, browser, case, origin, allowed_urls, evidence_root, settings, environment, on_screen
        )
        if _needs_retry(outcome) and not cancelled() and time.monotonic() <= deadline:
            if on_note:
                on_note(f"{case.title} did not pass. I'll run that case again.")
            outcome = await _execute_once(
                execute, browser, case, origin, allowed_urls, evidence_root, settings, environment, on_screen
            )
        results.append(outcome)
        if on_verdict:
            on_verdict(outcome)
    return results


def _needs_retry(outcome: CaseResult) -> bool:
    return outcome.verdict in {Verdict.FAIL, Verdict.INCONCLUSIVE}


async def _execute_once(execute, browser, case, origin, allowed_urls, evidence_root, settings, environment, on_screen):
    return await execute(
        browser,
        case,
        origin=origin,
        allowed_urls=allowed_urls,
        evidence_root=evidence_root,
        settings=settings,
        environment=environment,
        on_screen=on_screen,
    )


def _stopped(case: TestCase, reason: str, environment: dict[str, str]) -> CaseResult:
    verdict, classification, kind, text = judge([], blocked_reason=reason)
    return CaseResult(
        test_case_id=case.test_case_id,
        title=case.title,
        provenance=case.provenance,
        verdict=verdict,
        classification=classification,
        failure_kind=kind,
        reason=text,
        environment=environment,
    )
