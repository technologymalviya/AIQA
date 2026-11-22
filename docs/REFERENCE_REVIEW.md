# Reference review

Checked 22 November 2026. These repositories were read as architecture references. No source was copied. A README was not treated as evidence that a project is production-ready. Stars, licence files, and dependency claims were checked against the tree that was actually fetched.

Official docs rechecked the same day:

- Playwright video: https://playwright.dev/docs/videos — recording is off by default; `retain-on-failure` removes video from successful tests; a manually created context must be closed before the file is available. The pilot uses the Python context options `record_video_dir` and `record_video_size`, then `video.save_as` or `video.delete` after close.
- Playwright trace viewer: https://playwright.dev/docs/trace-viewer — `tracing.start` / `tracing.stop(path=...)` writes a zip. The pilot starts a trace per case and exports it only for FAIL and INCONCLUSIVE.
- OpenAI structured outputs: https://developers.openai.com/api/docs/guides/structured-outputs — `response_format` `json_schema` with `strict` is sent on the optional chat-completions call. The local validator still runs, because compatible servers differ and page text is untrusted.

Research named in the master spec (not re-executed here): arXiv 2504.01495, 2406.12952, and 2603.25226. The conclusion kept from the spec is that an agent narrative is not a verdict. This pilot separates design from a deterministic checker and does not claim it finds every defect.

## QA-Swarm

- Repo: https://github.com/Vaskoor/qa-swarm
- Default branch: `main`
- Tip commit: `3295c91289af0372b865f5d88356304108a3b828` (2026-10-18, “add bug report template”)
- Licence: inconsistent. `package.json` and the README badge say Apache-2.0. The root `LICENSE` file is the MIT licence text, copyright 2026 Bhaskar Kaphle. Nothing was copied, so this conflict did not need to be resolved by reuse.
- Adopted as ideas: separate explorer, generator, executor, triage, and reporter stages; a shared run record; a provider switch that can be absent; stop when a budget runs out.
- Observed limits: 0 stars, version 0.1.0, and the architecture doc argues against requiring LangGraph while `package.json` depends on it. Self-healing and quarantine are described. The sample CLI transcript is marketing text, not a result from this pilot.
- Not copied: LangGraph, Qdrant, Postgres, the self-heal path, generated regression files, or any of its TypeScript.

## Multi-Agent QA Orchestrator

- Repo: https://github.com/sakshammhere/Multi-Agent-QA-Orchestrator
- Default branch: `main`
- Tip commit: `6ecc76aa18faf4e37e3ea65a05dec7ac9c09153e` (2026-07-25, “Make security scans non-blocking”)
- Licence: no `LICENSE` file and no licence section in the README. The GitHub API reported no SPDX licence. Code was not copied.
- Adopted as ideas: document-or-URL intake, a bounded crawler, cases checked against elements that were actually seen, Playwright execution, separate capture and report steps.
- Observed limits: about 1 star. `requirements.txt` says the deterministic mock was removed and `OPENAI_API_KEY` is required. The README also describes an LLM-as-judge and a simulate mode. The tree is large (on the order of 80 MB) and includes an `outputs/` area. That is not a library to vendor.
- Not copied: LLM verdicts, Supabase, Excel/PDF report code, self-healing selectors, or the requirement that a run fail closed when no model key is set. This pilot’s fallback is the opposite of that requirement.

## QA Agent Lab

- Repo: https://github.com/relinxx/qa_application
- Default branch: `main`
- Tip commit: `f714c757f3b67294c07a49436e52d5c40b9cec25` (2026-06-18, “docs: redesign project overview and architecture”)
- Licence: no `LICENSE` file and no licence grant in the README. Code was not copied.
- Adopted as ideas: a bounded tool loop, URL and path checks, timeouts, and a live progress surface. This pilot uses a run directory plus a reloading page instead of MCP SSE.
- Observed limits: about 1 star. The README presents a portfolio demo with fixed sandbox apps. Server dependencies in `requirements.txt` are unpinned (`openai`, `python-dotenv`).
- Not copied: MCP Playwright, GPT-4o choosing browser tools, or writing executable Playwright files and then launching them. Model output here cannot become JavaScript or a shell command.

## AutonomousQA Agent

- Repo: https://github.com/iklymchuk/autonomous-qa-agent
- Default branch: `main`
- Tip commit: `5adb220d71225c5499275293b3a7963687f8729d` (2026-03-26, “add test coverage for test reporters”)
- Licence: no `LICENSE` file and no licence field in `pyproject.toml`. Code was not copied.
- Adopted as ideas: BFS page discovery, a trace per executed case, and a report that a person can open without a model.
- Observed limits: about 11 stars and 2 forks. The README calls the project production-grade and requires `OPENAI_API_KEY` for the demo. Accessibility and pixel diff are described; they are not in Phase 0.
- Not copied: generated pytest files, CLI codegen, axe injection, or Pillow diffs.

## QA exploratory agent

- Repo: https://github.com/crislerwin/qa-agent
- Default branch: `main`
- Tip commit: `c9cb81e521f3070d8136e872a5bd9be4e4153d8a` (2026-04-25, “fix(list_sessions): handle missing or numeric startTime/endTime”)
- Licence: no licence file, no licence field in `package.json`, and no licence section in the README. Code was not copied.
- Adopted as ideas: treat console errors and failed navigations as observations, and keep the action choice separate from the observation.
- Observed limits: about 1 star. The README targets one public practice site and an open observe/think/act loop. The package name in `package.json` is `agentforge`.
- Not copied: the free-form act loop, LangChain tool selection, OTP helpers, or SQLite session code.

## What this pilot kept from that reading

Stages stay separate, and the executor’s verdict does not read a model field. Discovery is bounded. Selectors and URLs have to have been seen, except for the health invariants “HTTP 2xx” and “no page error”, which are expectations rather than copies of a broken page. Video and trace follow Playwright’s retain-on-failure behaviour. None of the reference trees were imported.
