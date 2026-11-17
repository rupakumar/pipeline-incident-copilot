"""SQLite checkpointer: every graph step is saved, so a paused incident survives restarts.

Our state holds Pydantic models; LangGraph only restores types it is told to trust, so the
schema classes are registered explicitly (unregistered types will be blocked in future versions).
"""

import sqlite3

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from pipeline_incident_copilot.config import RUNTIME_DIR
from pipeline_incident_copilot.schemas import agent_outputs, evidence, incident, remediation

CHECKPOINT_DB = RUNTIME_DIR / "checkpoints.db"

# (module, class) pairs the checkpoint may contain
STATE_TYPES = [
    (incident.__name__, "Incident"),
    (incident.__name__, "IncidentStatus"),
    (incident.__name__, "FailedRun"),
    (evidence.__name__, "EvidenceBundle"),
    (evidence.__name__, "RunComparison"),
    (evidence.__name__, "RunSnapshot"),
    (evidence.__name__, "CodeChange"),
    (evidence.__name__, "ToolResult"),
    (agent_outputs.__name__, "IncidentClassification"),
    (agent_outputs.__name__, "LogAnalysis"),
    (agent_outputs.__name__, "Citation"),
    (agent_outputs.__name__, "KnowledgeContext"),
    (agent_outputs.__name__, "RootCause"),
    (agent_outputs.__name__, "ProposedAction"),
    (agent_outputs.__name__, "Resolution"),
    (remediation.__name__, "ApprovalDecision"),
    (remediation.__name__, "RemediationResult"),
    (remediation.__name__, "AutoApproval"),
]


def sqlite_checkpointer(path=CHECKPOINT_DB) -> SqliteSaver:
    path.parent.mkdir(parents=True, exist_ok=True)
    serde = JsonPlusSerializer(allowed_msgpack_modules=STATE_TYPES)
    return SqliteSaver(sqlite3.connect(path, check_same_thread=False), serde=serde)
