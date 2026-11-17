"""RunVerifier: wait for the rerun and say whether the fix worked (and if not, what failed).

The rerun is watched on the connection that started it (RemediationResult.connection_id).
"""

from pipeline_incident_copilot.connectors.hub import ConnectorHub
from pipeline_incident_copilot.schemas.remediation import RemediationResult


class RunVerifier:
    def __init__(self, hub: ConnectorHub):
        self.hub = hub

    def verify(self, result: RemediationResult) -> RemediationResult:
        return self.hub.get(result.connection_id).wait_for_rerun(result)
