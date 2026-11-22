import asyncio
import json
from pathlib import Path

from app.config import Settings
from app.discovery.challenge import challenge_reason
from app.generation.design import design_suite
from app.generation.heuristic import cases_for_page
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


def test_hugging_face_token_is_the_planner_without_an_openai_key(tmp_path: Path):
    captured: dict = {}

    class _Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "model": "meta-llama/Llama-3.3-70B-Instruct",
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "cases": [
                                        {
                                            "title": "Home loads",
                                            "summary": "Open the home page.",
                                            "steps": [
                                                {"action": "goto", "target": "http://127.0.0.1:9/demo/", "expected": ""},
                                                {"action": "assert_http_status", "target": "", "expected": "2xx"},
                                            ],
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ],
            }

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url: str, json: dict, headers: dict) -> _Response:
            captured["url"] = url
            captured["model"] = json["model"]
            captured["auth"] = headers["Authorization"]
            return _Response()

    settings = _settings(tmp_path, None)
    settings = Settings(
        runs_dir=settings.runs_dir,
        navigation_timeout_ms=settings.navigation_timeout_ms,
        action_timeout_ms=settings.action_timeout_ms,
        run_timeout_s=settings.run_timeout_s,
        max_pages_cap=settings.max_pages_cap,
        llm_base_url=settings.llm_base_url,
        llm_api_key=None,
        llm_model=settings.llm_model,
        browser_channel=None,
        retain_pass_media=False,
        hf_token="hf_exampletoken12345678",
        hf_model="meta-llama/Llama-3.3-70B-Instruct",
        hf_base_url="https://router.huggingface.co/v1",
    )
    import app.generation.llm as llm_module

    original = llm_module.httpx.AsyncClient
    llm_module.httpx.AsyncClient = _Client
    try:
        result = asyncio.run(
            design_suite(
                _discovery(),
                mode="hybrid",
                requirements_text="Check the home page loads.",
                settings=settings,
            )
        )
    finally:
        llm_module.httpx.AsyncClient = original
    assert captured["url"] == "https://router.huggingface.co/v1/chat/completions"
    assert captured["model"] == "meta-llama/Llama-3.3-70B-Instruct"
    assert captured["auth"].startswith("Bearer ")
    assert "hf_exampletoken12345678" not in result.fallback_reason
    assert result.provenance is Provenance.LLM
    assert result.model == "meta-llama/Llama-3.3-70B-Instruct"


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
    assert "QA_HF_TOKEN" in result.fallback_reason
    assert any("mode is auto" in question for question in result.open_questions)


def test_a_browser_check_is_not_treated_as_the_product():
    reason = challenge_reason("Access Denied", "", "Pardon Our Interruption")
    assert "browser check" in reason
    page = DiscoveredPage(
        url="https://insurer.example:443/",
        title="Access Denied",
        http_status=200,
        challenge=reason,
    )
    cases = cases_for_page(page, 2000)
    assert len(cases) == 1
    assert cases[0].blocked_reason == reason
    assert cases[0].steps == []
    assert challenge_reason("Lemonade", "Insurance made easy", "Get a quote") == ""


def test_cost_ledger_does_not_invent_prices(tmp_path: Path):
    write_cost(tmp_path, CostLedger(crawl_pages=1, tool_calls=1, wall_clock_seconds=1.2))
    text = (tmp_path / "cost-ledger.md").read_text(encoding="utf-8")
    assert "not calculated" in text
    assert "$" not in text
    assert "USD" not in text
