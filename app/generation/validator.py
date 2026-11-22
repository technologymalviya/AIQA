"""Reject model output that is unsafe or not grounded in discovery.

An unsafe case discards the whole model suite. An ungrounded case is dropped
on its own. Neither path executes arbitrary code.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, ValidationError

from app.generation.heuristic import case_id
from app.models import DiscoveredPage, EvidenceRef, Provenance, TestCase, TestStep
from app.security.paths import CASE_ID_RE
from app.security.text import normalize_label, redact
from app.security.url_policy import ScopeError, check_target, path_is_destructive
from app.security.url_policy import Origin

_ALLOWED = {
    "goto",
    "assert_http_status",
    "assert_title",
    "assert_heading",
    "assert_text",
    "assert_no_page_errors",
}
_UNSAFE_MARKERS = (
    "javascript:",
    "data:",
    "file:",
    "<script",
    "eval(",
    "function(",
    "`",
    "process.",
    "document.",
    "window.",
    "../",
    "..\\",
    "\x00",
    "%2e%2e",
    "%00",
    "rm -",
    "shell",
    "evaluate",
)


class _ModelStep(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str = ""
    target: str = ""
    expected: str = ""
    timeout_ms: int = 6000


class _ModelCase(BaseModel):
    model_config = ConfigDict(extra="ignore")

    test_case_id: str = ""
    title: str = ""
    summary: str = ""
    steps: list[_ModelStep] = []
    requirement_ids: list[str] = []


class _ModelSuite(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cases: list[_ModelCase] = []


@dataclass
class ValidationDecision:
    cases: list[TestCase] = field(default_factory=list)
    unsafe: bool = False
    reason: str = ""
    dropped: list[str] = field(default_factory=list)


def _blob_unsafe(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in _UNSAFE_MARKERS)


def _id_unsafe(value: str) -> bool:
    if not value:
        return False
    if any(ch in value for ch in ("/", "\\", "\x00", "%")) or ".." in value:
        return True
    return False


def validate_model_suite(
    payload: dict,
    pages: list[DiscoveredPage],
    origin: Origin,
) -> ValidationDecision:
    try:
        suite = _ModelSuite.model_validate(payload)
    except ValidationError:
        return ValidationDecision(unsafe=False, reason="model JSON did not match the suite schema")
    if not suite.cases or len(suite.cases) > 20:
        return ValidationDecision(reason="model suite was empty or larger than 20 cases")
    by_url = {page.url: page for page in pages if not page.redirect_escape}
    accepted: list[TestCase] = []
    dropped: list[str] = []
    for raw in suite.cases:
        if _id_unsafe(raw.test_case_id) or _blob_unsafe(raw.title) or _blob_unsafe(raw.summary):
            return ValidationDecision(
                unsafe=True,
                reason="model output included an unsafe id or code-like text and was discarded",
            )
        if len(raw.steps) > 8:
            return ValidationDecision(unsafe=True, reason="model case had too many steps and was discarded")
        grounded = _ground_case(raw, by_url, origin)
        if grounded is None:
            return ValidationDecision(
                unsafe=True,
                reason="model output included an action or URL outside the allowed set and was discarded",
            )
        if grounded is False:
            dropped.append(redact(raw.title or "untitled", 120))
            continue
        accepted.append(grounded)
    if not accepted:
        return ValidationDecision(reason="no evidence-grounded cases remained", dropped=dropped)
    return ValidationDecision(cases=accepted, dropped=dropped)


def _ground_case(raw: _ModelCase, pages: dict[str, DiscoveredPage], origin: Origin) -> TestCase | None | bool:
    """Return a case, False when ungrounded, or None when unsafe."""
    steps: list[TestStep] = []
    current: DiscoveredPage | None = None
    for step in raw.steps:
        action = step.action.strip()
        if _blob_unsafe(step.target) or _blob_unsafe(step.expected):
            return None
        if action not in _ALLOWED:
            continue
        if action == "goto":
            try:
                url = check_target(step.target, authorized=origin)
            except ScopeError:
                return False
            if path_is_destructive(url):
                return None
            if url not in pages:
                return False
            current = pages[url]
            steps.append(TestStep(action="goto", target=url, timeout_ms=_timeout(step.timeout_ms)))
            continue
        if current is None:
            return False
        expected = normalize_label(step.expected, 300)
        if action == "assert_http_status":
            if expected not in {"2xx", "200", "201", "204"}:
                continue
            status = current.http_status
            if status is None or not 200 <= status <= 299:
                continue
            expected = "2xx"
        elif action == "assert_title":
            if not current.title or expected != current.title:
                continue
        elif action == "assert_heading":
            allowed = set(current.headings)
            if current.primary_heading:
                allowed.add(current.primary_heading)
            if expected not in allowed:
                continue
        elif action == "assert_text":
            blob = " ".join(
                [current.title, current.text_excerpt, current.primary_heading, *current.headings, *current.buttons]
            )
            if not expected or expected not in blob:
                continue
        elif action == "assert_no_page_errors":
            expected = ""
        steps.append(
            TestStep(action=action, expected=expected, timeout_ms=_timeout(step.timeout_ms))  # type: ignore[arg-type]
        )
    if not steps or not any(item.action == "goto" for item in steps):
        return False
    if raw.test_case_id and not CASE_ID_RE.fullmatch(raw.test_case_id):
        # A merely odd id is replaced. Path-like ids were already rejected.
        assigned = case_id(steps[0].target, raw.title or "model")
    else:
        assigned = raw.test_case_id or case_id(steps[0].target, raw.title or "model")
    page = pages[steps[0].target]
    return TestCase(
        test_case_id=assigned,
        title=normalize_label(raw.title or page.title or "Generated case", 180),
        summary=redact(raw.summary or "Generated from discovered pages.", 500),
        priority="P1",
        risk="medium",
        feature=(page.headings[0] if page.headings else page.title or "page")[:80],
        requirement_ids=[item[:40] for item in raw.requirement_ids[:10]],
        source_evidence=[
            EvidenceRef(kind="url", ref=page.url, observed_at="", detail=f"HTTP {page.http_status}")
        ],
        preconditions=["The tester confirmed authorization for this origin."],
        steps=steps,
        expected_outcomes=[item.expected or item.action for item in steps],
        cleanup_steps=["Close the browser context. The crawl does not create data."],
        confidence=0.55,
        assumptions=["Model text was checked against the pages this run discovered."],
        review_required=False,
        tags=["generated"],
        provenance=Provenance.LLM,
    )


def _timeout(value: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 6000
    return max(100, min(number, 30000))
