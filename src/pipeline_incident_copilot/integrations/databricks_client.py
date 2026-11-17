"""Shared Databricks WorkspaceClient: the workspace saved in the console (Keychain), else .env."""

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config

from pipeline_incident_copilot.config import require

CATALOG = "workspace"
SCHEMA = "pipeline_demo"
VOLUME = "raw_logs"
JOB_NAME = "loghub_etl"


def saved_credentials(connection_id: str | None = None) -> tuple[str, str] | None:
    """Host + token of the default Databricks connection when they were entered in the console form."""
    from pipeline_incident_copilot.schemas.connection import DEFAULT_CONNECTION_ID
    from pipeline_incident_copilot.security.secret_store import KeychainSecretStore
    from pipeline_incident_copilot.storage.connection_repository import ConnectionRepository
    from pipeline_incident_copilot.storage.database import connect

    try:
        c = ConnectionRepository(connect()).get(connection_id or DEFAULT_CONNECTION_ID)
        if c is None or c.platform != "databricks" or not c.settings.get("host"):
            return None
        token = KeychainSecretStore().get(c.connection_id, "token")
        return (c.settings["host"], token) if token else None
    except Exception:  # noqa: BLE001 — no store or no Keychain (CI): fall back to .env
        return None


def get_client(host: str | None = None, token: str | None = None) -> WorkspaceClient:
    """A saved connection's host + token when given; otherwise the default workspace from the console form,
    else DATABRICKS_HOST / DATABRICKS_TOKEN from .env."""
    if not (host and token):
        host, token = saved_credentials() or (require("DATABRICKS_HOST"), require("DATABRICKS_TOKEN"))
    cfg = Config(
        host=host,
        token=token,
        retry_timeout_seconds=120,  # rides out slow API spells; still fails well before the 5-minute default
        http_timeout_seconds=20,  # a reused connection the load balancer dropped fails fast, then retries fresh
    )
    return WorkspaceClient(config=cfg)
