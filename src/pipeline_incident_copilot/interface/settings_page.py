# ruff: noqa: E501  (HTML, CSS and JS templates read better unwrapped)
"""The console's Settings page (http://localhost:8787/settings): email delivery and Grafana Cloud.

GET  /settings                 the form; each field shows where its value comes from now (console, .env, not set)
POST /settings/save            save (secrets to the Keychain; an empty secret keeps the stored one)
POST /settings/test-email      JSON: send a test email with the saved settings

Replaces the GMAIL_* / NOTIFY_EMAIL_TO / GRAFANA_* lines of .env; .env stays a fallback until a value is saved here.
"""

import hmac
import json
from html import escape

from pipeline_incident_copilot.interface.console_ui import frame
from pipeline_incident_copilot.security.app_settings import FIELDS, AppSettings, SettingField

JSON = "application/json"
SECTIONS = {
    "email": (
        "Email delivery",
        "The account that sends ticket emails and the nightly digest. Approvers and notify lists are set per "
        "connection; these recipients are the default.",
    ),
    "dashboards": (
        "Grafana Cloud",
        "Where the pipeline-health dashboard is published (python -m scripts.publish_grafana_dashboard). The "
        "Databricks read-only token Grafana queries with is set on the Databricks connection.",
    ),
}
SOURCE_BADGE = {
    "console": '<span class="source console">saved here</span>',
    ".env": '<span class="source env">from .env</span>',
    None: '<span class="source none">not set</span>',
}


class SettingsPage:
    def __init__(self, settings: AppSettings, notifier, form_key: str):
        self.settings = settings
        self.notifier = notifier  # EmailNotifier: the test email goes through the real sender
        self.form_key = form_key

    def get(self, path: str, query: dict[str, list[str]]):
        if path.rstrip("/") == "/settings":
            return 200, self.render(query.get("saved", [""])[0] == "1")
        return 404, frame("Not found", '<div class="banner bad">Not found.</div>', "settings")

    def post(self, path: str, form: dict[str, list[str]]):
        value = {k: v[0] for k, v in form.items()}
        if not hmac.compare_digest(value.get("form_key", ""), self.form_key):
            return 403, frame("Form expired", '<div class="banner bad">Reload the page and try again.</div>', "")
        if path == "/settings/save":
            errors = self._errors(value)
            if errors:
                return 400, self.render(False, "Fix these first: " + "; ".join(errors), value)
            self.settings.save({f.name: value.get(f.name, "") for f in FIELDS})
            return 303, "/settings?saved=1"
        if path == "/settings/test-email":
            return 200, json.dumps(self.test_email()), JSON
        return 404, frame("Not found", '<div class="banner bad">Not found.</div>', "settings")

    @staticmethod
    def _errors(value: dict[str, str]) -> list[str]:
        from pipeline_incident_copilot.interface.connections_page import bad_emails
        from pipeline_incident_copilot.security.app_settings import split_emails

        errors = []
        if wrong := bad_emails(split_emails(value.get("GMAIL_SMTP_USER")) + split_emails(value.get("NOTIFY_EMAIL_TO"))):
            errors.append(f"not an email address: {', '.join(wrong)}")
        url = value.get("GRAFANA_URL", "").strip()
        if url and not url.startswith("https://"):
            errors.append("Grafana Cloud URL must start with https://")
        return errors

    def test_email(self) -> dict:
        if not self.notifier.configured:
            return {"ok": False, "message": "Save the sender address and app password first."}
        to = self.notifier.default_recipients()
        ok = self.notifier.send_message(
            "Pipeline Incident Copilot: test email",
            "This is a test from the console's Settings page. Ticket emails will arrive like this one.",
            what="test email",
            to=to,
        )
        if ok:
            return {"ok": True, "message": f"Sent to {', '.join(to)}."}
        return {"ok": False, "message": "Gmail refused it: check the address and the app password."}

    def _field(self, f: SettingField, typed: dict[str, str] | None) -> str:
        source = self.settings.source(f.name)
        fid = f"s-{f.name}"
        if f.kind == "secret":
            value, kind = "", "password"
            placeholder = "saved in the Keychain · leave empty to keep it" if source == "console" else f.placeholder
            if source == ".env":
                placeholder = "now read from .env · type it here to move it into the Keychain"
        else:
            saved = self.settings._saved(f.name) or ""
            value = (typed or {}).get(f.name, saved)
            kind = {"url": "url", "email": "email"}.get(f.kind, "text")
            placeholder = (
                f"now from .env: {self.settings.get(f.name)}" if source == ".env" and not saved else f.placeholder
            )
        return (
            f'<div class="field"><label for="{fid}">{escape(f.label)}{SOURCE_BADGE[source]}</label>'
            f'<input id="{fid}" name="{f.name}" type="{kind}" value="{escape(value, quote=True)}" '
            f'placeholder="{escape(placeholder, quote=True)}"{" autocomplete=off" if f.kind == "secret" else ""}>'
            f'<span class="help">{escape(f.help)}</span></div>'
        )

    def render(self, saved: bool = False, error: str = "", typed: dict[str, str] | None = None) -> str:
        sections = ""
        for key, (title, intro) in SECTIONS.items():
            fields = "".join(self._field(f, typed) for f in FIELDS if f.section == key)
            extra = (
                '<div class="row"><button type="button" class="btn" id="test-email">Send test email</button>'
                '<span class="small muted" id="test-email-msg" role="status"></span></div>'
                if key == "email"
                else ""
            )
            sections += (
                f'<section class="card" style="padding:20px 22px"><h2>{escape(title)}</h2>'
                f'<p class="small muted" style="margin:4px 0 16px;max-width:70ch">{escape(intro)}</p>{fields}{extra}</section>'
            )
        on_env = [f.name for f in FIELDS if self.settings.source(f.name) == ".env"]
        banner = (
            '<div class="banner ok">Saved. The watcher and console use the new values from the next email on.</div>'
            if saved
            else f'<div class="banner bad">{escape(error)}</div>'
            if error
            else ""
        )
        env_note = (
            f'<div class="banner info small">Still read from .env: {escape(", ".join(on_env))}. Save them here and the '
            "lines can be removed from .env.</div>"
            if on_env
            else ""
        )
        body = f"""{banner}
<div class="head"><div><div class="eyebrow">Settings</div><h1>Email and dashboards</h1>
<p class="sub">Secrets are stored in the macOS Keychain. Saved values take priority over .env.</p></div></div>{env_note}
<form method="post" action="/settings/save" id="sform" style="display:grid;gap:16px;max-width:760px">
<input type="hidden" name="form_key" value="{escape(self.form_key, quote=True)}">{sections}
<div class="row"><button class="btn primary" type="submit">Save settings</button></div></form>"""
        js = r"""
const tb=document.getElementById('test-email'), tm=document.getElementById('test-email-msg');
tb.addEventListener('click',async()=>{tb.disabled=true;tm.textContent='Sending…';
  try{const d=await postForm('/settings/test-email',document.getElementById('sform'));tm.textContent=d.message;
    tm.style.color=d.ok?'var(--good)':'var(--bad)';}catch(e){tm.textContent='No answer: '+e;}finally{tb.disabled=false;}});
"""
        return frame("Settings", body, "settings", js)
