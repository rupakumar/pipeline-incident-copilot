"""What each agent returns. Typed, so the graph can route on it and evals can score it."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

FailureClass = Literal["missing_input", "schema_change", "permissions", "data_quality", "resource_exhaustion", "other"]
Severity = Literal["sev1", "sev2", "sev3"]


class IncidentClassification(BaseModel):
    failure_class: FailureClass = Field(description="Which kind of failure this is")
    severity: Severity = Field(
        description="sev1 = data loss or many consumers down; sev2 = a daily load failed; sev3 = minor"
    )
    rationale: str = Field(description="One sentence: why this class")

    @field_validator("failure_class", mode="before")
    @classmethod
    def _normalise(cls, v):
        return v.strip().lower().replace(" ", "_").replace("-", "_") if isinstance(v, str) else v


class LogAnalysis(BaseModel):
    error_code: str | None = Field(None, description="Error class, e.g. PATH_NOT_FOUND")
    exception_type: str | None = None
    failing_operation: str = Field(description="What the code was doing when it failed")
    failing_task: str | None = None
    key_lines: list[str] = Field(default_factory=list, description="The few log lines that matter")
    search_query: str = Field(description="Query used to search the knowledge base")


class Citation(BaseModel):
    parent_id: str  # e.g. runbook:missing-input-data, ticket:TCK-1004, spark:PATH_NOT_FOUND
    source_type: str
    title: str
    snippet: str
    score: float  # reranker score; higher = more relevant


class KnowledgeContext(BaseModel):
    citations: list[Citation]
    queries: list[str]  # every query tried, in order
    retries: int = 0  # corrective-RAG rewrites
    weak: bool = False  # True when even after retry few documents looked relevant


class RootCause(BaseModel):
    cause: str = Field(description="The specific root cause, under 30 words")
    failure_class: FailureClass
    confidence: float = Field(ge=0, le=1, description="0-1: how sure, given the evidence")
    evidence_refs: list[str] = Field(
        description="Up to 3 things it rests on: check names (check_path_exists) or document ids (ticket:TCK-1004)"
    )
    needs_human: bool = Field(description="True if a person must decide, e.g. the evidence conflicts")


class ProposedAction(BaseModel):
    name: Literal["rerun_job"] = Field(description="The only automated action available")
    parameters: dict[str, str] = Field(
        description="Job parameters to set for the rerun; empty string resets a parameter to its default"
    )
    reason: str


class Resolution(BaseModel):
    summary: str = Field(description="What to do, under 20 words")
    steps: list[str] = Field(description="2-3 short steps a person follows, in order")
    action: ProposedAction | None = Field(
        description="A safe automated rerun that fixes it, or null when a person must act first (e.g. upstream)"
    )
    risk: Literal["low", "medium", "high"]
    verify: list[str] = Field(description="1 check that confirms the fix worked")
    action_rejected_reason: str | None = None  # set by the guardrail if the proposed action was not allowed


class Diagnosis(BaseModel):
    """Root cause and fix in one answer (the fast path: one model call when the cause is clear)."""

    root_cause: RootCause
    resolution: Resolution
