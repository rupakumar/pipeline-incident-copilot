"""ActionExecutor: start the approved rerun. The only code in the copilot that changes anything.

Platform-neutral: the guardrail and the parameter rules live here; the connector of the incident's platform
(ConnectorHub) only reads the job's defaults and starts the run.
"""

from pipeline_incident_copilot.connectors.hub import ConnectorHub
from pipeline_incident_copilot.guardrails.action import safe_actions
from pipeline_incident_copilot.schemas.agent_outputs import ProposedAction
from pipeline_incident_copilot.schemas.incident import Incident
from pipeline_incident_copilot.schemas.remediation import RemediationResult


def rerun_parameters(failed: dict[str, str], defaults: dict[str, str], action: dict[str, str]) -> dict[str, str]:
    """The failed run's non-default settings with the fix applied; "" in the fix means 'job default'."""
    params = {k: v for k, v in failed.items() if v != defaults.get(k)}
    for key, value in action.items():
        if value == "" or value == defaults.get(key):
            params.pop(key, None)
        else:
            params[key] = value
    return params


class ActionExecutor:
    def __init__(self, hub: ConnectorHub):
        self.hub = hub

    def execute(
        self, incident: Incident, action: ProposedAction, effective: dict[str, str], last_good: dict | None = None
    ) -> RemediationResult:
        # defence in depth: the guardrail runs again right before anything changes
        if (why := safe_actions.check(action, effective, last_good, safe_actions.rules_for(incident))) is not None:
            return RemediationResult(refused=why)
        connector = self.hub.for_incident(incident)
        params = rerun_parameters(
            incident.run.parameters, connector.job_defaults(incident.run.job_id), action.parameters
        )
        return connector.start_rerun(incident, params)
