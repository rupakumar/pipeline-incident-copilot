"""MemoryRepository: the SQLite record of what the copilot has learned (one row per failure pattern)."""

import json
import re
import sqlite3
from datetime import UTC, datetime


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class MemoryRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def by_pattern(self, pattern: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM memories WHERE pattern = ?", (pattern,)).fetchone()
        return self._row(row)

    def get(self, memory_id: int) -> dict | None:
        return self._row(self.conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone())

    def all(self) -> list[dict]:
        return [self._row(r) for r in self.conn.execute("SELECT * FROM memories ORDER BY updated_at DESC").fetchall()]

    def insert(
        self,
        pattern: str,
        incident_id: str,
        text: str,
        failure_class: str,
        error_code,
        failed_task,
        pipeline_key: str | None = None,
        fix: dict[str, str] | None = None,
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO memories (pattern, failure_class, error_code, failed_task, incident_ids, text,"
                " pipeline_key, fix_json, fix_successes, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pattern,
                    failure_class,
                    error_code,
                    failed_task,
                    json.dumps([incident_id]),
                    text,
                    pipeline_key,
                    json.dumps(fix or {}, sort_keys=True),
                    1 if fix else 0,
                    _now(),
                    _now(),
                ),
            )
        return cur.lastrowid

    def add_occurrence(self, memory_id: int, incident_id: str, text: str, fix: dict[str, str] | None = None) -> None:
        """Another verified incident with this pattern. The same fix again counts one more success; a different
        fix replaces the stored one and its count starts again (only an exact fix earns a track record)."""
        m = self.get(memory_id)
        new = incident_id not in m["incident_ids"]
        ids = [*m["incident_ids"], incident_id] if new else m["incident_ids"]
        fix = fix or {}
        same = fix == m["fix"]
        successes = (m["fix_successes"] + 1 if new else m["fix_successes"]) if same else (1 if fix else 0)
        with self.conn:
            self.conn.execute(
                "UPDATE memories SET incident_ids = ?, text = ?, seen_count = ?, fix_json = ?, fix_successes = ?,"
                " updated_at = ? WHERE id = ?",
                (json.dumps(ids), text, len(ids), json.dumps(fix, sort_keys=True), successes, _now(), memory_id),
            )

    def merge(self, keep_id: int, other_ids: list[int], pattern: str) -> None:
        """Fold other memories (same fix) into keep_id: incidents, fix record and track record add up; keep_id takes
        the pattern. One transaction."""
        keep = self.get(keep_id)
        others = [self.get(i) for i in other_ids]
        ids = list(dict.fromkeys([*keep["incident_ids"], *(i for o in others for i in o["incident_ids"])]))
        text = re.sub(r"Seen \d+ time\(s\)\.", f"Seen {len(ids)} time(s).", keep["text"])
        total = {c: keep[c] + sum(o[c] for o in others) for c in ("fix_successes", "success_count", "failure_count")}
        with self.conn:
            self.conn.executemany("DELETE FROM memories WHERE id = ?", [(i,) for i in other_ids])
            self.conn.execute(
                "UPDATE memories SET pattern = ?, incident_ids = ?, seen_count = ?, text = ?, fix_successes = ?,"
                " success_count = ?, failure_count = ?, updated_at = ? WHERE id = ?",
                (pattern, json.dumps(ids), len(ids), text, total["fix_successes"], total["success_count"],
                 total["failure_count"], _now(), keep_id),
            )  # fmt: skip

    def record_outcome(self, memory_id: int, worked: bool) -> None:
        column = "success_count" if worked else "failure_count"
        with self.conn:
            self.conn.execute(
                f"UPDATE memories SET {column} = {column} + 1, updated_at = ? WHERE id = ?", (_now(), memory_id)
            )

    @staticmethod
    def _row(row) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        d["incident_ids"] = json.loads(d["incident_ids"])
        d["fix"] = json.loads(d.get("fix_json") or "{}")
        return d
