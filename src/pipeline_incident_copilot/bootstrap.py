"""Composition root: the one place that picks concrete classes and wires them together."""

import os
from dataclasses import dataclass
from functools import cached_property

from databricks.sdk import WorkspaceClient

from pipeline_incident_copilot.config import PROJECT_ROOT
from pipeline_incident_copilot.connectors.hub import ConnectorHub
from pipeline_incident_copilot.detection.failure_watcher import FailureWatcher
from pipeline_incident_copilot.detection.incident_intake import IncidentIntake
from pipeline_incident_copilot.integrations.databricks_client import JOB_NAME, get_client
from pipeline_incident_copilot.integrations.databricks_sql import SqlWarehouse
from pipeline_incident_copilot.integrations.git_client import GitClient
from pipeline_incident_copilot.notifications.approval_links import ApprovalLinks
from pipeline_incident_copilot.notifications.base import Notifier
from pipeline_incident_copilot.notifications.email_notifier import EmailNotifier
from pipeline_incident_copilot.schemas.connection import DEFAULT_CONNECTION_ID, Connection
from pipeline_incident_copilot.security.app_settings import AppSettings
from pipeline_incident_copilot.security.secret_store import KeychainSecretStore, SecretStore
from pipeline_incident_copilot.storage.connection_repository import ConnectionRepository
from pipeline_incident_copilot.storage.database import connect
from pipeline_incident_copilot.storage.incident_repository import IncidentRepository
from pipeline_incident_copilot.tools.registry import ToolRegistry
from pipeline_incident_copilot.workflow.checkpointer import sqlite_checkpointer
from pipeline_incident_copilot.workflow.diagnoser import IncidentDiagnoser


@dataclass
class App:
    job_name: str = JOB_NAME

    @cached_property
    def workspace(self) -> WorkspaceClient:
        return get_client()

    @cached_property
    def repo(self) -> IncidentRepository:
        return IncidentRepository(connect())

    @cached_property
    def connections(self) -> ConnectionRepository:
        connections = ConnectionRepository(self.repo.conn)
        seed_default_connection(connections, self.repo, self.job_name)
        return connections

    @cached_property
    def secrets(self) -> SecretStore:
        return KeychainSecretStore()

    @cached_property
    def settings(self) -> AppSettings:
        """Email + Grafana settings entered in the console (.env only as the fallback)."""
        return AppSettings(self.repo.conn, self.secrets)

    @cached_property
    def hub(self) -> ConnectorHub:
        return ConnectorHub(self.connections, self.secrets)

    @cached_property
    def tools(self) -> ToolRegistry:
        return ToolRegistry.default(self.workspace, SqlWarehouse(self.workspace), GitClient())

    @cached_property
    def notifier(self) -> Notifier:
        # reads the account and each connection's recipients on every send; prints instead when email is not set up
        return EmailNotifier(self.settings, self.connections, ApprovalLinks.from_env())

    @cached_property
    def watcher(self) -> FailureWatcher:
        return FailureWatcher(self.repo, self.connections, self.hub)

    @cached_property
    def intake(self) -> IncidentIntake:
        return IncidentIntake(self.repo, self.hub, self.notifier)

    @cached_property
    def memory(self):
        from pipeline_incident_copilot.learning.incident_memory import IncidentMemory
        from pipeline_incident_copilot.storage.memory_repository import MemoryRepository

        return IncidentMemory(MemoryRepository(self.repo.conn))

    @cached_property
    def agent_commands(self):
        """The copilot as JSON tools for Claude Code subagents (cli: evidence, search, check, apply)."""
        from pipeline_incident_copilot.interface.agent_commands import AgentCommands
        from pipeline_incident_copilot.remediation.action_executor import ActionExecutor
        from pipeline_incident_copilot.remediation.run_verifier import RunVerifier
        from pipeline_incident_copilot.storage.fix_request_repository import FixRequestRepository

        return AgentCommands(
            self.repo,
            self.tools,
            ActionExecutor(self.hub),
            RunVerifier(self.hub),
            self.memory,
            self.notifier,
            lambda incident: self.hub.for_incident(incident).job_defaults(incident.run.job_id),
            FixRequestRepository(self.repo.conn),
            os.getenv("COPILOT_APPROVAL_BASE_URL", "http://localhost:8787"),
            PROJECT_ROOT / "data" / "runtime" / "claude_skill" / "audit",
        )

    @cached_property
    def diagnoser(self) -> IncidentDiagnoser:
        # imported here: loading the agents pulls in the retrieval models
        from pipeline_incident_copilot.agents.incident_agent import IncidentAgent
        from pipeline_incident_copilot.agents.knowledge_agent import KnowledgeAgent
        from pipeline_incident_copilot.agents.log_analysis_agent import LogAnalysisAgent
        from pipeline_incident_copilot.agents.resolution_agent import ResolutionAgent
        from pipeline_incident_copilot.agents.root_cause_agent import RootCauseAgent
        from pipeline_incident_copilot.remediation.action_executor import ActionExecutor
        from pipeline_incident_copilot.remediation.run_verifier import RunVerifier
        from pipeline_incident_copilot.workflow.graph import Agents, Remediation, build_graph

        agents = Agents(
            incident=IncidentAgent(),
            log_analysis=LogAnalysisAgent(),
            knowledge=KnowledgeAgent(),
            root_cause=RootCauseAgent(self.tools),
            resolution=ResolutionAgent(),
        )
        remediation = Remediation(executor=ActionExecutor(self.hub), verifier=RunVerifier(self.hub))
        from pipeline_incident_copilot.learning.auto_approval import AutoApprovalPolicy

        policy = AutoApprovalPolicy(self.memory.repo, self.connections, self.repo)
        graph = build_graph(agents, self.repo, remediation, sqlite_checkpointer(), self.memory, policy)
        return IncidentDiagnoser(self.repo, graph, self.notifier)


def seed_default_connection(connections: ConnectionRepository, repo: IncidentRepository, job_name: str) -> None:
    """Once per store: register the Databricks workspace from .env (the original setup) as a connection,
    so existing incidents and the watcher keep working. Deleting it later is respected."""
    if repo.get_state("seeded:default_connection") or connections.get(DEFAULT_CONNECTION_ID):
        return
    connections.save(
        Connection(
            connection_id=DEFAULT_CONNECTION_ID,
            name="LogHub ETL (Databricks Free Edition)",
            platform="databricks",
            settings={"credentials": "env"},  # DATABRICKS_HOST / DATABRICKS_TOKEN from .env until the form saves them
            pipelines=[job_name],
        )
    )
    repo.set_state("seeded:default_connection", "yes")
