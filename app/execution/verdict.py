"""Recompute the verdict from assertion and runner facts.

A model narrative is not an input to this function.
"""

from __future__ import annotations

from app.models import CaseResult, Classification, FailureKind, StepResult, Verdict


def failure_kind_for(action: str) -> FailureKind:
    if action == "assert_http_status":
        return FailureKind.REACHABILITY
    if action == "assert_no_page_errors":
        return FailureKind.RUNTIME_ERROR
    if action in {"assert_title", "assert_heading", "assert_text"}:
        return FailureKind.CONTENT
    return FailureKind.NONE


def judge(
    steps: list[StepResult],
    *,
    blocked_reason: str = "",
    runner_error: str = "",
) -> tuple[Verdict, Classification, FailureKind, str]:
    if blocked_reason:
        return Verdict.BLOCKED, Classification.BLOCKED, FailureKind.NONE, blocked_reason
    runner_steps = [step for step in steps if step.kind == "runner" and not step.passed]
    if runner_error or runner_steps:
        reason = runner_error or runner_steps[0].error or "runner failure"
        return Verdict.INCONCLUSIVE, Classification.ENVIRONMENT, FailureKind.NONE, reason
    failed = [step for step in steps if not step.passed]
    if failed:
        step = failed[0]
        reason = step.error or f"expected {step.expected!r} but observed {step.actual!r}"
        return (
            Verdict.FAIL,
            Classification.PRODUCT_BUG,
            failure_kind_for(step.action),
            reason,
        )
    if steps and all(step.passed for step in steps):
        return Verdict.PASS, Classification.NONE, FailureKind.NONE, ""
    return Verdict.INCONCLUSIVE, Classification.UNKNOWN, FailureKind.NONE, "no steps executed"


def should_retain_media(verdict: Verdict, *, retain_pass_media: bool) -> bool:
    if verdict in {Verdict.SKIPPED, Verdict.BLOCKED}:
        return False
    if retain_pass_media:
        return True
    return verdict in {Verdict.FAIL, Verdict.INCONCLUSIVE}


def apply_judgement(result: CaseResult, steps: list[StepResult], **kwargs: str) -> CaseResult:
    verdict, classification, kind, reason = judge(steps, **kwargs)
    result.verdict = verdict
    result.classification = classification
    result.failure_kind = kind
    result.reason = reason
    result.steps = steps
    return result
