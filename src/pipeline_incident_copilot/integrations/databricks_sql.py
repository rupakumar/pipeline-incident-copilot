"""Run read-only SQL on a Databricks SQL warehouse (used for the pipeline_runs / dq_results audit tables)."""

import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementParameterListItem, StatementState

POLL_SECONDS = 3
TIMEOUT_SECONDS = 180  # a stopped serverless warehouse takes a few seconds to start


class SqlWarehouse:
    def __init__(self, w: WorkspaceClient, warehouse_id: str | None = None):
        self.w = w
        self._warehouse_id = warehouse_id

    @property
    def warehouse_id(self) -> str:
        if self._warehouse_id is None:
            warehouse = next(iter(self.w.warehouses.list()), None)
            if warehouse is None:
                raise RuntimeError("no SQL warehouse in this workspace")
            self._warehouse_id = warehouse.id
        return self._warehouse_id

    def query(self, sql: str, **params: str | int | float | None) -> list[dict]:
        """Rows as dicts (values are strings, as the Statement API returns them). Use :name placeholders."""
        resp = self.w.statement_execution.execute_statement(
            statement=sql,
            warehouse_id=self.warehouse_id,
            wait_timeout="30s",
            parameters=[
                StatementParameterListItem(name=k, value=None if v is None else str(v)) for k, v in params.items()
            ],
        )
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
            if time.monotonic() > deadline:
                raise TimeoutError(f"statement still {resp.status.state.value} after {TIMEOUT_SECONDS}s")
            time.sleep(POLL_SECONDS)
            resp = self.w.statement_execution.get_statement(resp.statement_id)
        if resp.status.state != StatementState.SUCCEEDED:
            message = resp.status.error.message if resp.status.error else resp.status.state.value
            raise RuntimeError(f"SQL failed: {message}")
        columns = [c.name for c in resp.manifest.schema.columns]
        return [dict(zip(columns, row, strict=True)) for row in (resp.result.data_array or [])]
