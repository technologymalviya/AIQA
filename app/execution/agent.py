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
from app.models import CaseResult, TestCase
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
    """Execute the generated suite. Order is the suite order."""
    results: list[CaseResult] = []
    for case in cases:
        if cancelled():
            results.append(_stopped(case, "cancelled before execution", environment))
            continue
        if time.monotonic() > deadline:
            results.append(_stopped(case, "run budget exhausted before execution", environment))
            continue
        if on_note:
            on_note(f"Playwright is running {case.title}.")
        outcome = await execute(
            browser,
            case,
            origin=origin,
            allowed_urls=allowed_urls,
            evidence_root=evidence_root,
            settings=settings,
            environment=environment,
            on_screen=on_screen,
        )
        results.append(outcome)
        if on_verdict:
            on_verdict(outcome)
    return results


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
