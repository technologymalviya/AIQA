"""Write the run folder. Discovered text is escaped before it reaches Markdown."""

from __future__ import annotations

import json
from pathlib import Path
from xml.sax.saxutils import escape

from app.models import (
    CaseResult,
    CostLedger,
    Discovery,
    Requirement,
    TestCase,
    Verdict,
)
from app.security.paths import require_case_id, safe_child
from app.security.text import markdown_text


def _write(root: Path, name: str, text: str) -> None:
    path = safe_child(root, name)
    path.write_text(text, encoding="utf-8")


def _json(root: Path, name: str, payload: object) -> None:
    _write(root, name, json.dumps(payload, indent=2) + "\n")


def write_discovery(root: Path, discovery: Discovery) -> None:
    _json(root, "discovery.json", discovery.model_dump(mode="json"))


def write_cases(root: Path, cases: list[TestCase]) -> None:
    _json(root, "test-cases.json", [case.model_dump(mode="json") for case in cases])
    lines = ["# Test cases", ""]
    for case in cases:
        lines.append(f"## {markdown_text(case.test_case_id)} — {markdown_text(case.title)}")
        lines.append("")
        lines.append(f"- Provenance: {case.provenance.value}")
        lines.append(f"- Priority: {case.priority}")
        lines.append(f"- Risk: {case.risk}")
        lines.append(f"- Feature: {markdown_text(case.feature)}")
        lines.append(f"- Requirements: {markdown_text(', '.join(case.requirement_ids) or 'none')}")
        lines.append(f"- Review required: {str(case.review_required).lower()}")
        if case.blocked_reason:
            lines.append(f"- Blocked: {markdown_text(case.blocked_reason)}")
        lines.append("")
        lines.append(markdown_text(case.summary))
        lines.append("")
        lines.append("### Steps")
        lines.append("")
        if not case.steps:
            lines.append("No automated steps.")
        for index, step in enumerate(case.steps, start=1):
            lines.append(
                f"{index}. `{step.action}` target `{markdown_text(step.target)}` "
                f"expected `{markdown_text(step.expected)}`"
            )
        lines.append("")
    _write(root, "test-cases.md", "\n".join(lines) + "\n")


def write_understanding(root: Path, discovery: Discovery, mode: str) -> None:
    lines = [
        "# Application understanding",
        "",
        f"- Mode: {mode}",
        f"- Target: {markdown_text(discovery.target_url)}",
        f"- Origin: {markdown_text(discovery.origin)}",
        f"- Pages visited: {len(discovery.pages)}",
        "",
        "The crawl reads each screen and does not collect typed-in values. "
        "When the app has a signup screen, one later step creates a single test account and reuses it.",
        "",
        "## Pages",
        "",
    ]
    for page in discovery.pages:
        lines.append(f"### {markdown_text(page.title or page.url)}")
        lines.append("")
        lines.append(f"- URL: {markdown_text(page.url)}")
        lines.append(f"- Final URL: {markdown_text(page.final_url or page.url)}")
        lines.append(f"- HTTP status: {page.http_status if page.http_status is not None else 'none'}")
        if page.load_error:
            lines.append(f"- Load error: {markdown_text(page.load_error)}")
        if page.redirect_escape:
            lines.append("- Redirect left the authorized origin. Body text was not stored.")
        if page.app_version:
            lines.append(f"- App version: {markdown_text(page.app_version)}")
        if page.headings:
            lines.append("- Headings: " + markdown_text(" | ".join(page.headings)))
        if page.buttons:
            lines.append("- Buttons: " + markdown_text(", ".join(page.buttons)))
        if page.forms:
            described = []
            for form in page.forms:
                fields = ", ".join(
                    f"{field.name or '(unnamed)'}:{field.type or field.tag}" for field in form.fields
                )
                described.append(f"{form.method} {form.action or '(no action)'} [{fields}]")
            lines.append("- Forms: " + markdown_text("; ".join(described)))
        if page.console_errors:
            lines.append("- Console errors: " + markdown_text(" | ".join(page.console_errors)))
        if page.page_errors:
            lines.append("- Page errors: " + markdown_text(" | ".join(page.page_errors)))
        if page.text_excerpt:
            lines.append("")
            lines.append("```")
            lines.append(markdown_text(page.text_excerpt, 800))
            lines.append("```")
        lines.append("")
        lines.append("Links:")
        lines.append("")
        for link in page.links:
            note = f" ({link.skipped_reason})" if link.skipped_reason else ""
            lines.append(f"- {markdown_text(link.label)} → {markdown_text(link.href)}{markdown_text(note)}")
        lines.append("")
    if discovery.external_links:
        lines.append("## External links not fetched")
        lines.append("")
        for link in discovery.external_links:
            lines.append(f"- {markdown_text(link)}")
        lines.append("")
    if discovery.denylisted:
        lines.append("## Denylisted links not fetched")
        lines.append("")
        for link in discovery.denylisted:
            lines.append(f"- {markdown_text(link)}")
        lines.append("")
    missing = [page for page in discovery.pages if page.http_status is not None and page.http_status >= 400]
    if missing:
        lines.append("## Intentionally observed broken routes")
        lines.append("")
        for page in missing:
            lines.append(
                f"- Missing-page or error route: {markdown_text(page.url)} (HTTP {page.http_status})."
            )
        lines.append("")
    _write(root, "application-understanding.md", "\n".join(lines) + "\n")


def write_results(root: Path, results: list[CaseResult], environment: dict[str, str]) -> None:
    payload = {
        "environment": environment,
        "results": [item.model_dump(mode="json") for item in results],
    }
    _json(root, "results.json", payload)
    lines = ["# Results", "", "Classification does not change the verdict.", ""]
    lines.append("## Environment")
    lines.append("")
    for key, value in environment.items():
        lines.append(f"- {markdown_text(key)}: {markdown_text(value)}")
    lines.append("")
    lines.append("| Case | Verdict | Classification | Kind | Duration ms |")
    lines.append("| --- | --- | --- | --- | --- |")
    for item in results:
        lines.append(
            f"| {item.test_case_id} | {item.verdict.value} | {item.classification.value} | "
            f"{item.failure_kind.value} | {item.duration_ms} |"
        )
    lines.append("")
    for item in results:
        lines.append(f"## {item.test_case_id} — {markdown_text(item.title)}")
        lines.append("")
        lines.append(f"- Verdict: {item.verdict.value}")
        lines.append(f"- Classification: {item.classification.value}")
        lines.append(f"- Failure kind: {item.failure_kind.value}")
        lines.append(f"- Provenance: {item.provenance.value}")
        if item.reason:
            lines.append(f"- Reason: {markdown_text(item.reason)}")
        lines.append(f"- Duration ms: {item.duration_ms}")
        if item.evidence:
            lines.append("- Evidence:")
            for ref in item.evidence:
                lines.append(f"  - {ref}")
        lines.append("")
        for step in item.steps:
            lines.append(
                f"- step `{step.action}` {step.kind} passed={str(step.passed).lower()} "
                f"expected `{markdown_text(step.expected)}` actual `{markdown_text(step.actual)}` "
                f"error `{markdown_text(step.error)}`"
            )
        lines.append("")
    _write(root, "results.md", "\n".join(lines) + "\n")


def write_junit(root: Path, results: list[CaseResult]) -> None:
    failures = sum(1 for item in results if item.verdict == Verdict.FAIL)
    errors = sum(1 for item in results if item.verdict == Verdict.INCONCLUSIVE)
    skipped = sum(1 for item in results if item.verdict in {Verdict.BLOCKED, Verdict.SKIPPED})
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<testsuite name="qa-orchestrator" tests="{len(results)}" '
            f'failures="{failures}" errors="{errors}" skipped="{skipped}">'
        ),
    ]
    for item in results:
        seconds = f"{item.duration_ms / 1000:.3f}"
        name = escape(item.title)
        parts.append(
            f'<testcase name="{name}" classname="{escape(item.test_case_id)}" time="{seconds}">'
        )
        if item.verdict == Verdict.FAIL:
            parts.append(f'<failure message="{escape(item.reason)}">{escape(item.reason)}</failure>')
        elif item.verdict == Verdict.INCONCLUSIVE:
            parts.append(f'<error message="{escape(item.reason)}">{escape(item.reason)}</error>')
        elif item.verdict in {Verdict.BLOCKED, Verdict.SKIPPED}:
            parts.append(f'<skipped message="{escape(item.reason)}"/>')
        parts.append("</testcase>")
    parts.append("</testsuite>")
    _write(root, "junit.xml", "\n".join(parts) + "\n")


def write_defects(root: Path, cases: list[TestCase], results: list[CaseResult], environment: dict[str, str]) -> list[str]:
    by_id = {case.test_case_id: case for case in cases}
    names: list[str] = []
    for result in results:
        if result.verdict != Verdict.FAIL:
            continue
        case = by_id.get(result.test_case_id)
        case_id = require_case_id(result.test_case_id)
        name = f"DEF-{case_id}.md"
        lines = [
            f"# {name[:-3]} — {markdown_text(result.title)}",
            "",
            "- Status: draft",
            "- Publishing: NOT_IMPLEMENTED (Jira and GitHub are out of Phase 0)",
            f"- Severity: {'major' if result.failure_kind.value == 'runtime_error' else 'high'}",
            f"- Verdict: {result.verdict.value}",
            f"- Classification: {result.classification.value}",
            f"- Failure kind: {result.failure_kind.value}",
            "- Note: classification does not change the verdict.",
            f"- Environment: {markdown_text(environment.get('target_url', ''))}",
            f"- Browser: {markdown_text(environment.get('browser', 'chromium'))}",
            "",
            "## Preconditions",
            "",
        ]
        for item in case.preconditions if case else []:
            lines.append(f"- {markdown_text(item)}")
        lines.extend(["", "## Steps", ""])
        if case:
            for index, step in enumerate(case.steps, start=1):
                lines.append(f"{index}. `{step.action}` `{markdown_text(step.target or step.expected)}`")
        lines.extend(["", "## Expected", ""])
        if case:
            for item in case.expected_outcomes:
                lines.append(f"- {markdown_text(item)}")
        lines.extend(["", "## Actual", "", markdown_text(result.reason), "", "## Evidence", ""])
        for ref in result.evidence:
            lines.append(f"- {ref}")
        defect_dir = root / "defects"
        defect_dir.mkdir(exist_ok=True)
        safe_child(defect_dir, name).write_text("\n".join(lines) + "\n", encoding="utf-8")
        names.append(f"defects/{name}")
    return names


def write_open_questions(root: Path, questions: list[str], results: list[CaseResult]) -> None:
    lines = ["# Open questions", ""]
    if not questions and not any(item.verdict == Verdict.INCONCLUSIVE for item in results):
        lines.append("None recorded.")
    for question in questions:
        lines.append(f"- {markdown_text(question)}")
    for result in results:
        if result.verdict == Verdict.INCONCLUSIVE:
            lines.append(
                f"- {result.test_case_id} was INCONCLUSIVE ({markdown_text(result.reason)}). "
                "This is an environment result, not a product failure."
            )
        if result.verdict == Verdict.BLOCKED:
            lines.append(
                f"- {result.test_case_id} was BLOCKED ({markdown_text(result.reason)})."
            )
    lines.append("")
    lines.append(
        "Verdicts come from deterministic assertions and runner status. "
        "A model explanation cannot change them."
    )
    lines.append("")
    _write(root, "open-questions.md", "\n".join(lines) + "\n")


def write_traceability(root: Path, requirements: list[Requirement], results: list[CaseResult]) -> None:
    by_case = {item.test_case_id: item for item in results}
    lines = ["# Traceability", "", "| Requirement | Cases | Verdicts |", "| --- | --- | --- |"]
    if not requirements:
        lines.append("| (none supplied) | | |")
    for requirement in requirements:
        ids = requirement.linked_case_ids or ["none"]
        verdicts = []
        for case_id in requirement.linked_case_ids:
            found = by_case.get(case_id)
            verdicts.append(found.verdict.value if found else "not run")
        lines.append(
            f"| {markdown_text(requirement.requirement_id)} | {markdown_text(', '.join(ids))} | "
            f"{markdown_text(', '.join(verdicts) or 'uncovered')} |"
        )
    lines.append("")
    uncovered = [
        requirement.requirement_id
        for requirement in requirements
        if not requirement.linked_case_ids and not requirement.destructive
    ]
    if uncovered:
        lines.append("## Uncovered")
        lines.append("")
        for item in uncovered:
            lines.append(f"- {markdown_text(item)}")
        lines.append("")
    _write(root, "traceability.md", "\n".join(lines) + "\n")


def write_cost(root: Path, ledger: CostLedger) -> None:
    lines = [
        "# Cost ledger",
        "",
        f"- Price configuration: {ledger.price_configuration}",
        f"- Monetary cost: {ledger.monetary_cost_note}",
        f"- Provider: {ledger.provider or 'none'}",
        f"- Model: {ledger.model or 'none'}",
        f"- LLM calls: {ledger.llm_calls}",
        f"- Input tokens: {ledger.input_tokens}",
        f"- Output tokens: {ledger.output_tokens}",
        f"- Crawl pages: {ledger.crawl_pages}",
        f"- Browser sessions: {ledger.browser_sessions}",
        f"- Tool calls: {ledger.tool_calls}",
        f"- Wall clock seconds: {ledger.wall_clock_seconds:.3f}",
        f"- Artefact bytes (measured): {ledger.artefact_bytes}",
        f"- Evidence bytes (measured): {ledger.evidence_bytes}",
        "",
        "No currency amount is calculated unless a versioned price table is supplied. None was supplied.",
        "",
    ]
    text = "\n".join(lines)
    if "$" in text or "USD" in text:
        raise RuntimeError("cost ledger must not invent a price")
    _write(root, "cost-ledger.md", text)


def measured_bytes(root: Path) -> tuple[int, int]:
    artefact = 0
    evidence = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        size = path.stat().st_size
        if "evidence" in path.parts:
            evidence += size
        else:
            artefact += size
    return artefact, evidence
