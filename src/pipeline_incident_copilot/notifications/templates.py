"""Subject and plain-text body for each incident event."""

from pipeline_incident_copilot.connectors.catalog import platform_label
from pipeline_incident_copilot.notifications.base import EventKind, IncidentEvent


def is_auto_fixed(event: IncidentEvent) -> bool:
    """Resolved by a known fix the auto-apply policy approved (the diagnoser starts the note with this)."""
    return event.kind is EventKind.RESOLVED and (event.note or "").startswith("Auto-fixed")


def _evidence_lines(event: IncidentEvent) -> list[str]:
    ev = event.evidence
    if ev is None:
        return []
    lines = ["", "Evidence collected:"]
    lines += [f"  - {r.tool}: {r.summary}" for r in ev.tool_results]
    lines += [f"  - not collected: {p}" for p in ev.problems]
    return lines


def _confidence_lines(event: IncidentEvent) -> list[str]:
    conf, rc = event.confidence or {}, event.root_cause
    score = conf.get("calibrated") or (rc.confidence if rc else None)
    if score is None or not (event.resolution and event.resolution.action):
        return []
    band = "high" if score >= 0.85 else "medium" if score >= 0.70 else "low"
    lines = ["", f"Confidence in this fix: {score:.0%} ({band}; propose >= 70%, auto-apply >= 85%)"]
    lines += [f"  [{'x' if ok else ' '}] {name.replace('_', ' ')}" for name, ok in (conf.get("signals") or {}).items()]
    auto = event.auto_approval or {}
    if auto.get("memory_id"):
        lines.append(f"  known fix: worked {auto.get('successes', 0)}x before; {auto.get('reason')}")
    return lines


def render(event: IncidentEvent) -> tuple[str, str]:
    inc = event.incident
    run = inc.run
    where = f"{run.failed_task} failed" if run.failed_task else run.result_state
    head = [
        f"Incident:   {inc.incident_id}  ({inc.status.value})",
        f"Pipeline:   {platform_label(run.platform)} · {run.job_name}  · source {inc.source or '?'}"
        f" · run_date {inc.run_date or '?'}",
        f"Failed:     {where}",
        f"Run:        {run.run_page_url or run.run_id}",
    ]
    if event.evidence and event.evidence.error:
        head.append(f"Error:      {event.evidence.error.strip().splitlines()[0][:300]}")

    if event.kind is EventKind.INCIDENT_OPENED:
        subject = f"[{inc.incident_id}] {run.job_name}: {where} — investigating"
        intro = ["A pipeline run failed. The copilot opened an incident and is investigating.", ""]
        outro = ["", "No action is needed from you yet. You will get the diagnosis and a proposed fix to approve."]
    else:
        what = "auto-fixed (known fix)" if is_auto_fixed(event) else event.kind.value.replace("_", " ")
        subject = f"[{inc.incident_id}] {run.job_name}: {what}"
        intro, outro = [event.note, ""] if event.note else [], []
    body = "\n".join(
        intro + head + _confidence_lines(event) + _evidence_lines(event) + outro + ["", "— Pipeline Incident Copilot"]
    )
    return subject, body
