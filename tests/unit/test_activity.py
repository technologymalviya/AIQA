from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import RunRequest


def test_the_activity_log_keeps_every_line(tmp_path: Path):
    settings = Settings(
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
    app = create_app(settings)
    record = app.state.store.create(
        RunRequest(
            target_url="http://127.0.0.1:9/demo/",
            mode="auto",
            authorization_confirmed=True,
        )
    )
    for index in range(90):
        app.state.store.event(record["run_id"], "mind", {"note": f"step {index}"})
    body = TestClient(app).get(f"/api/v1/runs/{record['run_id']}/activity").json()
    assert len(body["events"]) == 91
    assert body["events"][0]["caption"] == "Run accepted"
    assert body["events"][-1]["caption"] == "step 89"
