"""The agent contract, the prompt loader, and the evidence brief every agent's prompt starts from."""

import os
from functools import cache
from pathlib import Path
from typing import Protocol

from pipeline_incident_copilot.schemas.triage_state import TriageState

DEFAULT_PROMPTS_DIR = Path(__file__).parent / "prompts"
PROMPTS_DIR = Path(os.getenv("COPILOT_PROMPTS_DIR", str(DEFAULT_PROMPTS_DIR)))  # candidates are evaluated from a copy


class Agent(Protocol):
    name: str

    def run(self, state: TriageState) -> dict:
        """Return only the TriageState fields this agent adds."""
        ...


@cache
def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text().strip()


def evidence_brief(state: TriageState) -> str:
    """Compact, factual summary of the incident and its standard evidence (no guesses)."""
    inc, ev = state["incident"], state["evidence"]
    databricks = inc.run.platform == "databricks"  # the original prompt, kept byte-identical for the evals
    lines = [
        f"Incident {inc.incident_id}: job {inc.run.job_name}, source {inc.source}, run_date {inc.run_date}"
        if databricks
        else f"Incident {inc.incident_id}: {inc.run.platform} job {inc.run.job_name}, run {inc.run.run_id}",
        f"Failed task: {ev.failed_task}",
        f"Error: {(ev.error or '(none)').strip().splitlines()[0][:400] if ev.error else '(none)'}",
        "Distilled log:",
        *("  " + line for line in ev.distilled_log.splitlines()[:5]),
    ]
    c = ev.comparison
    if c and c.last_success:
        changed = "; ".join(f"{k}: '{a}' → '{b}'" for k, (a, b) in c.changed_parameters.items()) or "none"
        lines += [
            f"Last successful run: {c.last_success.job_run_id} (run_date {c.last_success.run_date}, rows bronze "
            f"{c.last_success.bronze_rows} / silver {c.last_success.silver_rows} / gold {c.last_success.gold_rows})"
            if databricks
            else f"Last successful run: {c.last_success.job_run_id}",
            f"Parameters changed since then: {changed}",
            f"Code changed since then: {'yes' if c.code_changed else 'no'}",
        ]
        if c.failed and databricks:
            f = c.failed
            lines.append(f"Failed run rows: bronze {f.bronze_rows} / silver {f.silver_rows} / gold {f.gold_rows}")
    # "" means "job default" — listing it invites the misreading "parameter is missing"
    set_params = {k: v for k, v in inc.run.parameters.items() if v != ""}
    lines.append(f"Failed run parameters (all others at their defaults): {set_params}")
    if ev.code_changes:
        lines.append("Code changes: " + ", ".join(f"{x.status} {x.path}" for x in ev.code_changes))
    lines += ["Checks already made:", *(f"  - {r.tool}: {r.summary}" for r in ev.tool_results)]
    lines += [f"  - not collected: {p}" for p in ev.problems]
    return "\n".join(lines)
