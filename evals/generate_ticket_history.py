"""Generate the support-ticket history from TRAIN failure runs only.

Facts (error, root cause, fix, run link, dates) come from the scenario catalog and the run;
the local LLM only writes the human parts of a ticket (vague title, description, a wrong
first guess, investigation notes). Test scenarios never become tickets, and a leak guard
fails the run if any test scenario's specific details appear in a ticket.

Usage: python -m evals.generate_ticket_history
"""

import os
import sys
from datetime import datetime, timedelta

# One-off generation; no need to trace it.
os.environ.setdefault("LANGFUSE_TRACING_ENABLED", "false")

from pydantic import BaseModel, Field  # noqa: E402

from evals.dataset_loader import load_failed_runs, scenario_params, scenarios_by_id  # noqa: E402
from pipeline_incident_copilot.llm import model_router  # noqa: E402
from pipeline_incident_copilot.rag.ingestion.loaders import KNOWLEDGE_DIR  # noqa: E402
from pipeline_incident_copilot.rag.retrieval.query.log_distiller import distill  # noqa: E402

TICKETS_DIR = KNOWLEDGE_DIR / "tickets"
FIRST_TICKET_NUMBER = 1001
HISTORY_START = datetime(2026, 7, 6, 6, 10)
REPORTERS = ["on-call data engineer", "production support", "analytics team", "dashboard owner"]
SEVERITY = {
    "missing_input": "sev2",
    "schema_change": "sev2",
    "data_quality": "sev2",
    "permissions": "sev3",
    "resource_exhaustion": "sev3",
}
SYSTEM = "You write realistic internal support tickets for a data engineering team. Be brief and plain."


class TicketNarrative(BaseModel):
    title: str = Field(description="Vague ticket title as a reporter would write it, max 10 words, no root cause")
    description: str = Field(description="2-3 sentences: what the reporter observed and its impact")
    first_guess: str = Field(description="One plausible but WRONG first hypothesis the engineer had")
    investigation_notes: str = Field(description="2-3 sentences: how the engineer found the real cause")


def narrate(run: dict, scenario: dict, evidence: str, reporter: str) -> TicketNarrative:
    prompt = (
        f"Job loghub_etl ({run['source']} logs) failed. Reporter: {reporter}.\n"
        f"Error evidence:\n{evidence}\n\n"
        f"Actual root cause (found later): {scenario['root_cause']}\n"
        f"Fix applied: {scenario['fix']}\n\n"
        "Write the human parts of the support ticket."
    )
    return model_router.structured("write_ticket", prompt, TicketNarrative, system=SYSTEM, max_tokens=400)


def render(number: int, run: dict, scenario: dict, evidence: str, story: TicketNarrative, reporter: str) -> str:
    opened = HISTORY_START + timedelta(days=6 * (number - FIRST_TICKET_NUMBER))
    resolved = opened + timedelta(hours=2 + (number % 3))
    ticket_id = f"TCK-{number}"
    return f"""---
ticket_id: {ticket_id}
title: "{story.title.replace('"', "'")}"
status: resolved
severity: {SEVERITY[run["failure_class"]]}
opened_at: {opened.isoformat()}Z
resolved_at: {resolved.isoformat()}Z
failure_class: {run["failure_class"]}
source_run_id: {run["run_id"]}
reporter: {reporter}
---

# {ticket_id}: {story.title}

## Report

{story.description}

Job: `loghub_etl` ({run["source"]}) · failed run: {run["run_page_url"]}

```
{evidence}
```

## Timeline

- {opened:%Y-%m-%d %H:%M} UTC — reported by {reporter}
- First hypothesis: {story.first_guess}
- Investigation: {story.investigation_notes}
- {resolved:%Y-%m-%d %H:%M} UTC — resolved

## Root cause

{scenario["root_cause"]}

## Resolution

{scenario["fix"]}
"""


def held_out_values(catalog: dict) -> set[str]:
    """Distinctive parameter values only TEST scenarios use (paths, tables, formats, dates) — must never leak.

    Values that train scenarios share (e.g. a source's normal run_date) are not test-specific."""

    def distinctive(split: str) -> set[str]:
        return {
            value
            for scenario in catalog.values()
            if scenario.get("split") == split
            for key, value in scenario_params(scenario).items()
            if key != "source" and ("/" in value or "." in value or len(value) >= 8)
        }

    return distinctive("test") - distinctive("train") - distinctive("baseline")


def main() -> None:
    catalog = scenarios_by_id()
    train_runs = sorted(load_failed_runs("train"), key=lambda r: r["scenario_id"])
    TICKETS_DIR.mkdir(parents=True, exist_ok=True)
    for old in TICKETS_DIR.glob("TCK-*.md"):
        old.unlink()

    tickets = {}
    try:
        for i, run in enumerate(train_runs):
            number = FIRST_TICKET_NUMBER + i
            reporter = REPORTERS[i % len(REPORTERS)]
            scenario = catalog[run["scenario_id"]]
            evidence = "\n".join(distill(run["error"] or "", run["error_trace"] or "").splitlines()[:6])
            story = narrate(run, scenario, evidence, reporter)
            tickets[f"TCK-{number}"] = render(number, run, scenario, evidence, story, reporter)
            print(f"TCK-{number}  {run['scenario_id']:<6} {story.title}")
    finally:
        model_router.unload_local()  # free the local model's memory right away

    leaks = {tid: v for tid, text in tickets.items() for v in held_out_values(catalog) if v in text}
    if leaks:
        sys.exit(f"leak guard: test-scenario details found in tickets: {leaks}")
    for ticket_id, text in tickets.items():
        (TICKETS_DIR / f"{ticket_id}.md").write_text(text)
    print(f"wrote {len(tickets)} tickets to {TICKETS_DIR} (leak guard passed)")


if __name__ == "__main__":
    main()
