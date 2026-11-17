"""TriageState: the one object passed through the agent graph. Each agent fills its own field."""

import operator
from typing import Annotated, TypedDict

from pipeline_incident_copilot.schemas.agent_outputs import (
    IncidentClassification,
    KnowledgeContext,
    LogAnalysis,
    Resolution,
    RootCause,
)
from pipeline_incident_copilot.schemas.evidence import EvidenceBundle, ToolResult
from pipeline_incident_copilot.schemas.incident import Incident
from pipeline_incident_copilot.schemas.remediation import ApprovalDecision, AutoApproval, RemediationResult


class TriageState(TypedDict, total=False):
    incident: Incident
    evidence: EvidenceBundle
    classification: IncidentClassification
    log_analysis: LogAnalysis
    knowledge: KnowledgeContext
    root_cause: RootCause
    confidence: dict  # {"model": float, "calibrated": float, "signals": {...}} (learning/confidence_calibrator)
    resolution: Resolution
    draft_resolution: (
        Resolution | None
    )  # fix written together with the root cause (fast path); checked by ResolutionAgent
    auto_approval: AutoApproval  # whether the fix could skip human approval, and why (or why not)
    approval: ApprovalDecision
    remediation: RemediationResult
    fix_attempts: int  # reruns made so far (at most MAX_FIX_ATTEMPTS)
    extra_checks: Annotated[list[ToolResult], operator.add]  # tool calls the agents made themselves
    trail: Annotated[list[str], operator.add]  # one line per step, for the incident history
