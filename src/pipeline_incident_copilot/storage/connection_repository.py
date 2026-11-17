"""ConnectionRepository: saved pipeline connections and the health of their pipelines (no secrets here)."""

import json
import sqlite3
from datetime import UTC, datetime

from pipeline_incident_copilot.schemas.connection import Connection, PipelineHealth


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


class ConnectionRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # --- connections ---------------------------------------------------------------------------
    def all(self) -> list[Connection]:
        rows = self.conn.execute("SELECT * FROM connections ORDER BY created_at, connection_id").fetchall()
        return [self._from_row(r) for r in rows]

    def enabled(self) -> list[Connection]:
        return [c for c in self.all() if c.enabled]

    def get(self, connection_id: str) -> Connection | None:
        row = self.conn.execute("SELECT * FROM connections WHERE connection_id = ?", (connection_id,)).fetchone()
        return self._from_row(row) if row else None

    def save(self, c: Connection) -> Connection:
        """Insert or update (keeps created_at and the last check of an existing connection)."""
        c = c.model_copy(update={"updated_at": datetime.now(UTC)})
        with self.conn:
            self.conn.execute(
                "INSERT INTO connections (connection_id, name, platform, enabled, auto_apply, auto_apply_min_successes,"
                " auto_apply_cooldown_hours,"
                " settings_json, secret_fields_json, pipelines_json, approvers_json, notify_json,"
                " created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (connection_id) DO UPDATE SET name = excluded.name, platform = excluded.platform,"
                " enabled = excluded.enabled, auto_apply = excluded.auto_apply,"
                " auto_apply_min_successes = excluded.auto_apply_min_successes,"
                " auto_apply_cooldown_hours = excluded.auto_apply_cooldown_hours,"
                " settings_json = excluded.settings_json,"
                " secret_fields_json = excluded.secret_fields_json, pipelines_json = excluded.pipelines_json,"
                " approvers_json = excluded.approvers_json, notify_json = excluded.notify_json,"
                " updated_at = excluded.updated_at",
                (
                    c.connection_id,
                    c.name,
                    c.platform,
                    int(c.enabled),
                    int(c.auto_apply),
                    c.auto_apply_min_successes,
                    c.auto_apply_cooldown_hours,
                    json.dumps(c.settings),
                    json.dumps(c.secret_fields),
                    json.dumps(c.pipelines),
                    json.dumps(c.approvers),
                    json.dumps(c.notify),
                    _iso(c.created_at),
                    _iso(c.updated_at),
                ),
            )
            # pipelines no longer watched drop off the dashboard
            marks = ",".join("?" * len(c.pipelines))
            self.conn.execute(
                f"DELETE FROM pipeline_health WHERE connection_id = ? AND pipeline NOT IN ({marks})",
                (c.connection_id, *c.pipelines),
            )
        return self.get(c.connection_id)

    def set_enabled(self, connection_id: str, enabled: bool) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE connections SET enabled = ?, updated_at = ? WHERE connection_id = ?",
                (int(enabled), _iso(datetime.now(UTC)), connection_id),
            )

    def delete(self, connection_id: str) -> None:
        """Forget the connection and its health rows; its incidents stay (they are the audit trail)."""
        with self.conn:
            self.conn.execute("DELETE FROM pipeline_health WHERE connection_id = ?", (connection_id,))
            self.conn.execute("DELETE FROM connections WHERE connection_id = ?", (connection_id,))

    def record_check(self, connection_id: str, ok: bool, message: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE connections SET last_check_at = ?, last_check_ok = ?, last_check_message = ?"
                " WHERE connection_id = ?",
                (_iso(datetime.now(UTC)), int(ok), message[:500], connection_id),
            )

    def unique_id(self, base: str) -> str:
        """`base`, or `base-2`, `base-3`, ... if taken."""
        candidate, n = base, 1
        while self.get(candidate) is not None:
            n += 1
            candidate = f"{base}-{n}"
        return candidate

    # --- pipeline health -------------------------------------------------------------------------
    def save_health(self, rows: list[PipelineHealth]) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO pipeline_health (connection_id, pipeline, checked_at, last_run_id, last_state,"
                " last_run_at, last_run_url, runs_24h, failures_24h) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        h.connection_id,
                        h.pipeline,
                        _iso(h.checked_at),
                        h.last_run_id,
                        h.last_state,
                        _iso(h.last_run_at),
                        h.last_run_url,
                        h.runs_24h,
                        h.failures_24h,
                    )
                    for h in rows
                ],
            )

    def health(self, connection_id: str) -> list[PipelineHealth]:
        rows = self.conn.execute(
            "SELECT * FROM pipeline_health WHERE connection_id = ? ORDER BY pipeline", (connection_id,)
        ).fetchall()
        return [PipelineHealth.model_validate(dict(r)) for r in rows]

    @staticmethod
    def _from_row(r: sqlite3.Row) -> Connection:
        return Connection(
            connection_id=r["connection_id"],
            name=r["name"],
            platform=r["platform"],
            enabled=bool(r["enabled"]),
            auto_apply=bool(r["auto_apply"]),
            auto_apply_min_successes=r["auto_apply_min_successes"],
            auto_apply_cooldown_hours=r["auto_apply_cooldown_hours"],
            settings=json.loads(r["settings_json"]),
            secret_fields=json.loads(r["secret_fields_json"]),
            pipelines=json.loads(r["pipelines_json"]),
            approvers=json.loads(r["approvers_json"] or "[]"),
            notify=json.loads(r["notify_json"] or "[]"),
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            last_check_at=r["last_check_at"],
            last_check_ok=None if r["last_check_ok"] is None else bool(r["last_check_ok"]),
            last_check_message=r["last_check_message"] or "",
        )
