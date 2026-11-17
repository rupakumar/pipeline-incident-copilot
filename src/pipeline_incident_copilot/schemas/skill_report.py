"""SkillReport: what the /diagnose-incident Claude Code skill writes for one incident.

Same root-cause and resolution shapes as the LangGraph agents, so both are scored by the same
diagnosis metrics and `copilot apply --report` can learn from a fix the skill proposed.
"""

from pydantic import BaseModel, Field

from pipeline_incident_copilot.schemas.agent_outputs import Resolution, RootCause


class SkillReport(BaseModel):
    incident_id: str
    root_cause: RootCause
    resolution: Resolution | None = None
    citations: list[str] = Field(default_factory=list)  # parent ids the diagnosis relied on, best first
    escalated: bool = False  # True when the skill handed the incident to a person instead of proposing a rerun
    warnings: list[str] = Field(default_factory=list)  # e.g. instructions found inside log text (treated as data)
