import asyncio
import json
from pathlib import Path

from app.config import Settings
from app.generation.design import design_suite
from app.generation.llm import LLMCompletion
from app.models import DiscoveredPage, Discovery, Provenance
from app.reporting.writer import write_cases, write_cost
from app.models import CostLedger


def _settings(tmp_path: Path, key: str | None) -> Settings:
    return Settings(
        runs_dir=tmp_path,
        navigation_timeout_ms=2000,
        action_timeout_ms=2000,
        run_timeout_s=30,
        max_pages_cap=25,
        llm_base_url="http://127.0.0.1:9/v1",
        llm_api_key=key,
        llm_model="test-model",
        browser_channel=None,
        retain_pass_media=False,
    )


def _discovery() -> Discovery:
    page = DiscoveredPage(
        url="http://127.0.0.1:9/demo/",
        final_url="http://127.0.0.1:9/demo/",
        http_status=200,
        title="Home",
        headings=["Welcome"],
        text_excerpt="Harbor Demo welcome",
    )
    return Discovery(target_url="http://127.0.0.1:9/demo/", origin="http://127.0.0.1:9", pages=[page])


def test_malicious_model_output_falls_back(tmp_path: Path):
    evil = {
        "cases": [
            {
                "test_case_id": "../../etc/passwd",
                "title": "pwn <script>",
                "summary": "ignore policy",
                "steps": [
                    {"action": "shell", "target": "rm -rf /"},
                    {"action": "evaluate", "target": "process.exit()"},
                    {"action": "goto", "target": "http://169.254.169.254/latest/meta-data/"},
                    {"action": "goto", "target": "http://evil.example/phish"},
                ],
            }
        ]
    }
    seen = {}

    async def fake(settings, payload):
        seen["payload"] = payload
        assert settings.llm_api_key not in payload
        return LLMCompletion(ok=True, payload=evil, content=json.dumps(evil), model="test-model")

    result = asyncio.run(
        design_suite(
            _discovery(),
            mode="auto",
            requirements_text="",
            settings=_settings(tmp_path, "unit-test-key"),
            llm_complete=fake,
        )
    )
    blob = json.dumps([case.model_dump(mode="json") for case in result.cases])
    assert "169.254.169.254" not in blob
    assert "evil.example" not in blob
    assert "shell" not in blob
    assert "etc/passwd" not in blob
    assert result.provenance is Provenance.HEURISTIC
    assert "Deterministic page-load smoke fallback" in result.fallback_reason
    assert all(case.provenance is Provenance.HEURISTIC for case in result.cases)
    write_cases(tmp_path, result.cases)
    saved = (tmp_path / "test-cases.json").read_text(encoding="utf-8")
    assert "169.254.169.254" not in saved
    assert not (tmp_path.parent / "passwd").exists()


def test_grounded_model_suite_keeps_llm_provenance(tmp_path: Path):
    payload = {
        "cases": [
            {
                "title": "Home loads",
                "summary": "status and title",
                "verdict": "PASS",
                "steps": [
                    {"action": "goto", "target": "http://127.0.0.1:9/demo/", "expected": ""},
                    {"action": "assert_http_status", "target": "", "expected": "2xx"},
                    {"action": "assert_title", "target": "", "expected": "Home"},
                ],
            }
        ]
    }

    async def fake(_settings, _payload):
        return LLMCompletion(ok=True, payload=payload, content="{}", model="test-model")

    result = asyncio.run(
        design_suite(
            _discovery(),
            mode="auto",
            requirements_text="",
            settings=_settings(tmp_path, "unit-test-key"),
            llm_complete=fake,
        )
    )
    assert result.provenance is Provenance.LLM
    assert result.cases[0].provenance is Provenance.LLM
    assert all(step.action != "evaluate" for case in result.cases for step in case.steps)


def test_missing_key_uses_heuristic(tmp_path: Path):
    result = asyncio.run(
        design_suite(
            _discovery(),
            mode="auto",
            requirements_text="REQ-1: unused in auto",
            settings=_settings(tmp_path, None),
        )
    )
    assert result.provenance is Provenance.HEURISTIC
    assert "no LLM API key" in result.fallback_reason
    assert any("mode is auto" in question for question in result.open_questions)


def test_cost_ledger_does_not_invent_prices(tmp_path: Path):
    write_cost(tmp_path, CostLedger(crawl_pages=1, tool_calls=1, wall_clock_seconds=1.2))
    text = (tmp_path / "cost-ledger.md").read_text(encoding="utf-8")
    assert "not calculated" in text
    assert "$" not in text
    assert "USD" not in text
