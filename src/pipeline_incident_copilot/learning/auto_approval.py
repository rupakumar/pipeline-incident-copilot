"""AutoApprovalPolicy: may this proposed fix run without a person? Only a known fix with a clean track record.

All must hold (agreed 2026-10-24):
- auto-apply is switched on for the incident's connection (connect form; off by default) and not off globally
  (COPILOT_AUTO_APPLY=off)
- this is the first fix attempt (after a failed rerun a person decides)
- a memory exists for the same failure pattern on the same pipeline, and its fix never failed
- the proposed fix is exactly that memory's fix, which has resolved at least `auto_apply_min_successes` incidents
- the evidence-calibrated confidence is at least AUTO_CONFIDENCE
- no other auto-fix on the same pipeline within the connection's cooldown (connect form; default 1 h, 0 = none)
The guardrail still runs in ActionExecutor. Every answer carries a one-line reason for the history and the email.
"""

import os
from datetime import UTC, datetime, timedelta

from pipeline_incident_copilot.learning.incident_memory import fix_of, pattern_of, pipeline_key_of
from pipeline_incident_copilot.schemas.remediation import AutoApproval
from pipeline_incident_copilot.schemas.triage_state import TriageState
from pipeline_incident_copilot.storage.connection_repository import ConnectionRepository
from pipeline_incident_copilot.storage.incident_repository import IncidentRepository
from pipeline_incident_copilot.storage.memory_repository import MemoryRepository

AUTO_CONFIDENCE = 0.85
EVENT = "auto_approved"  # incident_events kind written when the policy approves


class AutoApprovalPolicy:
    def __init__(
        self,
        memories: MemoryRepository,
        connections: ConnectionRepository,
        incidents: IncidentRepository,
        enabled: bool | None = None,
    ):
        self.memories = memories
        self.connections = connections
        self.incidents = incidents
        self.enabled = os.getenv("COPILOT_AUTO_APPLY", "on") != "off" if enabled is None else enabled

    def decide(self, state: TriageState) -> AutoApproval:
        memory = self.memories.by_pattern(pattern_of(state))
        if memory and memory.get("pipeline_key") != pipeline_key_of(state):
            memory = None  # same symptom on another pipeline is not a known fix here
        known = {
            "memory_id": memory["id"] if memory else None,
            "from_incidents": memory["incident_ids"] if memory else [],
            "successes": memory["fix_successes"] if memory else 0,
            "failures": memory["failure_count"] if memory else 0,
        }

        def no(reason: str) -> AutoApproval:
            return AutoApproval(approved=False, reason=reason, **known)

        run = state["incident"].run
        connection = self.connections.get(run.connection_id)
        if memory is None:
            return no("no verified fix on record for this failure on this pipeline")
        fix = fix_of(state)
        if fix != memory["fix"]:
            return no(f"the proposed fix {fix} differs from the known fix {memory['fix']}")
        if memory["failure_count"] > 0:
            return no(f"the known fix failed {memory['failure_count']} time(s)")
        if not self.enabled:
            return no("auto-apply is switched off for this copilot (COPILOT_AUTO_APPLY=off)")
        if connection is None or not connection.auto_apply:
            return no(f"auto-apply is off for the connection {run.connection_id}")
        needed = connection.auto_apply_min_successes
        if memory["fix_successes"] < needed:
            return no(f"the known fix worked {memory['fix_successes']} of the {needed} times needed")
        if state.get("fix_attempts", 0) > 0:
            return no("an earlier rerun of this incident failed: a person decides")
        confidence = state.get("confidence", {}).get("calibrated", state["root_cause"].confidence)
        if confidence < AUTO_CONFIDENCE:
            return no(f"confidence {confidence:.2f} is below {AUTO_CONFIDENCE}")
        hours = connection.auto_apply_cooldown_hours
        if hours and (last := self._last_auto_fix(run.connection_id, run.job_name, hours)):
            return no(f"another auto-fix ran on this pipeline at {last:%Y-%m-%d %H:%M} UTC (at most 1 per {hours} h)")
        approved = AutoApproval(approved=True, reason="", **known)
        return approved.model_copy(update={"reason": f"approved automatically: {approved.known_fix}"})

    def _last_auto_fix(self, connection_id: str, pipeline: str, hours: int) -> datetime | None:
        since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")
        row = self.incidents.conn.execute(
            "SELECT max(e.at) FROM incident_events e JOIN incidents i USING (incident_id)"
            " WHERE e.kind = ? AND i.connection_id = ? AND i.pipeline = ? AND e.at >= ?",
            (EVENT, connection_id, pipeline, since),
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None
