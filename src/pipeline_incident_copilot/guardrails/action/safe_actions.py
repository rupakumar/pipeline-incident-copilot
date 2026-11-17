"""The safe-action allowlist. Only `rerun_job` exists, and only with parameter values that keep the
pipeline inside its own schema and landing volume. Anything else is refused, with the reason.

Deliberately NOT automatable: changing table schemas, granting permissions, editing or deleting
files, changing code. Those stay steps for a person.

The value RULES are for the job they were written for (loghub_etl). A pipeline connected later has no value
rules on file, so it gets the strict generic policy: every override must undo a change (reset to the job
default, or the value from the last successful run) and a last successful run must exist.
"""

import re

from pipeline_incident_copilot.schemas.agent_outputs import ProposedAction

OWN_SCHEMA = "workspace.pipeline_demo."
LANDING = "/Volumes/workspace/pipeline_demo/raw_logs/"
MAX_DEBUG_SAMPLE = 10_000

RULES = {
    "source": lambda v: v in {"spark", "hdfs"},
    "run_date": lambda v: bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", v)),
    "input_path": lambda v: v == "" or (v.startswith(LANDING) and ".." not in v),
    "timestamp_format": lambda v: v == "" or bool(re.fullmatch(r"[yMdHms/:\- .']{6,40}", v)),
    "silver_write_mode": lambda v: v in {"overwrite", "append"},
    "enrich_columns": lambda v: all(c.strip() in {"host_id", "region", "cluster"} for c in v.split(",") if c.strip()),
    "level_encoding": lambda v: v in {"text", "numeric"},
    "gold_table": lambda v: v == "" or (v.startswith(OWN_SCHEMA) and v.count(".") == 2),
    "debug_sample_rows": lambda v: v.isdigit() and int(v) <= MAX_DEBUG_SAMPLE,
}
DESCRIPTION = f"""Automated action available: rerun_job — rerun loghub_etl with parameter overrides.
Allowed parameters and values ("" resets a parameter to the job default):
- source: spark | hdfs;  run_date: YYYY-MM-DD
- input_path: "" or a file under {LANDING}
- timestamp_format: "" or a Spark datetime pattern
- silver_write_mode: overwrite (full refresh) | append (incremental)
- enrich_columns: "" or a comma list of host_id, region, cluster
- level_encoding: text | numeric
- gold_table: "" or a table in {OWN_SCHEMA}*
- debug_sample_rows: 0 to {MAX_DEBUG_SAMPLE}
Not automatable (write them as steps for a person): schema changes (ALTER TABLE), grants, file moves or
deletes, upstream deliveries, code changes. If the fix needs one of these first, propose no action."""


GENERIC_DESCRIPTION = """Automated action available: rerun_job — rerun the failed job with parameter overrides.
This job has no value rules on file, so every override must undo a change: "" (reset the parameter to the
job default) or exactly the value it had in the last successful run. Never invent a new value.
Not automatable (write them as steps for a person): data or file fixes, permissions, infrastructure, code
changes. If the fix needs one of these first, or no earlier run succeeded, propose no action."""
RULES_BY_JOB = {("databricks", "loghub_etl"): RULES}

# Which parameters can cause which kind of failure in loghub_etl. An automated reset must touch one of them:
# resetting an unrelated parameter that merely differs from the last good run (e.g. the silver write mode for null
# timestamps) would rerun into the same failure, so a person decides instead.
CAUSES = {
    "missing_input": {"input_path", "run_date", "source"},
    "data_quality": {"timestamp_format", "input_path"},
    "schema_change": {"enrich_columns", "silver_write_mode", "level_encoding"},
    "permissions": {"gold_table"},
    "resource_exhaustion": {"debug_sample_rows"},
}
CAUSES_BY_JOB = {("databricks", "loghub_etl"): CAUSES}


def causes_for(incident) -> dict | None:
    return causes_for_job(incident.run.platform, incident.run.job_name)


def causes_for_job(platform: str | None, job: str | None) -> dict | None:
    return CAUSES_BY_JOB.get((platform, job))


def rules_for(incident) -> dict | None:
    """The value rules for the incident's job; None = the generic undo-only policy."""
    return RULES_BY_JOB.get((incident.run.platform, incident.run.job_name))


def description_for(incident) -> str:
    return DESCRIPTION if rules_for(incident) is not None else GENERIC_DESCRIPTION


def check(
    action: ProposedAction | None,
    failed_parameters: dict[str, str] | None = None,
    last_good_parameters: dict[str, str] | None = None,
    rules: dict | None = RULES,
    failure_class: str | None = None,
    causes: dict | None = None,
) -> str | None:
    """None if allowed, else why not.

    - with `failed_parameters`: the rerun must change something (the same settings fail the same way)
    - with `last_good_parameters`: every changed value must be a reset ("") or the value that last
      worked — the copilot only undoes changes; a new value it came up with goes to a person
    """
    if action is None:
        return None
    if last_good_parameters is not None:
        for key, value in action.parameters.items():
            unchanged = failed_parameters is not None and failed_parameters.get(key, "") == str(value)
            if str(value) != "" and str(value) != last_good_parameters.get(key, "") and not unchanged:
                return f"{key}={value!r} is a new value, not a reset or the last working value: a person must decide"
    if failed_parameters is not None and all(
        failed_parameters.get(k, "") == str(v) for k, v in action.parameters.items()
    ):
        return "the rerun changes nothing compared with the failed run, so it would fail the same way"
    if action.name != "rerun_job":
        return f"unknown action {action.name}"
    if rules is None:  # generic policy: the undo-only check above is the whole rule set
        if last_good_parameters is None:
            return "no earlier successful run to undo back to, and no value rules for this job: a person must decide"
        return None
    for key, value in action.parameters.items():
        rule = rules.get(key)
        if rule is None:
            return f"parameter {key} cannot be changed"
        if not rule(str(value)):
            return f"value {value!r} not allowed for {key}"
    related = (causes or {}).get(failure_class or "")
    if related is not None and not related & set(action.parameters):
        changed = ", ".join(sorted(action.parameters))
        return (
            f"{changed} cannot cause a {failure_class} failure in this job, so a rerun would fail again: "
            "a person must decide"
        )
    return None
