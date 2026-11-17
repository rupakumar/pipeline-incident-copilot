"""ImprovementRepository: the self-improvement loop's record — every eval run and every decision, with reasons."""

import json
import sqlite3
from datetime import UTC, datetime


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ImprovementRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add_eval_run(self, label: str, split: str, config: str, summary: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO eval_runs (at, label, split, config, summary_json) VALUES (?, ?, ?, ?, ?)",
                (_now(), label, split, config, json.dumps(summary)),
            )

    def add_decision(
        self, kind: str, target: str, change: str, verdict: str, reason: str, deltas: dict, branch: str | None = None
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO improvement_decisions (at, kind, target, change, verdict, reason, deltas_json, branch)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_now(), kind, target, change, verdict, reason, json.dumps(deltas), branch),
            )
        return cur.lastrowid

    def decisions(self, since: str | None = None, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM improvement_decisions WHERE at >= ? ORDER BY id DESC LIMIT ?", (since or "", limit)
        ).fetchall()
        return [{**dict(r), "deltas": json.loads(r["deltas_json"])} for r in rows]

    def tried(self, change: str, days: int = 30) -> bool:
        """The same change was already decided recently (do not propose it again every night)."""
        since = datetime.fromtimestamp(datetime.now(UTC).timestamp() - days * 86400, UTC).isoformat()
        return (
            self.conn.execute(
                "SELECT 1 FROM improvement_decisions WHERE change = ? AND at >= ? AND verdict != 'skipped'",
                (change, since),
            ).fetchone()
            is not None
        )

    def latest_eval(self, label: str, split: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM eval_runs WHERE label = ? AND split = ? ORDER BY id DESC LIMIT 1", (label, split)
        ).fetchone()
        return {**dict(row), "summary": json.loads(row["summary_json"])} if row else None
