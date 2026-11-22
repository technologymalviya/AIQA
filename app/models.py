"""Versioned contracts for a Phase 0 run. Schema version 1.0.0."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import SCHEMA_VERSION


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"


class Classification(str, Enum):
    """Triage label. It never replaces the verdict."""

    NONE = "none"
    PRODUCT_BUG = "product_bug"
    TEST_BUG = "test_bug"
    FLAKY = "flaky"
    ENVIRONMENT = "environment"
    DATA_ISSUE = "data_issue"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class Provenance(str, Enum):
    LLM = "llm"
    HEURISTIC = "heuristic"
    MANUAL = "manual"


class FailureKind(str, Enum):
    NONE = "none"
    REACHABILITY = "reachability"
    RUNTIME_ERROR = "runtime_error"
    CONTENT = "content"


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_url: str = Field(min_length=1, max_length=2000)
    mode: Literal["auto", "hybrid"]
    requirements_text: str = Field(default="", max_length=20000)
    max_pages: int = Field(default=8, ge=1, le=80)
    max_depth: int = Field(default=2, ge=0, le=4)
    authorization_confirmed: bool

    @field_validator("authorization_confirmed")
    @classmethod
    def must_confirm(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("authorization_confirmed must be true")
        return value


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["url", "heading", "console", "requirement"]
    ref: str
    observed_at: str
    detail: str = ""


class TestStep(BaseModel):
    """One constrained action. There is no field for raw JavaScript or shell."""

    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "goto",
        "assert_http_status",
        "assert_title",
        "assert_heading",
        "assert_text",
        "assert_no_page_errors",
    ]
    target: str = ""
    expected: str = ""
    timeout_ms: int = Field(default=6000, ge=100, le=30000)


class TestCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    test_case_id: str
    version: int = 1
    title: str
    summary: str
    priority: Literal["P0", "P1", "P2", "P3"]
    risk: Literal["low", "medium", "high"]
    platform: Literal["web"] = "web"
    feature: str
    requirement_ids: list[str] = Field(default_factory=list)
    source_evidence: list[EvidenceRef] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    test_data_refs: list[str] = Field(default_factory=list)
    steps: list[TestStep] = Field(default_factory=list)
    expected_outcomes: list[str] = Field(default_factory=list)
    cleanup_steps: list[str] = Field(default_factory=list)
    automation_status: Literal["automated", "manual"] = "automated"
    confidence: float = Field(ge=0.0, le=1.0)
    assumptions: list[str] = Field(default_factory=list)
    review_required: bool = False
    owner: str = "qa-orchestrator"
    tags: list[str] = Field(default_factory=list)
    provenance: Provenance
    blocked_reason: str = ""


class FormField(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tag: str = ""
    type: str = ""
    name: str = ""
    required: bool = False


class FormMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    method: str = "get"
    action: str = ""
    fields: list[FormField] = Field(default_factory=list)


class LinkInfo(BaseModel):
    label: str
    href: str
    same_origin: bool
    skipped_reason: str = ""


class DiscoveredPage(BaseModel):
    url: str
    final_url: str = ""
    http_status: int | None = None
    title: str = ""
    app_version: str = ""
    headings: list[str] = Field(default_factory=list)
    primary_heading: str = ""
    text_excerpt: str = ""
    links: list[LinkInfo] = Field(default_factory=list)
    buttons: list[str] = Field(default_factory=list)
    forms: list[FormMeta] = Field(default_factory=list)
    console_errors: list[str] = Field(default_factory=list)
    page_errors: list[str] = Field(default_factory=list)
    load_error: str = ""
    challenge: str = ""
    redirect_escape: bool = False
    depth: int = 0
    elapsed_ms: int = 0


class Discovery(BaseModel):
    schema_version: str = SCHEMA_VERSION
    target_url: str
    origin: str
    pages: list[DiscoveredPage] = Field(default_factory=list)
    external_links: list[str] = Field(default_factory=list)
    denylisted: list[str] = Field(default_factory=list)
    coverage_notes: list[str] = Field(default_factory=list)
    stopped_reason: str = ""


class Requirement(BaseModel):
    requirement_id: str
    text: str
    ambiguous: bool = False
    destructive: bool = False
    linked_case_ids: list[str] = Field(default_factory=list)


class StepResult(BaseModel):
    action: str
    passed: bool
    expected: str = ""
    actual: str = ""
    error: str = ""
    kind: Literal["assertion", "runner", "policy"] = "assertion"
    duration_ms: int = 0


class CaseResult(BaseModel):
    schema_version: str = SCHEMA_VERSION
    test_case_id: str
    title: str
    provenance: Provenance
    verdict: Verdict
    classification: Classification
    failure_kind: FailureKind = FailureKind.NONE
    classification_note: str = "Classification does not change the verdict."
    reason: str = ""
    duration_ms: int = 0
    steps: list[StepResult] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)


class CostLedger(BaseModel):
    schema_version: str = SCHEMA_VERSION
    price_configuration: None = None
    monetary_cost: None = None
    monetary_cost_note: str = (
        "not calculated; no versioned price configuration was supplied"
    )
    provider: str | None = None
    model: str | None = None
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    crawl_pages: int = 0
    browser_sessions: int = 0
    tool_calls: int = 0
    wall_clock_seconds: float = 0.0
    artefact_bytes: int = 0
    evidence_bytes: int = 0
