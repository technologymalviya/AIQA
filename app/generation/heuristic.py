"""Deterministic page-load cases. Expected results are health invariants.

A 4xx page still gets an assertion that it should have loaded. The observed
error page is evidence, not the expected result.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from app.models import (
    DiscoveredPage,
    EvidenceRef,
    Provenance,
    Requirement,
    TestCase,
    TestStep,
)
from app.security.paths import require_case_id


def case_id(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:8].upper()
    return require_case_id(f"TC-{digest}")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _evidence(page: DiscoveredPage, detail: str) -> EvidenceRef:
    return EvidenceRef(
        kind="url",
        ref=page.url,
        observed_at=_now(),
        detail=detail[:300],
    )


def _base(page: DiscoveredPage, *, kind: str, title: str, summary: str, steps: list[TestStep], priority: str, risk: str, tags: list[str]) -> TestCase:
    return TestCase(
        test_case_id=case_id(page.url, kind),
        title=title[:180],
        summary=summary[:500],
        priority=priority,  # type: ignore[arg-type]
        risk=risk,  # type: ignore[arg-type]
        feature=(page.headings[0] if page.headings else page.title or "page")[:80],
        source_evidence=[_evidence(page, f"HTTP {page.http_status} {page.title}")],
        preconditions=["The tester confirmed authorization for this origin."],
        steps=steps,
        expected_outcomes=[step.expected or step.action for step in steps],
        cleanup_steps=["Close the browser context. The crawl does not create data."],
        confidence=0.85,
        assumptions=[
            "Smoke checks use observed titles and headings. They do not invent business rules."
        ],
        tags=tags,
        provenance=Provenance.HEURISTIC,
    )


def _goto(page: DiscoveredPage, timeout_ms: int) -> TestStep:
    return TestStep(action="goto", target=page.url, timeout_ms=timeout_ms)


def cases_for_page(page: DiscoveredPage, timeout_ms: int) -> list[TestCase]:
    if page.redirect_escape:
        return []
    timeout = max(100, min(timeout_ms, 30000))
    built: list[TestCase] = []
    status = page.http_status
    healthy = status is not None and 200 <= status <= 299 and not page.load_error
    if healthy:
        steps = [
            _goto(page, timeout),
            TestStep(action="assert_http_status", expected="2xx", timeout_ms=timeout),
        ]
        if page.title:
            steps.append(TestStep(action="assert_title", expected=page.title, timeout_ms=timeout))
        if page.headings:
            steps.append(TestStep(action="assert_heading", expected=page.headings[0], timeout_ms=timeout))
        built.append(
            _base(
                page,
                kind="smoke",
                title=f"{page.title or page.url} loads",
                summary="Open the page and check status, title, and the first heading.",
                steps=steps,
                priority="P1",
                risk="medium",
                tags=["smoke"],
            )
        )
        if page.page_errors or page.console_errors:
            built.append(
                _base(
                    page,
                    kind="runtime",
                    title=f"{page.title or page.url} has no runtime errors",
                    summary="The page loaded, and discovery saw a console or page error.",
                    steps=[
                        _goto(page, timeout),
                        TestStep(action="assert_http_status", expected="2xx", timeout_ms=timeout),
                        TestStep(action="assert_no_page_errors", timeout_ms=timeout),
                    ],
                    priority="P0",
                    risk="high",
                    tags=["runtime"],
                )
            )
        return built
    built.append(
        _base(
            page,
            kind="reachability",
            title=f"{page.title or page.url} is reachable",
            summary="The route was discovered and is expected to load with HTTP 2xx.",
            steps=[
                _goto(page, timeout),
                TestStep(action="assert_http_status", expected="2xx", timeout_ms=timeout),
            ],
            priority="P0",
            risk="high",
            tags=["reachability"],
        )
    )
    return built


def blocked_requirement_case(requirement: Requirement) -> TestCase:
    reason = "destructive or externally visible action is denied in Phase 0"
    if requirement.destructive:
        reason = "requirement asks for a destructive or payment action; it was not executed"
    return TestCase(
        test_case_id=case_id(requirement.requirement_id, "blocked"),
        title=f"{requirement.requirement_id} is not automated",
        summary="This requirement was not automated because it asks for a destructive or payment action.",
        priority="P2",
        risk="high",
        feature=requirement.requirement_id,
        requirement_ids=[requirement.requirement_id],
        preconditions=["A human reviews the requirement before any write action."],
        steps=[],
        expected_outcomes=["No destructive action is attempted."],
        cleanup_steps=[],
        automation_status="manual",
        confidence=1.0,
        assumptions=[],
        review_required=True,
        tags=["blocked"],
        provenance=Provenance.HEURISTIC,
        blocked_reason=reason,
    )


def link_requirements(cases: list[TestCase], requirements: list[Requirement], pages: list[DiscoveredPage]) -> None:
    by_url = {page.url: page for page in pages}
    for requirement in requirements:
        if requirement.destructive:
            continue
        tokens = {word.lower() for word in requirement.text.split() if len(word) > 3}
        for case in cases:
            if case.blocked_reason:
                continue
            page = None
            for step in case.steps:
                if step.action == "goto":
                    page = by_url.get(step.target)
            blob = " ".join(
                [
                    case.title,
                    case.feature,
                    page.title if page else "",
                    " ".join(page.headings if page else []),
                ]
            ).lower()
            if tokens and any(token.strip(".,:;") in blob for token in tokens):
                if requirement.requirement_id not in case.requirement_ids:
                    case.requirement_ids.append(requirement.requirement_id)
                if case.test_case_id not in requirement.linked_case_ids:
                    requirement.linked_case_ids.append(case.test_case_id)
