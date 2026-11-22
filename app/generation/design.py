"""Choose heuristic cases, or accept a validated model suite."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Settings
from app.generation.heuristic import blocked_requirement_case, cases_for_page, link_requirements
from app.generation.llm import LLMCompletion, complete_json, prompt_for
from app.generation.requirements import mentions_foreign_url, parse_requirements
from app.generation.validator import validate_model_suite
from app.models import Discovery, Provenance, Requirement, TestCase
from app.security.url_policy import origin_of


@dataclass
class GenerationResult:
    cases: list[TestCase]
    requirements: list[Requirement]
    open_questions: list[str] = field(default_factory=list)
    fallback_reason: str = ""
    provenance: Provenance = Provenance.HEURISTIC
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str | None = None
    provider: str | None = None


async def design_suite(
    discovery: Discovery,
    *,
    mode: str,
    requirements_text: str,
    settings: Settings,
    llm_complete=None,
) -> GenerationResult:
    questions: list[str] = []
    requirements: list[Requirement] = []
    if mode == "hybrid":
        requirements = parse_requirements(requirements_text)
    elif requirements_text.strip():
        questions.append("Requirements text was ignored because the run mode is auto.")
    origin = origin_of(discovery.target_url)
    for requirement in requirements:
        if mentions_foreign_url(requirement.text, origin.origin):
            questions.append(
                f"{requirement.requirement_id} mentions a URL outside the authorized origin. "
                "No case was generated for that URL."
            )
            requirement.ambiguous = True
    heuristic = []
    for page in discovery.pages:
        heuristic.extend(cases_for_page(page, settings.action_timeout_ms))
    for requirement in requirements:
        if requirement.destructive:
            blocked = blocked_requirement_case(requirement)
            requirement.linked_case_ids.append(blocked.test_case_id)
            heuristic.append(blocked)
            questions.append(
                f"{requirement.requirement_id} asks for an action Phase 0 will not run. "
                "The case is BLOCKED."
            )
    link_requirements(heuristic, requirements, discovery.pages)
    for requirement in requirements:
        if requirement.destructive or requirement.linked_case_ids:
            continue
        questions.append(
            f"{requirement.requirement_id} is not covered by a discovered page: {requirement.text[:180]}"
        )
    if not settings.llm_api_key and not settings.hf_token and llm_complete is None:
        reason = (
            "Deterministic page-load smoke fallback: no model key is configured "
            "(QA_LLM_API_KEY or QA_HF_TOKEN)."
        )
        questions.append(reason)
        return GenerationResult(
            cases=heuristic,
            requirements=requirements,
            open_questions=questions,
            fallback_reason=reason,
            provenance=Provenance.HEURISTIC,
        )
    caller = llm_complete or complete_json
    authored: list[TestCase] = []
    calls = 0
    input_tokens = 0
    output_tokens = 0
    model = ""
    last_reason = ""
    covered: set[str] = set()
    for page in discovery.pages:
        if page.redirect_escape or page.challenge:
            continue
        ask = prompt_for([page], requirements, requirements_text)
        completion, used = await _one_completion(caller, settings, ask)
        calls += used
        input_tokens += completion.input_tokens
        output_tokens += completion.output_tokens
        model = completion.model or model
        if completion.payload is None:
            last_reason = completion.error or "empty response"
            continue
        decision = validate_model_suite(completion.payload, [page], origin)
        if decision.dropped:
            questions.append("Dropped ungrounded model cases: " + "; ".join(decision.dropped))
        if decision.unsafe or not decision.cases:
            last_reason = decision.reason or completion.error or "model output failed validation"
            continue
        for case in decision.cases:
            targets = {step.target for step in case.steps if step.action == "goto"}
            if page.url in targets:
                covered.add(page.url)
        authored.extend(decision.cases)
    if not authored:
        reason = (
            "Deterministic page-load smoke fallback: model output was not usable "
            f"({last_reason or 'empty response'})."
        )
        questions.append(reason)
        return GenerationResult(
            cases=heuristic,
            requirements=requirements,
            open_questions=questions,
            fallback_reason=reason,
            provenance=Provenance.HEURISTIC,
            llm_calls=calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model or settings.llm_model,
            provider=settings.llm_base_url,
        )
    kept = [
        case
        for case in heuristic
        if case.blocked_reason
        or "runtime" in case.tags
        or not any(step.action == "goto" and step.target in covered for step in case.steps)
    ]
    missed = sorted(
        {
            step.target
            for case in kept
            for step in case.steps
            if step.action == "goto" and step.target not in covered
        }
    )
    if missed:
        questions.append("Page-load checks were kept for screens the model did not write: " + ", ".join(missed))
    cases = authored + kept
    link_requirements(cases, requirements, discovery.pages)
    return GenerationResult(
        cases=cases,
        requirements=requirements,
        open_questions=questions,
        provenance=Provenance.LLM,
        llm_calls=calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model=model or settings.llm_model,
        provider=settings.llm_base_url,
    )


async def _one_completion(caller, settings: Settings, ask: str) -> tuple[LLMCompletion, int]:
    """Ask once, and once more when the reply is not JSON."""
    first: LLMCompletion = await caller(settings, ask)
    if first.ok and first.payload is not None:
        return first, 1
    second: LLMCompletion = await caller(settings, ask + "\nReturn the JSON object only.")
    second.input_tokens += first.input_tokens
    second.output_tokens += first.output_tokens
    return second, 2
