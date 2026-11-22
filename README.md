# Autonomous QA Orchestrator — Phase 0

Local FastAPI and Playwright Chromium pilot. It crawls one authorized origin, writes evidence-grounded smoke cases, and runs them with a fixed action list. No LLM key is required.

Phase 0 does not include Android, iOS, TV, Postgres, Redis, or Jira/GitHub publishing. Those capabilities are reported as `NOT_IMPLEMENTED`.

## Install

Python 3.12. From this directory:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

`requirements.txt` pins each dependency to the minor line that was installed. `requirements.lock` is the exact freeze of that environment.

## Start

```bash
. .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

- Dashboard: http://127.0.0.1:8000
- Demo app: http://127.0.0.1:8000/demo/

Confirm authorization, leave mode on Auto, and start a run against the demo URL. The demo has Home, Pricing, Help Centre, and a linked missing page. Pricing is a runtime-error fixture. `/demo/fixtures/slow` is an environment-timeout fixture and is not linked from the demo nav.

Optional model calls use `QA_LLM_API_KEY`, `QA_LLM_BASE_URL`, and `QA_LLM_MODEL`. Leave the key unset for the deterministic fallback. The key is not written into reports.

## Tests

```bash
. .venv/bin/activate
python -m compileall app
python -m pytest
```

## Where artefacts go

Each run is a directory under `runs/<run_id>/` (override with `QA_RUNS_DIR`):

- `application-understanding.md`, `test-cases.md`, `results.md`, `cost-ledger.md`, `open-questions.md`, `traceability.md`
- `defects/DEF-TC-*.md` for FAIL cases
- `evidence/<case_id>/video.webm`, `screenshot.png`, and `trace.zip` for FAIL and INCONCLUSIVE cases

Passing cases do not keep video, screenshot, or trace. Monetary cost is left uncalculated unless a versioned price table is supplied. None is supplied in this pilot.

## Safety boundaries

The crawl is same-origin, GET-only, and does not record input values. Model output cannot add an off-origin URL, a shell or JavaScript action, or a path outside the run directory. Link-local and cloud-metadata addresses are rejected. Loopback is allowed only when that exact host and port was authorized.
