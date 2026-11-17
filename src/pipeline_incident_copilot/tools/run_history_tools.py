"""Run history checks on the pipeline_runs audit table."""

import json

from pydantic import BaseModel, Field

from pipeline_incident_copilot.integrations.databricks_sql import SqlWarehouse
from pipeline_incident_copilot.schemas.evidence import RunComparison, RunSnapshot, ToolResult

PIPELINE_RUNS = "workspace.pipeline_demo.pipeline_runs"
COLUMNS = (
    "job_run_id, run_date, source, status, failed_task, error_summary, bronze_rows, silver_rows, gold_rows,"
    " input_path, code_version, parameters, started_at, duration_seconds"
)


def to_snapshot(row: dict) -> RunSnapshot:
    as_int = lambda v: int(v) if v not in (None, "") else None  # noqa: E731
    return RunSnapshot(
        **{k: row[k] for k in ("job_run_id", "run_date", "source", "status", "failed_task", "error_summary")},
        bronze_rows=as_int(row["bronze_rows"]),
        silver_rows=as_int(row["silver_rows"]),
        gold_rows=as_int(row["gold_rows"]),
        input_path=row["input_path"],
        code_version=row["code_version"],
        parameters=json.loads(row["parameters"] or "{}"),
        started_at=row["started_at"],
        duration_seconds=float(row["duration_seconds"]) if row["duration_seconds"] else None,
    )


def changed_parameters(good: dict[str, str], bad: dict[str, str]) -> dict[str, tuple[str, str]]:
    return {
        k: (good.get(k, ""), bad.get(k, "")) for k in sorted(set(good) | set(bad)) if good.get(k, "") != bad.get(k, "")
    }


class CompareWithLastSuccess:
    name = "compare_with_last_success"
    description = (
        "Compare a failed run with the last successful run of the same source: parameters that changed, "
        "row counts per layer, duration, input path and code version."
    )

    class Args(BaseModel):
        job_run_id: str = Field(description="Databricks job run id of the failed run")
        # Fallbacks from the Jobs API, used when the run wrote no audit row (record_run failed or timed out)
        source: str | None = Field(default=None, description="spark | hdfs")
        parameters: dict[str, str] = Field(default_factory=dict, description="the run's job parameters")
        started_at: str | None = Field(default=None, description="ISO start time of the failed run")
        code_version: str | None = Field(default=None, description="commit the failed run was deployed from")
        failed_task: str | None = None

    def __init__(self, sql: SqlWarehouse):
        self.sql = sql

    def compare(self, args: "CompareWithLastSuccess.Args") -> RunComparison:
        rows = self.sql.query(f"SELECT {COLUMNS} FROM {PIPELINE_RUNS} WHERE job_run_id = :run", run=args.job_run_id)
        audit_row_found = bool(rows)
        failed = to_snapshot(rows[0]) if rows else self._from_jobs_api(args)
        source = failed.source
        before = failed.started_at.isoformat() if failed and failed.started_at else None
        last = self.sql.query(
            f"SELECT {COLUMNS} FROM {PIPELINE_RUNS} WHERE source = :source AND status = 'SUCCEEDED'"
            " AND (:before IS NULL OR started_at < CAST(:before AS TIMESTAMP)) ORDER BY started_at DESC LIMIT 1",
            source=source,
            before=before,
        )
        last_success = to_snapshot(last[0]) if last else None
        return RunComparison(
            failed=failed,
            last_success=last_success,
            changed_parameters=changed_parameters(last_success.parameters, failed.parameters)
            if failed and last_success
            else {},
            code_changed=bool(
                last_success and failed.code_version and failed.code_version != last_success.code_version
            ),
            audit_row_found=audit_row_found,
        )

    @staticmethod
    def _from_jobs_api(args: "CompareWithLastSuccess.Args") -> RunSnapshot:
        p = args.parameters
        return RunSnapshot(
            job_run_id=args.job_run_id,
            run_date=p.get("run_date"),
            source=args.source or p.get("source"),
            status="FAILED",
            failed_task=args.failed_task,
            error_summary=None,
            bronze_rows=None,
            silver_rows=None,
            gold_rows=None,
            input_path=p.get("input_path") or None,
            code_version=args.code_version,
            parameters=p,
            started_at=args.started_at,
            duration_seconds=None,
        )

    def run(self, args: "CompareWithLastSuccess.Args") -> ToolResult:
        try:
            c = self.compare(args)
        except (RuntimeError, TimeoutError) as e:
            return ToolResult(tool=self.name, args=args.model_dump(), ok=False, summary=f"cannot read run history: {e}")
        if c.last_success is None:
            summary = f"no earlier successful {args.source or ''} run found".replace("  ", " ")
        else:
            changes = "; ".join(
                f"{k}: {a or '(default)'} → {b or '(default)'}" for k, (a, b) in c.changed_parameters.items()
            )
            summary = (
                f"last success: run {c.last_success.job_run_id} ({c.last_success.run_date}, "
                f"{c.last_success.bronze_rows} bronze rows). Changed: {changes or 'no parameters'}; "
                f"code {'changed' if c.code_changed else 'unchanged'}"
            )
            if not c.audit_row_found:
                summary += " (no audit row for this run: record_run did not finish; compared with Jobs API data)"
        return ToolResult(
            tool=self.name,
            args=args.model_dump(),
            ok=True,
            summary=summary,
            data={"comparison": c.model_dump(mode="json")},
        )
