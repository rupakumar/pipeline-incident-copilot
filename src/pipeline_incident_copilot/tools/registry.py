"""The set of investigation tools and the one traced way to call them."""

from typing import Any

from databricks.sdk import WorkspaceClient

from pipeline_incident_copilot.integrations.databricks_sql import SqlWarehouse
from pipeline_incident_copilot.integrations.git_client import GitClient
from pipeline_incident_copilot.observability.langfuse_tracer import get_langfuse
from pipeline_incident_copilot.schemas.evidence import ToolResult
from pipeline_incident_copilot.tools.base import InvestigationTool
from pipeline_incident_copilot.tools.catalog_tools import CheckPermissions, DescribeTable
from pipeline_incident_copilot.tools.code_change_tools import GitDiffSinceLastSuccess
from pipeline_incident_copilot.tools.run_history_tools import CompareWithLastSuccess
from pipeline_incident_copilot.tools.storage_tools import CheckPathExists


class ToolRegistry:
    def __init__(self, tools: list[InvestigationTool]):
        self.tools = {t.name: t for t in tools}

    @classmethod
    def default(cls, w: WorkspaceClient, sql: SqlWarehouse, git: GitClient) -> "ToolRegistry":
        return cls(
            [
                CheckPathExists(w),
                DescribeTable(w),
                CheckPermissions(w),
                CompareWithLastSuccess(sql),
                GitDiffSinceLastSuccess(git),
            ]
        )

    def describe(self) -> list[dict]:
        """Name, description and argument schema of every tool (what an agent is shown)."""
        return [
            {"name": t.name, "description": t.description, "args": t.Args.model_json_schema()}
            for t in self.tools.values()
        ]

    def run(self, name: str, **args: Any) -> ToolResult:
        tool = self.tools[name]
        parsed = tool.Args(**args)
        with get_langfuse().start_as_current_observation(as_type="tool", name=name, input=parsed.model_dump()) as obs:
            result = tool.run(parsed)
            obs.update(output={"ok": result.ok, "summary": result.summary})
        return result
