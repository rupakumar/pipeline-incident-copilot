"""`copilot serve`: the page the email's Approve / Deny buttons open (http://localhost:8787).

GET  /decide?token=…  → shows the incident and a Confirm button (GET never changes anything: email
                        scanners open links automatically)
POST /decide          → checks the token again, marks it used, resumes the paused graph in the background
GET  /                → the incident console: every incident and its status
GET  /incident/INC-x  → one incident: details, history, Approve / Deny when a fix awaits approval
GET  /review/INC-x/approve|reject|close → the Grafana Approve / Deny / Close buttons: a fresh signed link, then the same
                        confirm page (opening it changes nothing; Confirm does)
GET|POST /connections…  → connect pipelines (form, test, save, pause, delete): see connections_page.py

Requests must name this console as their Host (blocks DNS-rebinding pages) and POSTs from another site's
Origin are refused.

Listens on 127.0.0.1 only. Tokens are signed, expire in 24 h and work once (see approval_links.py).
"""

import getpass
import json
import os
import secrets
import threading
from collections.abc import Callable
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from pipeline_incident_copilot.connectors.catalog import platform_label
from pipeline_incident_copilot.interface.connections_page import ConnectionsPage
from pipeline_incident_copilot.interface.knowledge_page import KnowledgePage
from pipeline_incident_copilot.interface.settings_page import SettingsPage
from pipeline_incident_copilot.notifications.approval_links import ApprovalLinks, InvalidLink
from pipeline_incident_copilot.notifications.html_templates import (
    AMBER,
    BLUE,
    FONT,
    GREEN,
    INK,
    MUTED,
    RED,
    RULE,
    render_html,
)
from pipeline_incident_copilot.rag.ingestion.document_converter import MAX_BYTES
from pipeline_incident_copilot.schemas.incident import IncidentStatus
from pipeline_incident_copilot.schemas.remediation import ApprovalDecision

NAV = (
    '<div style="display:flex;gap:14px;font-size:14px;margin:0 0 18px"><a href="/" style="font-weight:600">Incidents</a>'
    '<a href="/connections" style="font-weight:600">Connections</a>'
    '<a href="/knowledge" style="font-weight:600">Knowledge</a><a href="/settings" style="font-weight:600">Settings</a>'
    "</div>"
)
MAX_UPLOAD = MAX_BYTES + 1024
HOST = "127.0.0.1"
PORT = 8787


def page(title: str, body: str, color: str = BLUE) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} · Pipeline Incident Copilot</title></head>
<body style="margin:0;background:#F2F5F1;font-family:{FONT};color:{INK}">
<div style="max-width:660px;margin:0 auto;padding:28px 16px">
<div style="font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:{color};font-weight:700">Pipeline Incident Copilot</div>
<h1 style="font-size:24px;margin:6px 0 16px">{escape(title)}</h1>{body}</div></body></html>"""


# approve / reject decide a proposed fix; close retires an escalated ticket a person has handled
REQUIRED_STATUS = {
    "approve": IncidentStatus.AWAITING_APPROVAL,
    "reject": IncidentStatus.AWAITING_APPROVAL,
    "close": IncidentStatus.ESCALATED,
}


class ApprovalService:
    """Decision logic behind the page; HTTP-free so it can be tested on its own."""

    def __init__(self, repo, links: ApprovalLinks, event_for: Callable, start_resume: Callable):
        self.repo = repo
        self.links = links
        self.event_for = event_for  # incident_id -> IncidentEvent (details to show)
        self.start_resume = start_resume  # (incident_id, ApprovalDecision) -> None, runs in the background

    def _check(self, token: str) -> tuple[dict | None, str | None]:
        try:
            link = self.links.verify(token)
        except InvalidLink as e:
            return None, str(e)
        if self.repo.get_state(f"link_used:{link['nonce']}"):
            return None, "this link was already used"
        incident = self.repo.get(link["incident_id"])
        if incident is None:
            return None, f"{link['incident_id']} not found"
        if incident.status is not REQUIRED_STATUS[link["decision"]]:
            return (
                None,
                f"{incident.incident_id} is already {incident.status.value.replace('_', ' ')}; nothing to decide",
            )
        return link, None

    def confirm_page(self, token: str) -> tuple[int, str]:
        link, error = self._check(token)
        if error:
            return 400, page("This link can't be used", f"<p>{escape(error)}.</p>", RED)
        approve, close = link["decision"] == "approve", link["decision"] == "close"
        where = platform_label(self.repo.get(link["incident_id"]).run.platform)
        color, verb = (
            (GREEN, "Approve the fix") if approve else (MUTED, "Close the ticket") if close else (RED, "Deny the fix")
        )
        outcome = (
            f"The approved rerun starts on {escape(where)} right away; you get an email when it is verified."
            if approve
            else "Nothing runs. The ticket is marked closed, as handled by you; the note goes into its history."
            if close
            else f"Nothing in {escape(where)} changes. The incident is closed as rejected."
        )
        details = render_html(self.event_for(link["incident_id"]))
        form = f"""<form method="post" action="/decide" style="margin:18px 0;padding:16px;background:#fff;border:1px solid {RULE};border-radius:12px">
<input type="hidden" name="token" value="{escape(token, quote=True)}">
<label for="note" style="display:block;font-size:13px;color:{MUTED};margin-bottom:6px">Note (optional, saved in the incident history)</label>
<textarea id="note" name="note" rows="2" style="width:100%;box-sizing:border-box;font:inherit;padding:8px;border:1px solid {RULE};border-radius:8px"></textarea>
<button type="submit" style="margin-top:12px;background:{color};color:#fff;border:0;border-radius:8px;padding:12px 24px;font-size:16px;font-weight:600;cursor:pointer">Confirm: {escape(verb.lower())}</button>
<p style="font-size:12px;color:{MUTED};margin:10px 0 0">{outcome}</p>
</form>"""
        return 200, page(f"{verb} for {link['incident_id']}?", form + details, color)

    def decide(self, token: str, note: str) -> tuple[int, str]:
        link, error = self._check(token)
        if error:
            return 400, page("This link can't be used", f"<p>{escape(error)}.</p>", RED)
        self.repo.set_state(f"link_used:{link['nonce']}", "used")  # single use, before anything runs
        if link["decision"] == "close":
            by = f"{link['who'] or getpass.getuser()} ({link['via']})"
            self.repo.add_event(link["incident_id"], "closed", f"by {by}: {note[:500]}".strip(": "))
            self.repo.set_status(link["incident_id"], IncidentStatus.CLOSED, note[:500] or f"closed by {by}")
            return 200, page(
                f"{link['incident_id']} closed",
                "<p>Closed. Nothing was run; the ticket leaves the escalated list.</p>",
                MUTED,
            )
        decision = ApprovalDecision(
            decision=link["decision"], by=f"{link['who'] or getpass.getuser()} ({link['via']})", note=note[:500]
        )
        if holder := self.repo.claim_decision(link["incident_id"], decision.by, decision.decision):
            return 409, page(
                f"{link['incident_id']} was already decided",
                f"<p>Nothing was done: {escape(holder)}.</p>",
                MUTED,
            )
        self.start_resume(link["incident_id"], decision)
        iid = escape(link["incident_id"])
        where = escape(platform_label(self.repo.get(link["incident_id"]).run.platform))
        if decision.decision == "approve":
            body = f"<p>Approved. The rerun of {iid}'s job is starting on {where}. You'll get an email when it is verified (usually 1–3 minutes).</p>"
            return 200, page(f"{link['incident_id']} approved", body, GREEN)
        return 200, page(
            f"{link['incident_id']} denied", f"<p>Denied. Nothing was changed; {iid} is closed as rejected.</p>", RED
        )

    def review(self, incident_id: str, decision: str) -> tuple[int, str]:
        """A dashboard button (Grafana): issue a fresh single-use link and show the usual confirm page."""
        if decision not in REQUIRED_STATUS:
            return 404, page("Not found", "")
        incident = self.repo.get(incident_id)
        if incident is None:
            return 404, page("Not found", f"<p>No incident {escape(incident_id)}.</p>", RED)
        if incident.status is not REQUIRED_STATUS[decision]:
            state = incident.status.value.replace("_", " ")
            return 409, page(f"{incident_id} is {state}", "<p>Nothing to decide: refresh the dashboard.</p>", MUTED)
        return self.confirm_page(self.links.token(incident_id, decision, via="Grafana"))

    def index(self) -> tuple[int, str]:
        """The incident console: every incident, newest first, with its status."""
        incidents = self.repo.recent(limit=200)
        counts: dict[str, int] = {}
        for i in incidents:
            counts[i.status.value] = counts.get(i.status.value, 0) + 1
        chips = " ".join(
            f'<span style="display:inline-block;background:{STATUS_COLORS.get(k, MUTED)};color:#fff;font-size:12px;'
            f'font-weight:700;padding:3px 9px;border-radius:999px;margin:0 6px 6px 0">{escape(k.replace("_", " "))}: {v}</span>'
            for k, v in sorted(counts.items())
        )
        rows = "".join(
            f'<tr><td style="padding:8px 10px;border-bottom:1px solid {RULE}"><a href="/incident/{escape(i.incident_id)}" '
            f'style="color:{BLUE};font-weight:600">{escape(i.incident_id)}</a></td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid {RULE}"><span style="color:{STATUS_COLORS.get(i.status.value, MUTED)};'
            f'font-weight:700">{escape(i.status.value.replace("_", " "))}</span></td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid {RULE}">{escape(i.run.failed_task or i.run.result_state)}</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid {RULE}">{escape(platform_label(i.run.platform))} · '
            f"{escape(i.run.job_name)}{escape(' · ' + i.run_date if i.run_date and '{{' not in i.run_date else '')}</td>"
            f'<td style="padding:8px 10px;border-bottom:1px solid {RULE};color:{MUTED}">{i.updated_at:%Y-%m-%d %H:%M}</td></tr>'
            for i in incidents
        )
        table = (
            f'<div style="overflow-x:auto;background:#fff;border:1px solid {RULE};border-radius:12px">'
            f'<table style="border-collapse:collapse;width:100%;font-size:14px;min-width:560px">'
            f'<tr style="text-align:left;color:{MUTED};font-size:12px;text-transform:uppercase;letter-spacing:.05em">'
            f'<th style="padding:8px 10px">Incident</th><th style="padding:8px 10px">Status</th><th style="padding:8px 10px">'
            f'Failed task</th><th style="padding:8px 10px">Application</th><th style="padding:8px 10px">Updated (UTC)</th></tr>'
            f"{rows}</table></div>"
        )
        return 200, page("Incidents", f"{NAV}<p>{chips}</p>{table}")

    def incident_page(self, incident_id: str) -> tuple[int, str]:
        """One incident: details, history, and Approve / Deny when a fix awaits approval."""
        incident = self.repo.get(incident_id)
        if incident is None:
            return 404, page("Not found", f"<p>No incident {escape(incident_id)}.</p>", RED)
        event = self.event_for(incident_id)
        waiting = incident.status is IncidentStatus.AWAITING_APPROVAL and event.resolution and event.resolution.action
        details = (
            render_html(
                event,
                self.links.url(incident_id, "approve", via="console"),
                self.links.url(incident_id, "reject", via="console"),
            )
            if waiting
            else render_html(event)
        )
        history = "".join(
            f'<tr><td style="padding:5px 10px;color:{MUTED};white-space:nowrap">{escape(e["at"][:19].replace("T", " "))}</td>'
            f'<td style="padding:5px 10px;font-weight:600">{escape(e["kind"])}</td>'
            f'<td style="padding:5px 10px">{escape(e["detail"] or "")}</td></tr>'
            for e in self.repo.events(incident_id)
        )
        history_box = (
            f'<h2 style="font-size:16px;margin:22px 0 8px">History</h2><div style="overflow-x:auto;background:#fff;'
            f'border:1px solid {RULE};border-radius:12px"><table style="border-collapse:collapse;width:100%;font-size:13px;'
            f'min-width:560px">{history}</table></div>'
        )
        back = f'<p><a href="/" style="color:{BLUE}">← all incidents</a></p>'
        where = f"{platform_label(incident.run.platform)} · {incident.run.job_name}"
        back += f'<p style="font-size:13px;color:{MUTED};margin:-6px 0 12px">{escape(where)}</p>'
        return 200, page(f"{incident_id} · {incident.status.value.replace('_', ' ')}", back + details + history_box)


STATUS_COLORS = {
    "open": BLUE,
    "investigating": BLUE,
    "awaiting_approval": AMBER,
    "remediating": BLUE,
    "resolved": GREEN,
    "escalated": RED,
    "rejected": MUTED,
}


def allowed_hosts(port: int) -> set[str]:
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if base := os.getenv("COPILOT_APPROVAL_BASE_URL"):
        hosts.add(urlparse(base).netloc)
    return hosts


def make_handler(
    service: ApprovalService, connections=None, hosts: set[str] | None = None, settings=None, knowledge=None
):
    """`connections` / `settings`: the ConnectionsPage and SettingsPage (None: those routes are off, e.g. in tests)."""

    class Handler(BaseHTTPRequestHandler):
        def _trusted(self) -> bool:
            if hosts and self.headers.get("Host") not in hosts:
                self._send(421, page("Wrong host", "<p>Open the console at http://localhost:8787.</p>", RED))
                return False
            origin = self.headers.get("Origin")
            if self.command == "POST" and origin and hosts and urlparse(origin).netloc not in hosts:
                self._send(403, page("Refused", "<p>This form can only be sent from the console itself.</p>", RED))
                return False
            return True

        def _reply(self, status: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
            """A page, JSON (third element), or (303, location) to redirect after a successful POST."""
            if status == 303:
                self.send_response(303)
                self.send_header("Location", body)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send(status, body, content_type)

        def _send(self, status: int, html: str, content_type: str = "text/html; charset=utf-8") -> None:
            data = html.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            # the token must not leak to other sites; "no-referrer" would also make browsers send Origin: null on
            # the page's own form posts, which the Origin check refuses
            self.send_header("Referrer-Policy", "same-origin")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            if not self._trusted():
                return
            url = urlparse(self.path)
            if connections and (url.path == "/connections" or url.path.startswith("/connections/")):
                self._reply(*connections.get(url.path, parse_qs(url.query)))
            elif knowledge and url.path.rstrip("/") == "/knowledge":
                self._reply(*knowledge.get(url.path, parse_qs(url.query)))
            elif settings and url.path.rstrip("/") == "/settings":
                self._reply(*settings.get(url.path, parse_qs(url.query)))
            elif url.path == "/decide":
                self._send(*service.confirm_page(parse_qs(url.query).get("token", [""])[0]))
            elif url.path == "/":
                self._send(*service.index())
            elif url.path.startswith("/incident/"):
                self._send(*service.incident_page(url.path.removeprefix("/incident/")))
            elif url.path.startswith("/review/") and url.path.count("/") == 3:
                _, _, incident_id, decision = url.path.split("/")
                self._send(*service.review(incident_id, decision))
            else:
                self._send(404, page("Not found", ""))

        def do_POST(self):  # noqa: N802
            if not self._trusted():
                return
            url = urlparse(self.path)
            path = url.path
            if knowledge and path == "/knowledge/upload":  # a raw file body, up to the document size limit
                size = int(self.headers.get("Content-Length", 0))
                if size > MAX_UPLOAD:
                    return self._reply(413, json.dumps({"ok": False, "message": "over 20 MB"}), "application/json")
                return self._reply(*knowledge.upload(self.headers, parse_qs(url.query), self.rfile.read(size)))
            length = min(int(self.headers.get("Content-Length", 0)), 20_000)
            form = parse_qs(self.rfile.read(length).decode())
            if connections and path.startswith("/connections/"):
                return self._reply(*connections.post(path, form))
            if knowledge and path.startswith("/knowledge/"):
                return self._reply(*knowledge.post(path, form))
            if settings and path.startswith("/settings/"):
                return self._reply(*settings.post(path, form))
            if path != "/decide":
                return self._send(404, page("Not found", ""))
            self._send(*service.decide(form.get("token", [""])[0], form.get("note", [""])[0]))

        def log_message(self, fmt, *args):  # one short line per request, never the token
            print(f"[serve] {self.command} {urlparse(self.path).path} → {args[1] if len(args) > 1 else ''}")

    return Handler


def serve(port: int = PORT) -> None:
    from pipeline_incident_copilot.bootstrap import App

    app = App()
    lock = threading.Lock()  # one remediation at a time

    def start_resume(incident_id: str, decision: ApprovalDecision) -> None:
        def work():
            with lock:
                worker = App()  # its own SQLite connection and graph, off the request thread
                try:
                    if worker.agent_commands.pending_request(incident_id):  # a fix the Claude Code skill asked for
                        worker.agent_commands.decide_request(incident_id, decision.decision, decision.by, decision.note)
                    else:
                        worker.diagnoser.resume(incident_id, decision)
                    print(f"[serve] {incident_id}: {worker.repo.get(incident_id).status.value}")
                except Exception as e:  # keep serving; the incident history shows what happened
                    print(f"[serve] {incident_id}: resume failed: {e.__class__.__name__}: {e}")

        threading.Thread(target=work, daemon=True).start()

    def event_for(incident_id: str):
        return app.agent_commands.request_event(incident_id) or app.diagnoser.current_event(incident_id)

    service = ApprovalService(app.repo, ApprovalLinks.from_env(), event_for, start_resume)
    form_key = secrets.token_urlsafe(24)
    connections = ConnectionsPage(app.connections, app.secrets, app.hub, form_key, page, app.settings)
    settings_page = SettingsPage(app.settings, app.notifier, form_key)
    from pipeline_incident_copilot.rag.ingestion.knowledge_library import KnowledgeLibrary

    knowledge = KnowledgePage(KnowledgeLibrary(app.repo.conn), form_key)
    handler = make_handler(service, connections, allowed_hosts(port), settings_page, knowledge)
    server = HTTPServer((HOST, port), handler)  # one request at a time: one SQLite connection
    print(f"approval page on http://{HOST}:{port} (local only). Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
