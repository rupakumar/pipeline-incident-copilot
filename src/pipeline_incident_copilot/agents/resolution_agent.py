"""ResolutionAgent: fix steps plus at most one safe automated action (checked by the action guardrail)."""

from pipeline_incident_copilot.agents.base import load_prompt
from pipeline_incident_copilot.agents.root_cause_agent import case_brief
from pipeline_incident_copilot.connectors.databricks.evidence import volume_path_in
from pipeline_incident_copilot.guardrails.action import safe_actions
from pipeline_incident_copilot.llm import model_router
from pipeline_incident_copilot.observability.langfuse_tracer import get_langfuse
from pipeline_incident_copilot.schemas.agent_outputs import Resolution
from pipeline_incident_copilot.schemas.triage_state import TriageState

MAX_TOKENS = 300
RESETTABLE = {"input_path", "timestamp_format", "gold_table", "debug_sample_rows", "enrich_columns"}


def candidate_fix(state: TriageState) -> str:
    """'Undo what changed': parameters that differ from the last successful run and may be reset."""
    c = state["evidence"].comparison
    changed = {k: v for k, v in (c.changed_parameters if c else {}).items() if k in RESETTABLE}
    if not changed:
        return ""
    params = {k: good for k, (good, _bad) in changed.items()}
    undo = ", ".join(f"{k} '{bad}' → '{good or '(default)'}'" for k, (good, bad) in changed.items())
    return f"Candidate fix (undo what changed since the last success: {undo}): rerun_job {params}"


def effective_parameters(state: TriageState) -> dict[str, str]:
    """The failed run's parameters with defaults resolved where we know them (the input path it actually
    read), so a rerun that spells out the same default path counts as 'no change'."""
    params = dict(state["incident"].run.parameters)
    ev = state["evidence"]
    read = (ev.comparison.failed.input_path if ev.comparison and ev.comparison.failed else None) or volume_path_in(
        ev.error
    )
    if read and not params.get("input_path"):
        params["input_path"] = read.removeprefix("dbfs:")
    return params


class ResolutionAgent:
    name = "resolution"

    def run(self, state: TriageState) -> dict:
        rc = state["root_cause"]
        checks = state.get("extra_checks", [])
        prompt = "\n\n".join(
            part
            for part in [
                case_brief(state),  # same prefix as the root-cause prompt → reused from the server cache
                "\n".join(f"Extra check {r.tool}: {r.summary}" for r in checks),
                f"Root cause (confidence {rc.confidence:.2f}): {rc.cause}",
                candidate_fix(state),
                safe_actions.description_for(state["incident"]),
                load_prompt("resolution"),
            ]
            if part
        )
        result = state.get("draft_resolution") or model_router.structured(
            "resolution", prompt, Resolution, system=load_prompt("analyst"), max_tokens=MAX_TOKENS
        )
        c = state["evidence"].comparison
        last_good = c.last_success.parameters if c and c.last_success else None
        with get_langfuse().start_as_current_observation(
            as_type="guardrail", name="check-safe-action", input=result.action.model_dump() if result.action else None
        ) as guard:
            why = safe_actions.check(
                result.action,
                effective_parameters(state),
                last_good,
                safe_actions.rules_for(state["incident"]),
                failure_class=state["root_cause"].failure_class,
                causes=safe_actions.causes_for(state["incident"]),
            )
            guard.update(output={"allowed": why is None, "reason": why}, level="WARNING" if why else "DEFAULT")
        if why is not None:
            result = result.model_copy(update={"action": None, "action_rejected_reason": why})
        action = (
            f"rerun_job {result.action.parameters}"
            if result.action
            else f"no automated action{f' ({result.action_rejected_reason})' if result.action_rejected_reason else ''}"
        )
        return {"resolution": result, "trail": [f"resolution: {result.summary} → {action}"]}
