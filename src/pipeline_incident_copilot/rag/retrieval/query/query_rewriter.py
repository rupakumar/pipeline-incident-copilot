"""LLM query rewriting: distilled failure evidence → structured QueryPlan → search query."""

from pydantic import BaseModel, Field, field_validator

from pipeline_incident_copilot.llm import model_router


class QueryPlan(BaseModel):
    error_code: str | None = Field(None, description="Error class in brackets, e.g. PATH_NOT_FOUND, or null")
    exception_type: str | None = Field(None, description="e.g. AnalysisException, AssertionError, or null")
    failing_operation: str = Field(description="What the code was doing when it failed, in a few words")
    search_query: str = Field(description="One concise query to search an error catalog and pipeline code")

    @field_validator("error_code", "exception_type", mode="before")
    @classmethod
    def _null_strings(cls, v):
        # small models often write the string "null" instead of JSON null
        return None if isinstance(v, str) and v.strip().lower() in {"", "null", "none", "n/a"} else v


TRIAGE_SYSTEM_PROMPT = "You are an on-call data engineer triaging a failed Databricks job. Be concise and literal."


def llm_rewrite(distilled_log: str) -> QueryPlan:
    return model_router.structured(
        "query_rewrite",
        f"Failed job evidence:\n{distilled_log}\n\nExtract the key facts and write a search query.",
        QueryPlan,
        system=TRIAGE_SYSTEM_PROMPT,
    )


def plan_to_query(plan: QueryPlan) -> str:
    parts = [plan.error_code, plan.exception_type, plan.failing_operation, plan.search_query]
    return " ".join(p for p in parts if p)
