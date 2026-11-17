"""ConnectorHub: the live connector for a saved connection (or for the connection an incident came from)."""

from pipeline_incident_copilot.connectors.base import PipelineConnector
from pipeline_incident_copilot.connectors.catalog import PLATFORMS, PlatformSpec
from pipeline_incident_copilot.schemas.connection import Connection
from pipeline_incident_copilot.schemas.incident import Incident
from pipeline_incident_copilot.security.secret_store import SecretStore
from pipeline_incident_copilot.storage.connection_repository import ConnectionRepository


class ConnectorHub:
    def __init__(
        self,
        connections: ConnectionRepository,
        secrets: SecretStore,
        platforms: dict[str, PlatformSpec] = PLATFORMS,
    ):
        self.connections = connections
        self.secrets = secrets
        self.platforms = platforms
        self._live: dict[str, tuple[str, PipelineConnector]] = {}  # id → (connection updated_at, connector)

    def build(self, connection: Connection, secrets: dict[str, str]) -> PipelineConnector:
        """A connector from settings + secrets (also used by the connect form before anything is saved)."""
        spec = self.platforms.get(connection.platform)
        if spec is None or not spec.available:
            raise ValueError(f"{connection.platform} connections are not available yet")
        return spec.build(connection, secrets)

    def stored_secrets(self, connection: Connection) -> dict[str, str]:
        return {f: v for f in connection.secret_fields if (v := self.secrets.get(connection.connection_id, f))}

    def get(self, connection_id: str) -> PipelineConnector:
        """Cached per connection; rebuilt when the connection was edited (in the console, another process)."""
        connection = self.connections.get(connection_id)
        if connection is None:
            raise KeyError(f"connection {connection_id} not found (deleted?)")
        version = connection.updated_at.isoformat()
        cached = self._live.get(connection_id)
        if cached is None or cached[0] != version:
            self._live[connection_id] = (version, self.build(connection, self.stored_secrets(connection)))
        return self._live[connection_id][1]

    def for_incident(self, incident: Incident) -> PipelineConnector:
        return self.get(incident.run.connection_id)

    def forget(self, connection_id: str) -> None:
        """Drop the cached connector after the connection was edited or deleted."""
        self._live.pop(connection_id, None)
