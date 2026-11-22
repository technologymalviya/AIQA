from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.mind.plan import FlowDecision
from app.models import RunRequest
from app.orchestrator import _keep_signed_in, queue_continuation


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        runs_dir=tmp_path,
        navigation_timeout_ms=2000,
        action_timeout_ms=2000,
        run_timeout_s=30,
        max_pages_cap=25,
        llm_base_url="http://127.0.0.1:9/v1",
        llm_api_key=None,
        llm_model="test-model",
        browser_channel=None,
        retain_pass_media=False,
    )


def _finished(tmp_path: Path):
    app = create_app(_settings(tmp_path))
    record = app.state.store.create(
        RunRequest(
            target_url="http://127.0.0.1:9/demo/",
            mode="hybrid",
            requirements_text="test end to end",
            authorization_confirmed=True,
        )
    )
    app.state.store.update(record["run_id"], status="completed", phase="completed")
    return app, record["run_id"]


def test_a_finished_run_keeps_the_session_for_the_next_prompt(tmp_path: Path, monkeypatch):
    app, run_id = _finished(tmp_path)

    async def _quiet(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.main.continue_run", _quiet)
    client = TestClient(app)
    page = client.get(f"/runs/{run_id}")
    assert "Continue this session" in page.text
    assert 'id="next_prompt"' in page.text
    response = client.post(
        f"/runs/{run_id}/continue",
        data={"requirements_text": "Check the delivery boy flow"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    saved = app.state.store.load(run_id)
    assert saved["status"] == "queued"
    assert saved["phase"] == "continue"
    assert saved["request"]["requirements_text"] == "Check the delivery boy flow"
    assert saved["prompts"] == ["test end to end", "Check the delivery boy flow"]
    assert saved["cancel_requested"] is False


def test_an_empty_prompt_does_not_start_another_pass(tmp_path: Path):
    app, run_id = _finished(tmp_path)
    response = TestClient(app).post(
        f"/runs/{run_id}/continue",
        data={"requirements_text": "   "},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert app.state.store.load(run_id)["status"] == "completed"


def test_a_running_session_is_not_queued_again(tmp_path: Path):
    app, run_id = _finished(tmp_path)
    app.state.store.update(run_id, status="running")
    try:
        queue_continuation(app.state.store, run_id, "Open settings")
    except RuntimeError as exc:
        assert "still running" in str(exc)
    else:
        raise AssertionError("a running session accepted another prompt")


def test_a_follow_up_stays_with_the_remembered_account():
    decision = FlowDecision(summary="Remembered.", sign_in=False, create_account=False)
    kept = _keep_signed_in(
        decision,
        {"signup_done": True, "username": "remembered"},
        "Open the customer list",
    )
    assert kept.sign_in is True
    assert kept.create_account is False
    refused = _keep_signed_in(
        FlowDecision(summary="Public.", sign_in=False, create_account=False),
        {"signup_done": True, "username": "remembered"},
        "Check the public pages without an account",
    )
    assert refused.sign_in is False
