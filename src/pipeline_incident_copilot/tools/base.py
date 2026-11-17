"""The contract every investigation tool follows (agents see name, description and Args)."""

from typing import Protocol

from pydantic import BaseModel

from pipeline_incident_copilot.schemas.evidence import ToolResult


class InvestigationTool(Protocol):
    name: str
    description: str  # what it checks; shown to agents choosing a tool
    Args: type[BaseModel]

    def run(self, args: BaseModel) -> ToolResult:
        """Check one fact. Never changes anything. Errors become ok=False results, not exceptions."""
        ...
