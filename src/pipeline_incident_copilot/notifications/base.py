"""The Notifier contract and the event it sends."""

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel

from pipeline_incident_copilot.schemas.agent_outputs import Resolution, RootCause
from pipeline_incident_copilot.schemas.evidence import EvidenceBundle
from pipeline_incident_copilot.schemas.incident import Incident
from pipeline_incident_copilot.schemas.remediation import RemediationResult


class EventKind(StrEnum):
    INCIDENT_OPENED = "incident_opened"
    DIAGNOSIS_READY = "diagnosis_ready"  # Phase 6
    RESOLVED = "resolved"  # Phase 6
    ESCALATED = "escalated"  # Phase 6


class IncidentEvent(BaseModel):
    kind: EventKind
    incident: Incident
    evidence: EvidenceBundle | None = None
    note: str = ""
    root_cause: RootCause | None = None
    resolution: Resolution | None = None
    remediation: RemediationResult | None = None
    confidence: dict | None = None  # {model, calibrated, signals}: how sure the copilot is, and why
    auto_approval: dict | None = None  # AutoApproval as JSON: matched known fix and why it did / didn't auto-run


class Notifier(Protocol):
    def send(self, event: IncidentEvent) -> bool:
        """Deliver the event; return False (never raise) if it could not be delivered."""
        ...


class ConsoleNotifier:
    """Fallback when no email is configured: print what would have been sent."""

    def send(self, event: IncidentEvent) -> bool:
        from pipeline_incident_copilot.notifications.templates import render

        subject, _ = render(event)
        print(f"[notify] {subject}  (email not configured: set GMAIL_SMTP_USER / GMAIL_APP_PASSWORD)")
        return True

    def send_message(self, subject: str, text: str, html: str | None = None) -> bool:
        print(f"[notify] {subject}\n{text}")
        return True
