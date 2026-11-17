"""RootCauseAgent: name the cause in one direct call; run the tool loop only when unsure.

The prompt starts with the same case brief as the resolution prompt (shared system prompt, then
evidence, then knowledge), so the local server reuses its cache for that prefix on the second call.
"""

from pipeline_incident_copilot.agents.base import evidence_brief, load_prompt
from pipeline_incident_copilot.guardrails.action import safe_actions
from pipeline_incident_copilot.learning.confidence_calibrator import calibrate
from pipeline_incident_copilot.llm import model_router
from pipeline_incident_copilot.schemas.agent_outputs import Diagnosis, RootCause
from pipeline_incident_copilot.schemas.triage_state import TriageState
from pipeline_incident_copilot.tools.registry import ToolRegistry
from pipeline_incident_copilot.tuning.tunables import get_tunables

MAX_TOOL_ROUNDS = 3
SURE = get_tunables().root_cause_sure  # below this (or needs_human) the agent re-checks facts with tools
MAX_TOKENS = 450  # root cause + fix in one answer


def knowledge_brief(state: TriageState) -> str:
    k = state["knowledge"]
    lines = [f"Relevant knowledge ({'weak match' if k.weak else 'good match'}):"]
    for c in k.citations:
        hint = (
            " (a past fix that worked: a hint only, confirm it against the live evidence)"
            if c.source_type == "incident"
            else ""
        )
        lines.append(f"[{c.parent_id}] {c.title}{hint}\n{c.snippet.strip()[:220]}\n")
    return "\n".join(lines)


def case_brief(state: TriageState) -> str:
    """The shared prompt prefix: evidence, then knowledge (identical for root cause and resolution)."""
    return f"{evidence_brief(state)}\n\n{knowledge_brief(state)}"


class RootCauseAgent:
    name = "root_cause"

    def __init__(self, tools: ToolRegistry):
        self.tools = tools

    def run(self, state: TriageState) -> dict:
        c, la = state["classification"], state["log_analysis"]
        prompt = "\n\n".join(
            [
                case_brief(state),
                f"Classification: {c.failure_class} ({c.rationale}). "
                f"Error {la.error_code or '-'} ({la.exception_type or '-'}) at: {la.failing_operation}",
                load_prompt("root_cause"),
            ]
        )
        system = load_prompt("analyst")
        from pipeline_incident_copilot.agents.resolution_agent import candidate_fix  # avoid an import cycle

        fast_prompt = "\n\n".join(
            part
            for part in [
                prompt,
                candidate_fix(state),
                safe_actions.description_for(state["incident"]),
                load_prompt("resolution"),
            ]
            if part
        )
        diagnosis = model_router.structured("root_cause", fast_prompt, Diagnosis, system=system, max_tokens=MAX_TOKENS)
        result, checks, draft = diagnosis.root_cause, [], diagnosis.resolution
        if result.confidence < SURE or result.needs_human:
            draft = None  # careful path: re-check with tools, then write the fix separately
            result, checks = model_router.investigate(
                "root_cause",
                prompt + "\nYou were unsure: confirm with the read-only tools, then answer.",
                RootCause,
                self.tools,
                system=system,
                max_rounds=MAX_TOOL_ROUNDS,
            )
        calibrated, sig = calibrate(
            {**state, "root_cause": result, "extra_checks": [*state.get("extra_checks", []), *checks]}
        )
        return {
            "confidence": {"model": result.confidence, "calibrated": calibrated, "signals": sig},
            "draft_resolution": draft,  # None clears a stale draft (e.g. after a failed rerun)
            "root_cause": result,
            "extra_checks": checks,
            "trail": [
                f"root cause (model {result.confidence:.2f}, evidence-calibrated {calibrated:.2f}): {result.cause}"
            ]
            + [f"checked {r.tool}: {r.summary}" for r in checks],
        }
