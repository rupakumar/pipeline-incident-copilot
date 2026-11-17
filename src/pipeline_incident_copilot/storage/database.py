"""SQLite connection and schema. One local file; swapping to Postgres touches only storage/.

The schema version is kept in `PRAGMA user_version`; `migrate()` upgrades an older file in place after
copying it to data/runtime/backups/.
"""

import ast
import json
import os
import re
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pipeline_incident_copilot.config import RUNTIME_DIR

DB_PATH = Path(os.getenv("COPILOT_DB", str(RUNTIME_DIR / "copilot.db")))  # evals point this at a copy

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL UNIQUE,
    connection_id TEXT NOT NULL,           -- which saved connection found the failure
    platform TEXT NOT NULL,                -- databricks / jenkins / ...
    pipeline TEXT,                         -- job name (the "application")
    run_key TEXT NOT NULL UNIQUE,          -- connection:job:run, unique across platforms
    run_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    status TEXT NOT NULL,
    failed_task TEXT,
    source TEXT,
    run_date TEXT,
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    incident_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incident_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL REFERENCES incidents (incident_id),
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS evidence (
    incident_id TEXT PRIMARY KEY REFERENCES incidents (incident_id),
    collected_at TEXT NOT NULL,
    bundle_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diagnoses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL REFERENCES incidents (incident_id),
    thread_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    diagnosis_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL UNIQUE,          -- error code | failed task | changed parameters: one memory per pattern
    failure_class TEXT,
    error_code TEXT,
    failed_task TEXT,
    incident_ids TEXT NOT NULL,            -- JSON list, oldest first
    text TEXT NOT NULL,                    -- what retrieval searches and agents read
    seen_count INTEGER NOT NULL DEFAULT 1,
    success_count INTEGER NOT NULL DEFAULT 0,  -- cited in a diagnosis whose fix then worked
    failure_count INTEGER NOT NULL DEFAULT 0,  -- cited in a diagnosis whose fix then failed
    pipeline_key TEXT,                     -- connection:job the fix belongs to
    fix_json TEXT,                         -- the rerun parameters that fixed it ({} = none recorded)
    fix_successes INTEGER NOT NULL DEFAULT 0,  -- verified incidents this exact fix resolved
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watcher_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connections (
    connection_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    platform TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    auto_apply INTEGER NOT NULL DEFAULT 0,              -- known fixes may run without approval
    auto_apply_min_successes INTEGER NOT NULL DEFAULT 2,
    auto_apply_cooldown_hours INTEGER NOT NULL DEFAULT 1,  -- at most one auto-fix per pipeline per N hours (0 = off)
    settings_json TEXT NOT NULL,           -- non-secret settings only; secrets are in the macOS Keychain
    secret_fields_json TEXT NOT NULL,
    pipelines_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_check_at TEXT,
    last_check_ok INTEGER,
    last_check_message TEXT,
    approvers_json TEXT NOT NULL DEFAULT '[]',  -- emails that get the Approve / Deny email (own signed link each)
    notify_json TEXT NOT NULL DEFAULT '[]'      -- emails that get FYI updates (opened, resolved, escalated)
);

-- company documents uploaded in the console and indexed into the knowledge base (files in data/runtime)
CREATE TABLE IF NOT EXISTS knowledge_documents (
    doc_id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    title TEXT NOT NULL,
    doc_type TEXT NOT NULL,                -- architecture / runbook / product / postmortem
    failure_class TEXT,                    -- runbooks: the failure class they are for (optional)
    sha256 TEXT NOT NULL UNIQUE,
    bytes INTEGER NOT NULL,
    chars INTEGER NOT NULL,
    sections INTEGER NOT NULL,
    chunks INTEGER NOT NULL,               -- in kb_structure (what the diagnosis searches)
    uploaded_by TEXT,
    uploaded_at TEXT NOT NULL
);

-- settings entered in the console (email delivery, Grafana): non-secret values here, secrets in the Keychain.
-- Each key is the environment variable it replaces (.env is only the fallback until the form has a value).
CREATE TABLE IF NOT EXISTS app_settings (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- a fix proposed outside the graph (the Claude Code skill), waiting for a person
CREATE TABLE IF NOT EXISTS fix_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL REFERENCES incidents (incident_id),
    source TEXT NOT NULL,                  -- claude_skill
    parameters_json TEXT NOT NULL,         -- the rerun resets, already guardrail-checked
    report_json TEXT NOT NULL,             -- the skill's report (root cause, resolution, citations)
    status TEXT NOT NULL,                  -- pending / approved / rejected / superseded
    requested_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT
);

-- the self-improvement loop (Task 2): every eval it ran and every change it decided on
CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    label TEXT NOT NULL,                   -- baseline / candidate
    split TEXT NOT NULL,                   -- train (tuning) / test (holdout)
    config TEXT NOT NULL,                  -- what was evaluated (tunables hash or the change)
    summary_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS improvement_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,                    -- tunable / prompt
    target TEXT NOT NULL,                  -- the knob or prompt file changed
    change TEXT NOT NULL,                  -- e.g. knowledge_cite: 4 → 5
    verdict TEXT NOT NULL,                 -- accepted / rejected / skipped
    reason TEXT NOT NULL,
    deltas_json TEXT NOT NULL,             -- metric changes (candidate − baseline)
    branch TEXT                            -- the branch a person can merge (accepted only)
);

CREATE TABLE IF NOT EXISTS pipeline_health (
    connection_id TEXT NOT NULL REFERENCES connections (connection_id) ON DELETE CASCADE,
    pipeline TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    last_run_id TEXT,
    last_state TEXT,
    last_run_at TEXT,
    last_run_url TEXT,
    runs_24h INTEGER NOT NULL DEFAULT 0,
    failures_24h INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (connection_id, pipeline)
);
"""

# created after migrate(): an old incidents table has no connection_id column yet
INDEXES = """
CREATE INDEX IF NOT EXISTS incidents_status ON incidents (status);
CREATE INDEX IF NOT EXISTS incidents_connection ON incidents (connection_id, pipeline);
"""

SCHEMA_VERSION = 5
BACKUP_DIR = RUNTIME_DIR / "backups"

# v0 → v1: incidents get connection_id / platform / pipeline / run_key; run_id may be text (Glue, ADF ids) and
# is no longer unique on its own (Jenkins build numbers repeat per job). Existing rows came from Databricks.
MIGRATE_V1 = """
CREATE TABLE incidents_v1 (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL UNIQUE,
    connection_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    pipeline TEXT,
    run_key TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    status TEXT NOT NULL,
    failed_task TEXT,
    source TEXT,
    run_date TEXT,
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    incident_json TEXT NOT NULL
);
INSERT INTO incidents_v1 (seq, incident_id, connection_id, platform, pipeline, run_key, run_id, job_id, status,
                          failed_task, source, run_date, opened_at, updated_at, incident_json)
SELECT seq, incident_id, 'databricks-loghub', 'databricks', json_extract(incident_json, '$.run.job_name'),
       'databricks-loghub:' || job_id || ':' || run_id, run_id, job_id, status, failed_task, source, run_date,
       opened_at, updated_at, incident_json
FROM incidents;
DROP TABLE incidents;
ALTER TABLE incidents_v1 RENAME TO incidents;
INSERT OR IGNORE INTO watcher_state (key, value)
SELECT replace(key, 'loghub_etl', 'databricks-loghub'), value FROM watcher_state
WHERE key IN ('watch_from:loghub_etl', 'last_poll:loghub_etl');
"""


# v1 → v2: memories remember their fix and pipeline (auto-apply needs both); connections get the auto-apply switch
MIGRATE_V2_COLUMNS = {
    "memories": [
        ("pipeline_key", "TEXT"),
        ("fix_json", "TEXT"),
        ("fix_successes", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "connections": [
        ("auto_apply", "INTEGER NOT NULL DEFAULT 0"),
        ("auto_apply_min_successes", "INTEGER NOT NULL DEFAULT 2"),
    ],
}
FIX_IN_TEXT = re.compile(r"Fix that worked: rerun_job (\{.*?\})")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _backup(path: Path, version: int) -> None:
    if path.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = BACKUP_DIR / f"{path.stem}-v{version}-{datetime.now(UTC):%Y%m%dT%H%M%S}.db"
        shutil.copy2(path, backup)
        print(f"[storage] upgrading {path.name} to schema v{SCHEMA_VERSION} (backup: {backup})")


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Add the columns, then backfill old memories: fix from their text, pipeline from their first incident,
    and one verified success per incident they were learned from."""
    for table, columns in MIGRATE_V2_COLUMNS.items():
        have = _columns(conn, table)
        for name, ddl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    for row in conn.execute(
        "SELECT id, text, incident_ids, seen_count FROM memories WHERE fix_json IS NULL"
    ).fetchall():
        m = FIX_IN_TEXT.search(row["text"])
        fix = ast.literal_eval(m.group(1)) if m else {}
        first = json.loads(row["incident_ids"])[0]
        key = conn.execute(
            "SELECT connection_id || ':' || pipeline FROM incidents WHERE incident_id = ?", (first,)
        ).fetchone()
        conn.execute(
            "UPDATE memories SET fix_json = ?, pipeline_key = ?, fix_successes = ? WHERE id = ?",
            (json.dumps(fix, sort_keys=True), key[0] if key else None, row["seen_count"] if fix else 0, row["id"]),
        )
    conn.commit()


CALIBRATED_IN_TRAIL = re.compile(r"model ([0-9.]+), evidence-calibrated ([0-9.]+)")


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """Diagnoses saved before v3 lack the calibrated score: recover it from their trail line
    ("root cause (model 0.95, evidence-calibrated 0.83): ..."). The four signals were not recorded, so stay unknown."""
    for row in conn.execute("SELECT id, diagnosis_json FROM diagnoses").fetchall():
        d = json.loads(row["diagnosis_json"])
        if d.get("confidence"):
            continue
        found = [m for line in d.get("trail") or [] if (m := CALIBRATED_IN_TRAIL.search(line))]
        if found:
            d["confidence"] = {
                "model": float(found[-1].group(1)),
                "calibrated": float(found[-1].group(2)),
                "signals": None,
            }
        elif d.get("source") == "claude_skill" and (d.get("root_cause") or {}).get("confidence") is not None:
            c = d["root_cause"]["confidence"]
            d["confidence"] = {"model": c, "calibrated": c, "signals": None}
        else:
            continue
        conn.execute("UPDATE diagnoses SET diagnosis_json = ? WHERE id = ?", (json.dumps(d), row["id"]))
    conn.commit()


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """Connections get their own approvers and notify lists (empty = the default recipients)."""
    have = _columns(conn, "connections")
    for name in ("approvers_json", "notify_json"):
        if name not in have:
            conn.execute(f"ALTER TABLE connections ADD COLUMN {name} TEXT NOT NULL DEFAULT '[]'")
    conn.commit()


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """The auto-fix cooldown becomes a per-connection setting (was a fixed 6 h)."""
    if "auto_apply_cooldown_hours" not in _columns(conn, "connections"):
        conn.execute("ALTER TABLE connections ADD COLUMN auto_apply_cooldown_hours INTEGER NOT NULL DEFAULT 1")
    conn.commit()


def migrate(conn: sqlite3.Connection, path: Path) -> None:
    """Upgrade an older store in place (backup first). New files are created at the current version."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    conn.commit()
    has_data = conn.execute("SELECT count(*) FROM incidents").fetchone()[0] > 0
    if has_data:
        _backup(path, version)
    if "run_key" not in _columns(conn, "incidents"):
        # SQLite's table-rebuild procedure: other tables keep their foreign keys to incidents (incident_id)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(f"BEGIN; {MIGRATE_V1} COMMIT;")
        if problems := conn.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError(f"migration left broken references: {problems[:3]}")
        conn.execute("PRAGMA foreign_keys = ON")
    _migrate_v2(conn)
    _migrate_v3(conn)
    _migrate_v4(conn)
    _migrate_v5(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    migrate(conn, path)
    conn.executescript(INDEXES)
    return conn
