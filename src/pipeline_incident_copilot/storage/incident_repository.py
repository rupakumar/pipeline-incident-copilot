"""IncidentRepository: the only code that reads or writes incidents, events and evidence."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from pipeline_incident_copilot.schemas.evidence import EvidenceBundle
from pipeline_incident_copilot.schemas.incident import FailedRun, Incident, IncidentStatus


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


PENDING_RERUN_MAX = timedelta(minutes=30)  # a crash between starting a rerun and recording it stops hiding failures
CLOCK_SKEW = timedelta(seconds=60)  # platform clocks (Databricks, Jenkins) vs this Mac


class IncidentRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # --- incidents -------------------------------------------------------------------------
    def exists_for_run(self, run: FailedRun) -> bool:
        return self.conn.execute("SELECT 1 FROM incidents WHERE run_key = ?", (run.run_key,)).fetchone() is not None

    def started_by_copilot(self, run: FailedRun) -> bool:
        """A rerun the copilot started as a fix: its outcome belongs to the original incident (the verifier
        re-diagnoses or escalates there), so the watcher must not open a second ticket for it. Covers the moment
        between starting the rerun and recording its id too (the pending marker set before the start)."""
        recorded = self.conn.execute(
            "SELECT 1 FROM incident_events e JOIN incidents i USING (incident_id)"
            " WHERE e.kind = 'status:remediating' AND e.detail LIKE ? AND i.connection_id = ? AND i.pipeline = ?",
            (f"rerun {run.run_id} started%", run.connection_id, run.job_name),
        ).fetchone()
        if recorded:
            return True
        pending = self.get_state(self._pending_key(run.connection_id, run.job_name))
        if not pending:
            return False
        since = datetime.fromisoformat(pending)
        if datetime.now(UTC) - since > PENDING_RERUN_MAX:
            return False
        return run.started_at is None or run.started_at >= since - CLOCK_SKEW

    @staticmethod
    def _pending_key(connection_id: str, pipeline: str) -> str:
        return f"rerun_pending:{connection_id}:{pipeline}"

    @contextmanager
    def rerun_pending(self, connection_id: str, pipeline: str) -> Iterator[None]:
        """Wrap 'start the fix rerun + record its id': until the id is recorded, failures of runs of this pipeline that
        started from now on are the copilot's own rerun, not new incidents."""
        key = self._pending_key(connection_id, pipeline)
        self.set_state(key, _now())
        try:
            yield
        finally:
            with self.conn:
                self.conn.execute("DELETE FROM watcher_state WHERE key = ?", (key,))

    def open(self, run: FailedRun) -> Incident:
        """Save a new incident for a failed run (before any other work, so nothing is lost)."""
        incident = self.open_if_new(run)
        if incident is None:
            raise ValueError(f"run {run.run_key} already has an incident")
        return incident

    def open_if_new(self, run: FailedRun) -> Incident | None:
        """Like open(), but None when the run already has an incident (another watcher got there first). The write
        lock is taken before reading the next number, so two processes never pick the same incident id."""
        if self.conn.in_transaction:
            self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if self.exists_for_run(run):
                self.conn.rollback()
                return None
            seq = self.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM incidents").fetchone()[0]
            incident = Incident(incident_id=f"INC-{seq:04d}", run=run)
            self.conn.execute(
                "INSERT INTO incidents (seq, incident_id, connection_id, platform, pipeline, run_key, run_id, job_id,"
                " status, failed_task, source, run_date, opened_at, updated_at, incident_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    seq,
                    incident.incident_id,
                    run.connection_id,
                    run.platform,
                    run.job_name,
                    run.run_key,
                    str(run.run_id),
                    str(run.job_id),
                    incident.status.value,
                    run.failed_task,
                    incident.source,
                    incident.run_date,
                    incident.opened_at.isoformat(timespec="seconds"),
                    incident.updated_at.isoformat(timespec="seconds"),
                    incident.model_dump_json(),
                ),
            )
            self._event(incident.incident_id, "opened", f"run {run.run_id} failed in {run.failed_task}")
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return incident

    def get(self, incident_id: str) -> Incident | None:
        row = self.conn.execute("SELECT incident_json FROM incidents WHERE incident_id = ?", (incident_id,)).fetchone()
        return Incident.model_validate_json(row["incident_json"]) if row else None

    def recent(self, status: IncidentStatus | None = None, limit: int = 50) -> list[Incident]:
        sql = "SELECT incident_json FROM incidents"
        params: tuple = ()
        if status:
            sql, params = sql + " WHERE status = ?", (status.value,)
        rows = self.conn.execute(sql + " ORDER BY seq DESC LIMIT ?", (*params, limit)).fetchall()
        return [Incident.model_validate_json(r["incident_json"]) for r in rows]

    def undiagnosed(self, since: datetime) -> list[Incident]:
        """Incidents still open (evidence collected, never diagnosed) since `since`: opened by another process, or
        left behind by a crash. Older ones are left for a person, so a restart never acts on stale tickets."""
        rows = self.conn.execute(
            "SELECT incident_json FROM incidents i WHERE status = ? AND opened_at >= ?"
            " AND EXISTS (SELECT 1 FROM evidence e WHERE e.incident_id = i.incident_id)"
            " AND NOT EXISTS (SELECT 1 FROM diagnoses d WHERE d.incident_id = i.incident_id) ORDER BY seq",
            (IncidentStatus.OPEN.value, since.isoformat(timespec="seconds")),
        ).fetchall()
        return [Incident.model_validate_json(r["incident_json"]) for r in rows]

    def waiting_with_known_fix(self, since: datetime) -> list[Incident]:
        """Tickets paused for approval (by the graph, not a Claude Code request) whose fix matched a known fix but was
        held back by a rule that can change while they wait (cooldown, minimum successes, the connection switch)."""
        rows = self.conn.execute(
            "SELECT incident_json FROM incidents i WHERE status = ? AND opened_at >= ?"
            " AND EXISTS (SELECT 1 FROM incident_events e WHERE e.incident_id = i.incident_id"
            "             AND e.kind = 'auto_approval:no' AND e.detail LIKE '%matches known fix%')"
            " AND NOT EXISTS (SELECT 1 FROM fix_requests f"
            "                 WHERE f.incident_id = i.incident_id AND f.status = 'pending')"
            " ORDER BY seq",
            (IncidentStatus.AWAITING_APPROVAL.value, since.isoformat(timespec="seconds")),
        ).fetchall()
        return [Incident.model_validate_json(r["incident_json"]) for r in rows]

    def stalled(self, since: datetime, quiet_for: timedelta) -> list[Incident]:
        """Incidents left 'investigating' with no event for `quiet_for` (the process diagnosing them died), opened
        since `since`. The caller re-checks the status under the diagnosis lock before taking one over."""
        quiet = (datetime.now(UTC) - quiet_for).isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT incident_json FROM incidents i WHERE status = ? AND opened_at >= ?"
            " AND (SELECT max(at) FROM incident_events e WHERE e.incident_id = i.incident_id) < ? ORDER BY seq",
            (IncidentStatus.INVESTIGATING.value, since.isoformat(timespec="seconds"), quiet),
        ).fetchall()
        return [Incident.model_validate_json(r["incident_json"]) for r in rows]

    def set_status(self, incident_id: str, status: IncidentStatus, detail: str = "") -> Incident:
        incident = self.get(incident_id)
        if incident is None:
            raise KeyError(incident_id)
        incident = incident.model_copy(update={"status": status, "updated_at": datetime.now(UTC)})
        with self.conn:
            self.conn.execute(
                "UPDATE incidents SET status = ?, updated_at = ?, incident_json = ? WHERE incident_id = ?",
                (
                    status.value,
                    incident.updated_at.isoformat(timespec="seconds"),
                    incident.model_dump_json(),
                    incident_id,
                ),
            )
            self._event(incident_id, f"status:{status.value}", detail)
        return incident

    # --- evidence and events ----------------------------------------------------------------
    def save_evidence(self, bundle: EvidenceBundle) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO evidence (incident_id, collected_at, bundle_json) VALUES (?, ?, ?)",
                (bundle.incident_id, bundle.collected_at.isoformat(timespec="seconds"), bundle.model_dump_json()),
            )
            self._event(bundle.incident_id, "evidence", f"{len(bundle.tool_results)} tool checks")

    def get_evidence(self, incident_id: str) -> EvidenceBundle | None:
        row = self.conn.execute("SELECT bundle_json FROM evidence WHERE incident_id = ?", (incident_id,)).fetchone()
        return EvidenceBundle.model_validate_json(row["bundle_json"]) if row else None

    def events(self, incident_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT at, kind, detail FROM incident_events WHERE incident_id = ? ORDER BY id", (incident_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def add_event(self, incident_id: str, kind: str, detail: str = "") -> None:
        with self.conn:
            self._event(incident_id, kind, detail)

    def _event(self, incident_id: str, kind: str, detail: str) -> None:
        self.conn.execute(
            "INSERT INTO incident_events (incident_id, at, kind, detail) VALUES (?, ?, ?, ?)",
            (incident_id, _now(), kind, detail),
        )

    # --- diagnoses (one per graph run; the latest is current) ------------------------------
    def diagnosis_count(self, incident_id: str) -> int:
        return self.conn.execute("SELECT count(*) FROM diagnoses WHERE incident_id = ?", (incident_id,)).fetchone()[0]

    def save_diagnosis(self, incident_id: str, thread_id: str, diagnosis: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO diagnoses (incident_id, thread_id, created_at, diagnosis_json) VALUES (?, ?, ?, ?)",
                (incident_id, thread_id, _now(), json.dumps(diagnosis, default=str)),
            )

    def latest_thread(self, incident_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT thread_id FROM diagnoses WHERE incident_id = ? ORDER BY id DESC LIMIT 1", (incident_id,)
        ).fetchone()
        return row["thread_id"] if row else None

    def latest_diagnosis(self, incident_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT diagnosis_json FROM diagnoses WHERE incident_id = ? ORDER BY id DESC LIMIT 1", (incident_id,)
        ).fetchone()
        return json.loads(row["diagnosis_json"]) if row else None

    # --- watcher bookkeeping ----------------------------------------------------------------
    def claim_decision(self, incident_id: str, by: str, decision: str) -> str | None:
        """One decision per approval pause, across processes (console, watcher, CLI, Claude Code): the first claim
        wins atomically. Returns None when `by` holds the claim (a retry by the same decider passes too), else who
        decided first, e.g. "approve by anna@x.io (email link) at 10:42 UTC". A later pause (a failed rerun was
        diagnosed again) is a new claim."""
        pause = self.conn.execute(
            "SELECT max(id) FROM incident_events WHERE incident_id = ? AND kind = ?",
            (incident_id, f"status:{IncidentStatus.AWAITING_APPROVAL.value}"),
        ).fetchone()[0]
        key = f"decision:{incident_id}:{pause or 0}"
        mine = f"{decision} by {by} at {datetime.now(UTC):%H:%M} UTC"
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO watcher_state (key, value) VALUES (?, ?)", (key, mine))
        holder = self.get_state(key) or ""
        return None if holder.split(" by ", 1)[-1].rsplit(" at ", 1)[0] == by else holder

    def get_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM watcher_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO watcher_state (key, value) VALUES (?, ?)", (key, value))
