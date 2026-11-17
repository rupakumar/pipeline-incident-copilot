"""EmailNotifier: HTML + plain-text email over Gmail SMTP (TLS).

The sender account and recipients come from the console (Settings page and each connection's form; .env only as a
fallback) and are read on every send, so a change in the console applies without restarting the watcher.

Routing per connection:
- "approval needed" (a fix awaits a decision): one email per approver, each with their OWN signed Approve / Deny
  links, so the history names who decided; notify-only people get the same email without buttons
- every other update (opened, resolved, escalated): approvers + notify list
- a connection with neither list uses the default recipients (NOTIFY_EMAIL_TO)
"""

import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage

from pipeline_incident_copilot.notifications.approval_links import ApprovalLinks
from pipeline_incident_copilot.notifications.base import EventKind, IncidentEvent
from pipeline_incident_copilot.notifications.html_templates import render_html
from pipeline_incident_copilot.notifications.templates import render
from pipeline_incident_copilot.security.app_settings import AppSettings, split_emails

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # implicit TLS


def _smtp() -> smtplib.SMTP_SSL:
    return smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=30)


class EmailNotifier:
    def __init__(
        self,
        settings: AppSettings,
        connections=None,  # ConnectionRepository: per-connection approvers / notify lists
        links: ApprovalLinks | None = None,
        smtp: Callable[[], smtplib.SMTP_SSL] = _smtp,
    ):
        self.settings = settings
        self.connections = connections
        self.links = links
        self.smtp = smtp

    # --- configuration (read on every send) --------------------------------------------------------
    def account(self) -> tuple[str, str] | None:
        user = self.settings.get("GMAIL_SMTP_USER")
        password = (self.settings.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")  # Google shows groups of 4
        return (user, password) if user and password else None

    @property
    def configured(self) -> bool:
        return self.account() is not None

    def default_recipients(self) -> list[str]:
        account = self.account()
        return split_emails(self.settings.get("NOTIFY_EMAIL_TO")) or ([account[0]] if account else [])

    def routes(self, event: IncidentEvent) -> list[tuple[list[str], str | None]]:
        """[(recipients, approver)]: approver is set on the emails that carry that person's Approve / Deny links."""
        c = self.connections.get(event.incident.run.connection_id) if self.connections else None
        approvers = (c.approvers if c else []) or []
        notify = (c.notify if c else []) or []
        if not approvers and not notify:
            approvers = self.default_recipients()
        wants_decision = (
            event.kind is EventKind.DIAGNOSIS_READY and event.resolution and event.resolution.action and self.links
        )
        if wants_decision:
            fyi = [n for n in notify if n not in approvers]
            return [([a], a) for a in approvers] + ([(fyi, None)] if fyi else [])
        everyone = list(dict.fromkeys([*approvers, *notify]))
        return [(everyone, None)] if everyone else []

    # --- sending -----------------------------------------------------------------------------------
    def html(self, event: IncidentEvent, approver: str | None = None) -> str:
        if approver and self.links:
            iid = event.incident.incident_id
            return render_html(
                event,
                self.links.url(iid, "approve", who=approver),
                self.links.url(iid, "reject", who=approver),
                approver=approver,
            )
        return render_html(event)

    def send(self, event: IncidentEvent) -> bool:
        subject, body = render(event)
        if not self.configured:
            print(f"[notify] {subject}  (email not set up: Settings → Email in the console)")
            return True
        what = f"incident {event.incident.incident_id}"
        results = [
            self.send_message(subject, body, self.html(event, approver), what=what, to=to)
            for to, approver in self.routes(event)
        ]
        return bool(results) and all(results)

    def send_message(
        self, subject: str, text: str, html: str | None = None, what: str = "message", to: list[str] | None = None
    ) -> bool:
        """Any email (incident updates, the nightly digest, a test): plain text plus optional HTML."""
        account = self.account()
        recipients = to or self.default_recipients()
        if account is None or not recipients:
            print(f"[notify] {subject}  (email not set up: Settings → Email in the console)")
            return False
        msg = EmailMessage()
        msg["From"] = f"Pipeline Incident Copilot <{account[0]}>"
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        try:
            with self.smtp() as smtp:
                smtp.login(*account)
                smtp.send_message(msg)
            return True
        except (smtplib.SMTPException, OSError) as e:
            print(f"[notify] email failed ({e.__class__.__name__}); the {what} is still saved")
            return False
