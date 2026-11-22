"""Process settings. Secrets are read from the environment and never copied into reports."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int_env(name: str, default: int, *, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(low, min(high, value))


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    runs_dir: Path
    navigation_timeout_ms: int
    action_timeout_ms: int
    run_timeout_s: int
    max_pages_cap: int
    llm_base_url: str
    llm_api_key: str | None
    llm_model: str
    browser_channel: str | None
    retain_pass_media: bool

    def public_dict(self) -> dict[str, object]:
        """Settings safe to write into a run record. The API key is omitted."""
        return {
            "runs_dir": str(self.runs_dir),
            "navigation_timeout_ms": self.navigation_timeout_ms,
            "action_timeout_ms": self.action_timeout_ms,
            "run_timeout_s": self.run_timeout_s,
            "max_pages_cap": self.max_pages_cap,
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "llm_configured": bool(self.llm_api_key),
            "browser_channel": self.browser_channel or "bundled-chromium",
            "retain_pass_media": self.retain_pass_media,
        }


def load_settings() -> Settings:
    runs = os.environ.get("QA_RUNS_DIR", "runs").strip() or "runs"
    channel = os.environ.get("QA_BROWSER_CHANNEL", "").strip() or None
    key = os.environ.get("QA_LLM_API_KEY", "").strip() or None
    base = os.environ.get("QA_LLM_BASE_URL", "https://api.openai.com/v1").strip()
    model = os.environ.get("QA_LLM_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
    return Settings(
        runs_dir=Path(runs),
        navigation_timeout_ms=_int_env("QA_NAVIGATION_TIMEOUT_MS", 6000, low=500, high=60000),
        action_timeout_ms=_int_env("QA_ACTION_TIMEOUT_MS", 6000, low=500, high=60000),
        run_timeout_s=_int_env("QA_RUN_TIMEOUT_S", 420, low=10, high=900),
        max_pages_cap=_int_env("QA_MAX_PAGES_CAP", 60, low=1, high=80),
        llm_base_url=base.rstrip("/"),
        llm_api_key=key,
        llm_model=model,
        browser_channel=channel,
        retain_pass_media=_bool_env("QA_RETAIN_PASS_MEDIA", False),
    )
