"""Idempotently provision what the loghub_etl job needs; the job itself is deployed by the bundle.

Creates the schema and landing volume, the audit tables (sql/audit_tables.sql, run on a SQL
warehouse), and uploads one log drop per source and date to raw_logs/<source>/<YYYY-MM-DD>/.
Real days come from the LogHub samples split by their own timestamps; one corrupted
delivery per source mimics a broken upstream export.

Usage: python databricks/provision.py
"""

import io
import random
import re
import time

from databricks.sdk.errors import NotFound
from databricks.sdk.service.catalog import VolumeType
from databricks.sdk.service.sql import StatementState

from pipeline_incident_copilot.config import DATABRICKS_DIR, EXTERNAL_DATA_DIR
from pipeline_incident_copilot.integrations.databricks_client import CATALOG, SCHEMA, VOLUME, get_client

LOG_FILES = {"spark": "Spark_2k.log", "hdfs": "HDFS_2k.log"}
# Each line starts with yy/MM/dd (Spark) or yyMMdd (HDFS)
DAY_TOKEN = {"spark": "{yy}/{mm}/{dd}", "hdfs": "{yy}{mm}{dd}"}
DAY_PATTERN = {"spark": re.compile(r"^(\d{2})/(\d{2})/(\d{2}) "), "hdfs": re.compile(r"^(\d{2})(\d{2})(\d{2}) ")}
# Upstream re-sent a day's export under the next date with malformed timestamps:
# (source, delivered date) -> the real day its content was copied from
CORRUPTED_DELIVERIES = {("spark", "2017-06-10"): "2017-06-09", ("hdfs", "2008-11-12"): "2008-11-11"}
AUDIT_DDL = DATABRICKS_DIR / "sql" / "audit_tables.sql"


def split_by_day(source: str, text: str) -> dict[str, str]:
    """{YYYY-MM-DD: lines of that day}; lines without a timestamp stay with the previous day."""
    days: dict[str, list[str]] = {}
    day = None
    for line in text.splitlines(keepends=True):
        m = DAY_PATTERN[source].match(line)
        if m:
            day = f"20{m[1]}-{m[2]}-{m[3]}"
        if day:
            days.setdefault(day, []).append(line)
    return {d: "".join(lines) for d, lines in days.items()}


def redate(source: str, text: str, from_day: str, to_day: str) -> str:
    """Rewrite the leading date token of each line from one day to another."""

    def token(iso: str) -> str:
        yyyy, mm, dd = iso.split("-")
        return DAY_TOKEN[source].format(yy=yyyy[2:], mm=mm, dd=dd)

    old, new = token(from_day), token(to_day)
    return "".join(new + line[len(old) :] if line.startswith(old) else line for line in text.splitlines(keepends=True))


def corrupt_timestamps(text: str, every: int = 3, seed: int = 7) -> str:
    """Mangle the leading timestamp on every Nth line, as a broken upstream export would."""
    rng = random.Random(seed)
    lines = text.splitlines(keepends=True)
    for i in range(0, len(lines), every):
        lines[i] = re.sub(r"^[\d/ :]+", lambda _: f"??{rng.randint(0, 99):02d}-?? ", lines[i], count=1)
    return "".join(lines)


def daily_drops() -> dict[tuple[str, str], str]:
    """(source, date) -> file content for every delivery in the landing volume."""
    drops = {}
    for source, fname in LOG_FILES.items():
        for day, text in split_by_day(source, (EXTERNAL_DATA_DIR / "loghub" / fname).read_text()).items():
            drops[(source, day)] = text
    for (source, day), copied_from in CORRUPTED_DELIVERIES.items():
        drops[(source, day)] = corrupt_timestamps(redate(source, drops[(source, copied_from)], copied_from, day))
    return drops


def ensure_schema_and_volume(w) -> None:
    try:
        w.schemas.get(f"{CATALOG}.{SCHEMA}")
        print(f"schema {CATALOG}.{SCHEMA} exists")
    except NotFound:
        w.schemas.create(name=SCHEMA, catalog_name=CATALOG)
        print(f"created schema {CATALOG}.{SCHEMA}")
    try:
        w.volumes.read(f"{CATALOG}.{SCHEMA}.{VOLUME}")
        print(f"volume {VOLUME} exists")
    except NotFound:
        w.volumes.create(catalog_name=CATALOG, schema_name=SCHEMA, name=VOLUME, volume_type=VolumeType.MANAGED)
        print(f"created volume {VOLUME}")


def run_sql_file(w, path) -> None:
    """Run each statement of a .sql file on the first available SQL warehouse."""
    warehouse = next(iter(w.warehouses.list()), None)
    if warehouse is None:
        raise RuntimeError("no SQL warehouse found — create one to provision the audit tables")
    sql = "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("--"))
    for body in (s.strip() for s in sql.split(";")):
        if not body:
            continue
        resp = w.statement_execution.execute_statement(statement=body, warehouse_id=warehouse.id, wait_timeout="50s")
        while resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
            time.sleep(5)
            resp = w.statement_execution.get_statement(resp.statement_id)
        if resp.status.state != StatementState.SUCCEEDED:
            raise RuntimeError(f"statement failed: {resp.status.error.message if resp.status.error else resp.status}")
        print(f"ok: {body.splitlines()[0]}")


def main() -> None:
    w = get_client()
    ensure_schema_and_volume(w)
    run_sql_file(w, AUDIT_DDL)
    volume_root = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"
    for (source, day), text in sorted(daily_drops().items()):
        remote = f"{volume_root}/{source}/{day}/{LOG_FILES[source]}"
        w.files.upload(remote, io.BytesIO(text.encode()), overwrite=True)
        print(f"uploaded {remote} ({text.count(chr(10)):,} lines)")


if __name__ == "__main__":
    main()
