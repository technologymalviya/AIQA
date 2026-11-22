import asyncio
import time

from app.execution.agent import run_generated_cases
from app.models import CaseResult, Provenance, Verdict
from app.models import TestCase as GeneratedCase
from app.models import TestStep as GeneratedStep


def _case(case_id: str, url: str) -> GeneratedCase:
    return GeneratedCase(
        test_case_id=case_id,
        title=f"{url} loads",
        summary="Open the page.",
        priority="P1",
        risk="medium",
        feature="page",
        steps=[GeneratedStep(action="goto", target=url)],
        confidence=0.8,
        provenance=Provenance.HEURISTIC,
    )


def test_playwright_agent_runs_a_generated_case_the_journey_already_opened():
    login = _case("TC-AAAA1111", "https://dairyhisab.in/login")
    home = _case("TC-BBBB2222", "https://dairyhisab.in/")
    seen: list[str] = []

    async def execute(_browser, case, **_kwargs):
        seen.append(case.test_case_id)
        return CaseResult(
            test_case_id=case.test_case_id,
            title=case.title,
            provenance=case.provenance,
            verdict=Verdict.PASS,
            classification="none",
        )

    results = asyncio.run(
        run_generated_cases(
            None,
            [login, home],
            origin=None,
            allowed_urls={login.steps[0].target, home.steps[0].target},
            evidence_root=None,
            settings=None,
            environment={},
            deadline=time.monotonic() + 30,
            cancelled=lambda: False,
            execute=execute,
        )
    )
    assert seen == ["TC-AAAA1111", "TC-BBBB2222"]
    assert [item.verdict for item in results] == [Verdict.PASS, Verdict.PASS]
