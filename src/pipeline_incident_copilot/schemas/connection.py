"""A saved connection to a pipeline platform, and what the watcher last saw on it.

A connection is one platform account (a Databricks workspace, a Jenkins server, ...); its `pipelines` are the
applications the copilot watches there. Secrets never live here: only the names of the fields kept in the
secret store.
"""

from datetime import UTC, datetime

from pydantic import BaseModel, Field

RunId = int | str  # Databricks / Jenkins use numbers, Glue / ADF / Snowflake use strings
DEFAULT_CONNECTION_ID = "databricks-loghub"  # the workspace the copilot was built against (credentials in .env)


class Connection(BaseModel):
    connection_id: str  # slug, e.g. jenkins-local
    name: str
    platform: str  # key in connectors.catalog.PLATFORMS
    settings: dict[str, str] = Field(default_factory=dict)  # non-secret form fields (URL, user name, ...)
    secret_fields: list[str] = Field(default_factory=list)  # names of the fields kept in the secret store
    pipelines: list[str] = Field(default_factory=list)  # jobs to watch
    enabled: bool = True
    auto_apply: bool = False  # known fixes that worked often enough may run without approval (off by default)
    auto_apply_min_successes: int = 2
    auto_apply_cooldown_hours: int = 1  # at most one auto-fix per pipeline in this many hours (0 = no limit)
    approvers: list[str] = Field(default_factory=list)  # get the Approve / Deny email; empty = default recipients
    notify: list[str] = Field(default_factory=list)  # get FYI updates (opened, resolved, escalated)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_check_at: datetime | None = None
    last_check_ok: bool | None = None
    last_check_message: str = ""


class ConnectionTest(BaseModel):
    """Result of trying the credentials: can we reach the platform and see the pipelines?"""

    ok: bool
    message: str
    available: list[str] = Field(default_factory=list)  # pipelines the credentials can see (a sample)
    missing: list[str] = Field(default_factory=list)  # requested pipelines that do not exist


class PipelineHealth(BaseModel):
    """One watched pipeline as of the last poll (what the Connections dashboard shows)."""

    connection_id: str
    pipeline: str
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_run_id: str | None = None
    last_state: str | None = None  # SUCCESS / FAILED / ... (platform words, upper case)
    last_run_at: datetime | None = None
    last_run_url: str | None = None
    runs_24h: int = 0
    failures_24h: int = 0
