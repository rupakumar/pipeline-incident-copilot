"""The incident graph:

start → incident → log_analysis → knowledge (corrective RAG) → root_cause
root_cause ─ sure → resolution → propose          ─ unsure → escalate
propose    ─ safe action → auto_approval          ─ none → end (escalated with steps for a person)
auto_approval ─ known fix, clean record, connection opted in → remediate   ─ otherwise → approval (pause)
approval   ─ approve → remediate → verify         ─ reject → end (rejected, nothing changed)
verify     ─ fixed → learn → end (resolved)       ─ failed → root_cause (one retry) → … → fix_failed
"""

from dataclasses import dataclass
from functools import partial
from typing import Any

from langgraph.graph import END, START, StateGraph

from pipeline_incident_copilot.agents.incident_agent import IncidentAgent
from pipeline_incident_copilot.agents.knowledge_agent import KnowledgeAgent
from pipeline_incident_copilot.agents.log_analysis_agent import LogAnalysisAgent
from pipeline_incident_copilot.agents.resolution_agent import ResolutionAgent
from pipeline_incident_copilot.agents.root_cause_agent import RootCauseAgent
from pipeline_incident_copilot.schemas.triage_state import TriageState
from pipeline_incident_copilot.storage.incident_repository import IncidentRepository
from pipeline_incident_copilot.workflow import nodes
from pipeline_incident_copilot.workflow.routing import (
    by_action,
    by_auto_approval,
    by_confidence,
    by_decision,
    by_rerun_started,
    by_verification,
)
from pipeline_incident_copilot.workflow.subgraphs.corrective_rag import build_corrective_rag


@dataclass
class Agents:
    incident: IncidentAgent
    log_analysis: LogAnalysisAgent
    knowledge: KnowledgeAgent
    root_cause: RootCauseAgent
    resolution: ResolutionAgent


@dataclass
class Remediation:
    executor: Any  # ActionExecutor: execute(incident, action, effective) -> RemediationResult
    verifier: Any  # RunVerifier: verify(result) -> RemediationResult


def build_graph(
    agents: Agents, repo: IncidentRepository, remediation: Remediation, checkpointer, memory=None, auto_policy=None
):
    """`checkpointer` is required: the approval step pauses the graph and resumes it later."""
    g = StateGraph(TriageState)
    g.add_node("start", nodes.start_node(repo))
    g.add_node("incident", nodes.agent_node(agents.incident, repo))
    g.add_node("log_analysis", nodes.agent_node(agents.log_analysis, repo))
    g.add_node("knowledge", nodes.knowledge_node(build_corrective_rag(agents.knowledge), repo))
    g.add_node("root_cause", nodes.agent_node(agents.root_cause, repo))
    g.add_node("resolution", nodes.agent_node(agents.resolution, repo))
    g.add_node("propose", nodes.proposal_node(repo))
    g.add_node("escalate", nodes.escalate_node(repo))
    g.add_node("auto_approval", nodes.auto_approval_node(repo, auto_policy))
    g.add_node("approval", nodes.approval_node(repo))
    g.add_node("remediate", nodes.remediate_node(repo, remediation.executor))
    g.add_node("verify", nodes.verify_node(repo, remediation.verifier))
    g.add_node("fix_failed", nodes.fix_failed_node(repo, memory))
    g.add_node("learn", nodes.learn_node(repo, memory))

    g.add_edge(START, "start")
    g.add_edge("start", "incident")
    g.add_edge("incident", "log_analysis")
    g.add_edge("log_analysis", "knowledge")
    g.add_edge("knowledge", "root_cause")
    g.add_conditional_edges("root_cause", by_confidence, ["resolution", "escalate"])
    g.add_edge("resolution", "propose")
    g.add_conditional_edges("propose", by_action, {"approval": "auto_approval", "end": END})
    g.add_conditional_edges("auto_approval", by_auto_approval, ["remediate", "approval"])
    g.add_conditional_edges("approval", by_decision, {"remediate": "remediate", "end": END})
    g.add_conditional_edges("remediate", by_rerun_started, ["verify", "fix_failed"])
    g.add_conditional_edges(
        "verify",
        partial(by_verification, max_attempts=nodes.MAX_FIX_ATTEMPTS),
        {"end": "learn", "root_cause": "root_cause", "fix_failed": "fix_failed"},
    )
    g.add_edge("escalate", END)
    g.add_edge("fix_failed", END)
    g.add_edge("learn", END)
    return g.compile(checkpointer=checkpointer)
