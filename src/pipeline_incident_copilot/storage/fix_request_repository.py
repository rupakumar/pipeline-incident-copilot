"""FixRequestRepository: fixes proposed outside the agent graph (the Claude Code skill) that wait for a person.

The skill can only ask; a person decides through the same channels as graph proposals (console, Grafana, email,
`copilot approve`), and the console then runs the request. At most one request per incident is pending.
"""

import json
import sqlite3
from datetime import UTC, datetime


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class FixRequestRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(self, incident_id: str, source: str, parameters: dict[str, str], report: dict) -> int:
        with self.conn:
            self.conn.execute(
                "UPDATE fix_requests SET status = 'superseded', decided_at = ?"
                " WHERE incident_id = ? AND status = 'pending'",
                (_now(), incident_id),
            )
            cur = self.conn.execute(
                "INSERT INTO fix_requests (incident_id, source, parameters_json, report_json, status, requested_at)"
                " VALUES (?, ?, ?, ?, 'pending', ?)",
                (incident_id, source, json.dumps(parameters), json.dumps(report), _now()),
            )
        return cur.lastrowid

    def pending(self, incident_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM fix_requests WHERE incident_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
            (incident_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["parameters"] = json.loads(d.pop("parameters_json"))
        d["report"] = json.loads(d.pop("report_json"))
        return d

    def close(self, request_id: int, status: str, by: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE fix_requests SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
                (status, _now(), by, request_id),
            )
