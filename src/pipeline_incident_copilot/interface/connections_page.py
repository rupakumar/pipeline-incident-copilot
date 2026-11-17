# ruff: noqa: E501  (HTML, CSS and JS templates read better unwrapped)
"""The connect gallery and adapter form of the console (http://localhost:8787/connections).

GET  /connections                    platform gallery (live · adapter ready, paid account · coming) + connected list
GET  /connections/new?platform=x     the adapter form (fields from connectors.catalog); paid tier: read-only preview
GET  /connections/<id>/edit          the same form for a saved connection (secrets are never shown)
POST /connections/test               JSON: try the typed credentials (the adapter animation), nothing saved
POST /connections/save               test again server-side; saves only if the test passes
POST /connections/<id>/live          JSON or redirect: switch the connection on (watched) or off (ignored)
POST /connections/<id>/check|delete

Secrets go to the SecretStore (macOS Keychain); SQLite gets settings and field names only. Every POST carries
the page's form key, so another website open in the browser cannot submit these forms.
"""

import hmac
import json
import re
from html import escape

from pipeline_incident_copilot.connectors.catalog import FormField, PlatformSpec
from pipeline_incident_copilot.connectors.hub import ConnectorHub
from pipeline_incident_copilot.interface.console_ui import adapter, chips, frame, mark, switch
from pipeline_incident_copilot.learning.auto_approval import AUTO_CONFIDENCE
from pipeline_incident_copilot.schemas.connection import Connection, ConnectionTest
from pipeline_incident_copilot.security.app_settings import split_emails
from pipeline_incident_copilot.security.secret_store import SecretStore
from pipeline_incident_copilot.storage.connection_repository import ConnectionRepository

Reply = tuple  # (status, html) | (303, location) | (status, json, "application/json")
JSON = "application/json"
STATE_PILL = {"SUCCESS": "good", "SUCCEEDED": "good", "FAILED": "bad", "FAILURE": "bad", "UNSTABLE": "warn"}
TIER_TEXT = {"live": "Connect", "paid": "Adapter ready · paid account", "coming": "Coming"}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "connection"


def connection_slug(platform: str, name: str) -> str:
    """`Local Jenkins` → local-jenkins; `Production` (Jenkins) → jenkins-production."""
    base = slug(name)
    word = platform.split("_")[0]
    return base if word in base.split("-") else f"{slug(platform)}-{base}"


def split_pipelines(text: str) -> list[str]:
    seen: list[str] = []
    for part in re.split(r"[\n,]", text or ""):
        if (p := part.strip()) and p not in seen:
            seen.append(p)
    return seen


def bad_emails(emails: list[str]) -> list[str]:
    return [e for e in emails if not re.fullmatch(r"[^\s@,;]+@[^\s@,;]+\.[^\s@,;]+", e)]


def uses_env(c: Connection | None) -> bool:
    """The original Databricks setup: credentials still from .env (until the form saves host + token)."""
    return bool(c and c.settings.get("credentials") == "env" and not c.settings.get("host"))


class ConnectionsPage:
    def __init__(
        self,
        connections: ConnectionRepository,
        secrets: SecretStore,
        hub: ConnectorHub,
        form_key: str,
        page=None,  # kept for callers that pass the console's old frame; pages now use console_ui.frame
        settings=None,  # AppSettings: shows whether email is set up and the default recipients
    ):
        self.connections = connections
        self.secrets = secrets
        self.hub = hub
        self.form_key = form_key
        self.settings = settings

    # --- routing -----------------------------------------------------------------------------------
    def get(self, path: str, query: dict[str, list[str]]) -> Reply:
        parts = path.strip("/").split("/")  # ["connections", ...]
        if parts == ["connections"]:
            return self.index(query.get("saved", [""])[0], query.get("note", [""])[0])
        if parts == ["connections", "new"]:
            return self.form_page(query.get("platform", [""])[0], None)
        if len(parts) == 3 and parts[2] == "edit" and (c := self.connections.get(parts[1])):
            return self.form_page(c.platform, c)
        return 404, self._missing()

    def post(self, path: str, form: dict[str, list[str]]) -> Reply:
        value = {k: v[0] for k, v in form.items()}
        if not hmac.compare_digest(value.get("form_key", ""), self.form_key):
            return 403, frame("Form expired", '<div class="banner bad">Reload the page and try again.</div>', "")
        parts = path.strip("/").split("/")
        if parts == ["connections", "save"]:
            return self.save(value)
        if parts == ["connections", "test"]:
            return 200, json.dumps(self.try_form(value)), JSON
        if len(parts) == 3 and (c := self.connections.get(parts[1])):
            action = parts[2]
            if action == "check":
                result = self.check(c)
                return 303, f"/connections?note={'ok' if result.ok else 'failed'}:{c.connection_id}"
            if action == "live":
                on = value.get("live") == "on"
                self.connections.set_enabled(c.connection_id, on)
                self.hub.forget(c.connection_id)
                if value.get("ajax"):
                    return 200, json.dumps({"live": on}), JSON
                return 303, "/connections"
            if action in ("pause", "resume"):  # older forms
                self.connections.set_enabled(c.connection_id, action == "resume")
                self.hub.forget(c.connection_id)
                return 303, "/connections"
            if action == "delete":
                for f in c.secret_fields:
                    self.secrets.delete(c.connection_id, f)
                self.connections.delete(c.connection_id)
                self.hub.forget(c.connection_id)
                return 303, "/connections"
        return 404, self._missing()

    def _missing(self) -> str:
        return frame("Not found", '<div class="banner bad">Not found. <a href="/connections">Back</a></div>', "")

    # --- actions -----------------------------------------------------------------------------------
    def check(self, c: Connection) -> ConnectionTest:
        self.hub.forget(c.connection_id)
        try:
            result = self.hub.get(c.connection_id).test()
        except Exception as e:  # noqa: BLE001 — building the connector failed: that is the check result
            result = ConnectionTest(ok=False, message=f"{e.__class__.__name__}: {e}"[:300])
        self.connections.record_check(c.connection_id, result.ok, result.message)
        return result

    def _test(self, connection: Connection, secrets: dict[str, str]) -> ConnectionTest:
        try:
            return self.hub.build(connection, secrets).test()
        except Exception as e:  # noqa: BLE001 — e.g. a malformed URL
            return ConnectionTest(ok=False, message=f"{e.__class__.__name__}: {e}"[:300])

    def try_form(self, value: dict[str, str]) -> dict:
        """The Test button: the typed credentials (stored ones where a secret is left blank). Nothing is saved."""
        existing = self.connections.get(value.get("connection_id", "")) if value.get("connection_id") else None
        spec = self.hub.platforms.get(existing.platform if existing else value.get("platform", ""))
        if spec is None or not spec.available:
            return {"ok": False, "message": "this platform has no live adapter yet"}
        connection, typed, errors = self._read_form(spec, value, existing, for_test=True)
        if errors:
            return {"ok": False, "message": "; ".join(errors)}
        result = self._test(connection, {**(self.hub.stored_secrets(existing) if existing else {}), **typed})
        return result.model_dump()

    def save(self, value: dict[str, str]) -> Reply:
        existing = self.connections.get(value.get("connection_id", "")) if value.get("connection_id") else None
        spec = self.hub.platforms.get(existing.platform if existing else value.get("platform", ""))
        if spec is None or not spec.available:
            return 400, self._missing()
        connection, typed_secrets, errors = self._read_form(spec, value, existing)
        if errors:
            return 400, self._form(spec, existing, value, ("bad", "Fix these first: " + "; ".join(errors)))
        result = self._test(connection, {**(self.hub.stored_secrets(existing) if existing else {}), **typed_secrets})
        if not result.ok:
            seen = f" Jobs it can see: {', '.join(result.available[:12])}." if result.available else ""
            return 200, self._form(
                spec, existing, value, ("bad", f"Test failed, nothing saved: {result.message}.{seen}")
            )
        if existing is None:
            connection = connection.model_copy(
                update={"connection_id": self.connections.unique_id(connection_slug(spec.platform, connection.name))}
            )
        for name, secret in typed_secrets.items():
            self.secrets.set(connection.connection_id, name, secret)
        self.connections.save(connection)
        self.connections.record_check(connection.connection_id, True, result.message)
        self.hub.forget(connection.connection_id)
        return 303, f"/connections?saved={connection.connection_id}"

    def _read_form(
        self, spec: PlatformSpec, value: dict[str, str], existing: Connection | None, for_test: bool = False
    ) -> tuple[Connection, dict[str, str], list[str]]:
        errors: list[str] = []
        name = value.get("name", "").strip()
        if not name and not for_test:
            errors.append("give the connection a name")
        pipelines = split_pipelines(value.get("pipelines", ""))
        if not pipelines and not for_test:
            errors.append(f"pick at least one entry under {spec.pipelines_label.lower()}")
        typed: dict[str, str] = {}
        settings: dict[str, str] = {}
        # the original .env setup stays on .env until both connect fields are typed in
        env_now = uses_env(existing)
        moving = env_now and all(
            value.get(f.name, "").strip() for f in spec.fields if f.section == "connect" and f.required
        )
        keep_env = env_now and not moving
        if keep_env:
            settings["credentials"] = "env"
        for f in spec.fields:
            v = value.get(f.name, "").strip()
            if keep_env and f.section == "connect":
                continue
            if f.kind == "secret":
                if v:
                    typed[f.name] = v
                elif f.required and not (existing and not env_now and self.secrets.get(existing.connection_id, f.name)):
                    errors.append(f"{f.label} is required")
                continue
            if f.required and not v:
                errors.append(f"{f.label} is required")
            if f.kind == "url" and v and not re.match(r"https?://", v):
                errors.append(f"{f.label} must start with http:// or https://")
            if v:
                settings[f.name] = v.rstrip("/") if f.kind == "url" else v
        approvers, notify = split_emails(value.get("approvers")), split_emails(value.get("notify"))
        if wrong := bad_emails(approvers + notify):
            errors.append(f"not an email address: {', '.join(wrong)}")
        try:
            min_successes = int(value.get("auto_apply_min_successes") or 2)
        except ValueError:
            min_successes = 0
        if not 1 <= min_successes <= 20:
            errors.append("minimum successes must be a whole number from 1 to 20")
        try:
            cooldown = int(value.get("auto_apply_cooldown_hours") or 1)
        except ValueError:
            cooldown = -1
        if not 0 <= cooldown <= 168:
            errors.append("the cooldown must be a whole number of hours from 0 to 168")
        kept = {"created_at": existing.created_at} if existing else {}
        connection = Connection(
            connection_id=existing.connection_id if existing else "unsaved",
            name=name or "-",
            platform=spec.platform,
            settings=settings,
            secret_fields=[f for f in spec.secret_fields if not (keep_env and f == "token")],
            pipelines=pipelines,
            enabled=value.get("live", "on" if existing is None else "") == "on",
            auto_apply=value.get("auto_apply") == "on",
            auto_apply_min_successes=min_successes if 1 <= min_successes <= 20 else 2,
            auto_apply_cooldown_hours=cooldown if 0 <= cooldown <= 168 else 1,
            approvers=approvers,
            notify=notify,
            **kept,
        )
        return connection, typed, errors

    # --- gallery -----------------------------------------------------------------------------------
    def index(self, saved: str = "", note: str = "") -> Reply:
        banner = ""
        if saved:
            banner = f'<div class="banner ok">Saved {escape(saved)}. The watcher picks it up on its next poll.</div>'
        elif note:
            ok, _, cid = note.partition(":")
            if c := self.connections.get(cid):
                banner = f'<div class="banner {"ok" if ok == "ok" else "bad"}">{escape(c.name)}: {escape(c.last_check_message)}</div>'
        conns = self.connections.all()
        live = sum(c.enabled for c in conns)
        failing = sum(c.enabled and c.last_check_ok is False for c in conns)
        watched = sum(len(c.pipelines) for c in conns if c.enabled)
        count = {p: sum(c.platform == p for c in conns) for p in self.hub.platforms}
        tiles = "".join(self._tile(s, count[s.platform], i) for i, s in enumerate(self.hub.platforms.values()))
        cards = "".join(self._card(c) for c in conns) or (
            '<div class="card" style="padding:20px"><p class="muted" style="margin:0">Nothing connected yet. '
            "Pick a platform above.</p></div>"
        )
        body = f"""{banner}
<div class="head"><div><div class="eyebrow">Connections</div><h1>Connect a pipeline platform</h1>
<p class="sub">Pick where your pipelines run. The copilot watches the jobs you choose, diagnoses failures and asks the
approvers you name before it changes anything.</p></div>
<div class="stats"><div class="stat"><b>{live}</b><span>live</span></div>
<div class="stat"><b>{watched}</b><span>jobs watched</span></div>
<div class="stat"><b style="color:{"var(--bad)" if failing else "inherit"}">{failing}</b><span>failing checks</span></div></div></div>
<style>{GALLERY_CSS}</style>
<section aria-label="Platforms" class="gallery">{tiles}</section>
<div class="head" style="margin:34px 0 14px"><h2>Your connections</h2>
<span class="muted small">Switch a connection off to stop watching it. Its tickets and history stay.</span></div>
<section class="conns">{cards}</section>"""
        return 200, frame("Connections", body, "connections", LIVE_JS.replace("__KEY__", json.dumps(self.form_key)))

    def _tile(self, s: PlatformSpec, connected: int, i: int) -> str:
        badge = {
            "live": f'<span class="pill {"good" if connected else "info"}">'
            f"{f'{connected} connected' if connected else 'Live adapter'}</span>",
            "paid": '<span class="pill warn">Needs paid account</span>',
            "coming": '<span class="pill off">Coming</span>',
        }[s.tier]
        inner = (
            f'{mark(s, 48)}<div class="t-body"><b>{escape(s.label)}</b><span class="muted small">{escape(s.category)}'
            f"</span></div>{badge}<p class='t-desc small muted'>{escape(s.description)}</p>"
            f'<span class="t-cta">{TIER_TEXT[s.tier]}{" →" if s.tier != "coming" else ""}</span>'
        )
        style = f"--brand:{s.color};animation-delay:{i * 35}ms"
        if s.tier == "coming":
            return f'<div class="tile coming" style="{style}" aria-disabled="true">{inner}</div>'
        return f'<a class="tile {s.tier}" style="{style}" href="/connections/new?platform={escape(s.platform)}">{inner}</a>'

    def _card(self, c: Connection) -> str:
        spec = self.hub.platforms.get(c.platform)
        if not c.enabled:
            status = '<span class="pill off" data-status>Off</span>'
        elif c.last_check_ok is None:
            status = '<span class="pill info" data-status>Not checked yet</span>'
        else:
            status = (
                '<span class="pill good" data-status>Live</span>'
                if c.last_check_ok
                else '<span class="pill bad" data-status>Check failed</span>'
            )
        health = {h.pipeline: h for h in self.connections.health(c.connection_id)}
        rows = "".join(
            f'<tr><td class="mono">{escape(p)}</td>'
            + (
                f'<td><span class="pill {STATE_PILL.get(h.last_state or "", "off")}">{escape(h.last_state or "-")}</span>'
                + (
                    f' <a href="{escape(h.last_run_url, quote=True)}" target="_blank" rel="noreferrer" class="small">'
                    f"#{escape(h.last_run_id or '')}</a>"
                    if h.last_run_url
                    else ""
                )
                + f'</td><td class="num">{h.runs_24h}</td><td class="num">{h.failures_24h}</td>'
                if (h := health.get(p))
                else '<td class="muted small" colspan="3">not polled yet</td>'
            )
            + "</tr>"
            for p in c.pipelines
        )
        cid = escape(c.connection_id, quote=True)
        key = f'<input type="hidden" name="form_key" value="{escape(self.form_key, quote=True)}">'
        people = c.approvers or ["default recipients"]
        creds = "credentials from .env" if uses_env(c) else "credentials in the Keychain"
        cool = f", {c.auto_apply_cooldown_hours} h cooldown" if c.auto_apply_cooldown_hours else ", no cooldown"
        auto = (
            f"auto-fix on (after {c.auto_apply_min_successes} verified fixes{cool})" if c.auto_apply else "auto-fix off"
        )
        checked = f"checked {c.last_check_at:%d %b %H:%M} UTC · " if c.last_check_at else ""
        label = spec.label if spec else c.platform
        logo = mark(spec, 40) if spec else ""
        return f"""<article class="card conn" data-id="{cid}">
<div class="c-head">{logo}<div class="c-title"><b>{escape(c.name)}</b>
<span class="muted small">{escape(label)} · <span class="mono">{cid}</span></span></div>{status}
<label class="toggle-row small"><span class="muted">Live</span>
<button type="button" class="switch" role="switch" aria-checked="{"true" if c.enabled else "false"}"
 data-live="{cid}" aria-label="Watch {escape(c.name, quote=True)}"></button></label></div>
<div class="c-meta small"><span>Approvers: {escape(", ".join(people))}</span><span>{escape(auto)}</span>
<span>{escape(creds)}</span></div>
<div class="c-check small muted">{escape(checked)}{escape(c.last_check_message or "")}</div>
<div class="c-table"><table><thead><tr><th>{escape(spec.pipelines_label if spec else "Pipelines")}</th><th>Last run</th>
<th class="num">Runs 24 h</th><th class="num">Failed 24 h</th></tr></thead><tbody>{rows}</tbody></table></div>
<div class="c-actions"><form method="post" action="/connections/{cid}/check">{key}<button class="btn">Check now</button></form>
<a class="btn" href="/connections/{cid}/edit">Edit</a>
<form method="post" action="/connections/{cid}/delete" onsubmit="return confirm('Delete {escape(c.name, quote=True)}? Its secrets are removed; its tickets stay.')">{key}
<button class="btn danger">Delete</button></form></div></article>"""

    # --- adapter form ------------------------------------------------------------------------------
    def form_page(self, platform: str, existing: Connection | None) -> Reply:
        spec = self.hub.platforms.get(platform)
        if spec is None or spec.tier == "coming":
            return 404, self._missing()
        values = (
            {
                "name": existing.name,
                "pipelines": "\n".join(existing.pipelines),
                "auto_apply": "on" if existing.auto_apply else "",
                "auto_apply_min_successes": str(existing.auto_apply_min_successes),
                "auto_apply_cooldown_hours": str(existing.auto_apply_cooldown_hours),
                "approvers": ", ".join(existing.approvers),
                "notify": ", ".join(existing.notify),
                "live": "on" if existing.enabled else "",
                **existing.settings,
            }
            if existing
            else {"live": "on", "auto_apply_min_successes": "2", "auto_apply_cooldown_hours": "1"}
        )
        return 200, self._form(spec, existing, values, None)

    def _input(self, f: FormField, value: str, existing: Connection | None, disabled: bool, env_now: bool) -> str:
        fid = f"f-{f.name}"
        kind = {"secret": "password", "url": "url"}.get(f.kind, "text")
        placeholder = f.placeholder
        if f.kind == "secret" and existing and not env_now and f.name in existing.secret_fields:
            if self.secrets.get(existing.connection_id, f.name):
                placeholder = "saved in the Keychain · leave empty to keep it"
        if env_now and f.section == "connect":
            placeholder = "now read from .env · type a value to move it into the Keychain"
        shown = "" if f.kind == "secret" else value
        optional = "" if f.required else ' <span class="muted small">optional</span>'
        return (
            f'<div class="field"><label for="{fid}">{escape(f.label)}{optional}</label>'
            f'<input id="{fid}" name="{escape(f.name)}" type="{kind}" value="{escape(shown, quote=True)}" '
            f'placeholder="{escape(placeholder, quote=True)}"'
            f"{' autocomplete=off' if f.kind == 'secret' else ''}{' disabled' if disabled else ''}>"
            + (f'<span class="help">{escape(f.help)}</span>' if f.help else "")
            + "</div>"
        )

    def _email_status(self) -> str:
        if self.settings is None:
            return ""
        source = self.settings.source("GMAIL_APP_PASSWORD")
        defaults = ", ".join(split_emails(self.settings.get("NOTIFY_EMAIL_TO"))) or "the sender"
        state = {
            "console": '<span class="source console">set in Settings</span>',
            ".env": '<span class="source env">from .env</span>',
            None: '<span class="source none">not set up</span>',
        }[source]
        return (
            f'<p class="small muted">Sender account {state} · <a href="/settings">Settings → Email</a>. '
            f"Empty lists use the default recipients ({escape(defaults)}).</p>"
        )

    def _form(
        self, spec: PlatformSpec, existing: Connection | None, values: dict[str, str], banner: tuple[str, str] | None
    ) -> str:
        preview = spec.tier == "paid"
        env_now = uses_env(existing)
        connect = "".join(
            self._input(f, values.get(f.name, ""), existing, preview, env_now)
            for f in spec.fields
            if f.section == "connect"
        )
        dash_fields = [f for f in spec.fields if f.section == "dashboards"]
        dashboards = "".join(
            self._input(f, values.get(f.name, ""), existing, preview, env_now) for f in dash_fields
        ) or (
            '<p class="muted">Nothing needed. The local Incident Command desk reads the copilot\'s own store; '
            "build results show up there as soon as this connection is live.</p>"
        )
        on = values.get("live") == "on"
        auto_on = values.get("auto_apply") == "on"
        name_field = (
            f'<div class="field"><label for="f-name">Connection name</label><input id="f-name" name="name" type="text" '
            f'value="{escape(values.get("name", ""), quote=True)}" placeholder="e.g. {escape(spec.label)} production"'
            f"{' disabled' if preview else ''}></div>"
        )
        env_note = (
            '<div class="banner info small">This connection still reads DATABRICKS_HOST and DATABRICKS_TOKEN from '
            ".env. Type the workspace URL and token below and save: they move into the Keychain and .env is no "
            "longer needed for Databricks.</div>"
            if env_now
            else ""
        )
        paid_note = (
            f'<div class="banner info">The {escape(spec.label)} adapter form is ready. Testing and saving need a paid '
            f"{escape(spec.label)} account, so the fields are shown read-only.</div>"
            if preview
            else ""
        )
        hidden = (
            f'<input type="hidden" name="form_key" value="{escape(self.form_key, quote=True)}">'
            f'<input type="hidden" name="platform" value="{escape(spec.platform, quote=True)}">'
            + (
                f'<input type="hidden" name="connection_id" value="{escape(existing.connection_id, quote=True)}">'
                if existing
                else ""
            )
        )
        note = f'<div class="banner {banner[0]}">{escape(banner[1])}</div>' if banner else ""
        steps = [("connect", "Connect"), ("jobs", "Jobs"), ("people", "Approvals & email")]
        steps += [("dashboards", "Dashboards"), ("review", "Review & save")]
        rail = "".join(
            f'<a href="#s-{key}" data-step="{key}"><span class="n">{i}</span>{label}</a>'
            for i, (key, label) in enumerate(steps, 1)
        )
        title = f"Edit {existing.name}" if existing else f"Connect {spec.label}"
        state, text = ("ok", "connected") if existing and existing.last_check_ok and on else ("idle", "not tested yet")
        if existing and not on:
            state, text = "paused", "switched off"
        disabled = " disabled" if preview else ""
        body = f"""<a class="small" href="/connections">← All platforms</a>
<div class="head" style="margin-top:10px"><div><div class="eyebrow">{escape(spec.category)} adapter</div><h1>{escape(title)}</h1>
<p class="sub">{escape(spec.description)}</p></div></div>
{note}{paid_note}
<style>{FORM_CSS}</style>
<form method="post" action="/connections/save" id="cform" class="layout" novalidate>{hidden}
<nav class="rail" aria-label="Steps">{rail}</nav>
<div class="steps">
<section class="card hero">{adapter(spec, state, text)}
<div class="hero-live"><div><b>Live</b><div class="small muted">On: the watcher polls these jobs and opens tickets.
Off: nothing is watched; history stays.</div></div>{switch("f-live", "live", on, "Live", preview)}</div></section>

<section class="card step" id="s-connect"><div class="s-head"><span class="n">1</span><div><h2>Connect</h2>
<p class="small muted">Secrets go to the macOS Keychain, never to the database, Grafana or .env.</p></div></div>
{env_note}{name_field}{connect}
<div class="row"><button type="button" class="btn" id="test"{disabled}>Test connection</button>
<span class="small muted" id="test-msg"></span></div></section>

<section class="card step" id="s-jobs"><div class="s-head"><span class="n">2</span><div><h2>{escape(spec.pipelines_label)}</h2>
<p class="small muted">{escape(spec.pipelines_help)} After a successful test, the jobs the credentials can see appear
here to tick.</p></div></div>
<div id="jobs-found" class="jobs"></div>
<div class="field"><label for="f-pipelines">Watched</label>
<textarea id="f-pipelines" name="pipelines" rows="3"{disabled}>{escape(values.get("pipelines", ""))}</textarea>
<span class="help">Each one shows up as an application on the dashboards.</span></div></section>

<section class="card step" id="s-people"><div class="s-head"><span class="n">3</span><div><h2>Approvals &amp; email</h2>
<p class="small muted">Who decides on a proposed fix, and who just needs to know.</p></div></div>
<div class="field"><label>Approvers</label>{chips("f-approvers", "approvers", split_emails(values.get("approvers")), "name@company.com, press Enter", preview)}
<span class="help">Each approver gets the "approval needed" email with their own Approve / Deny links, so the ticket
history records who decided.</span></div>
<div class="field"><label>Notify</label>{chips("f-notify", "notify", split_emails(values.get("notify")), "team@company.com", preview)}
<span class="help">Get updates when a ticket opens, is fixed or needs a person. No buttons.</span></div>
{self._email_status()}
<div class="auto"><div class="toggle-row"><div style="flex:1"><b>Auto-fix known failures</b>
<div class="small muted">Run a fix without waiting for approval when this exact fix already resolved this failure on
this pipeline.</div></div>{switch("f-auto", "auto_apply", auto_on, "Auto-fix", preview)}</div>
<div class="row" style="margin:14px 0 6px;align-items:flex-start">
<div class="field" style="margin:0;width:220px"><label for="f-auto-min">Minimum verified fixes</label>
<input id="f-auto-min" name="auto_apply_min_successes" type="number" min="1" max="20"
 value="{escape(values.get("auto_apply_min_successes") or "2", quote=True)}"{disabled}></div>
<div class="field" style="margin:0;width:260px"><label for="f-auto-cool">Cooldown per pipeline (hours)</label>
<input id="f-auto-cool" name="auto_apply_cooldown_hours" type="number" min="0" max="168"
 value="{escape(values.get("auto_apply_cooldown_hours") or "1", quote=True)}"{disabled}>
<span class="help">At most one auto-fix per pipeline in this time; 0 = no limit. A pipeline that keeps failing is
usually a sign of a deeper problem.</span></div></div>
<ul class="rules small muted"><li>the fix never failed before</li><li>confidence at least {AUTO_CONFIDENCE:.0%}</li>
<li>first attempt on this ticket</li><li>outside the pipeline's cooldown</li>
<li>tickets already waiting are re-checked every minute</li>
<li>the guardrail still checks every rerun</li></ul></div></section>

<section class="card step" id="s-dashboards"><div class="s-head"><span class="n">4</span><div><h2>Dashboards</h2>
<p class="small muted">What Grafana needs to chart this platform's runs. Grafana Cloud's address and token are in
<a href="/settings">Settings</a>.</p></div></div>{dashboards}</section>

<section class="card step" id="s-review"><div class="s-head"><span class="n">5</span><div><h2>Review &amp; save</h2>
<p class="small muted">Saving runs the connection test again; nothing is saved if it fails.</p></div></div>
<dl class="review" id="review"></dl>
<div class="row"><button class="btn primary" type="submit"{disabled}>{"Save changes" if existing else "Test and save"}</button>
<a class="btn" href="/connections">Cancel</a></div></section>
</div></form>"""
        return frame(title, body, "connections", FORM_JS)


GALLERY_CSS = """
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}
.tile{position:relative;display:grid;grid-template-columns:auto 1fr;grid-template-rows:auto auto 1fr auto;gap:4px 12px;
padding:16px;background:var(--surface);border:1px solid var(--line);border-radius:16px;color:var(--ink);
text-decoration:none;box-shadow:var(--shadow);transition:transform .18s ease,box-shadow .18s ease,border-color .18s;
animation:rise .4s ease-out both}
.tile .mark{grid-row:1/3} .t-body{display:grid;align-content:center} .t-body b{font-size:16px}
.tile .pill{grid-column:1/-1;justify-self:start;margin-top:8px} .t-desc{grid-column:1/-1;margin:4px 0 8px}
.t-cta{grid-column:1/-1;font-size:13px;font-weight:700;color:var(--brand)}
a.tile:hover,a.tile:focus-visible{transform:translateY(-3px);border-color:var(--brand);
box-shadow:0 0 0 3px color-mix(in srgb,var(--brand) 18%,transparent),var(--shadow)}
a.tile:hover .mark{animation:wiggle .5s ease}
.tile.coming{box-shadow:none;background:transparent;border-style:dashed}
.tile.coming .mark{filter:grayscale(1);opacity:.55} .tile.coming .t-cta{color:var(--muted)}
.tile.paid .t-cta{color:var(--warn)}
@keyframes wiggle{0%,100%{transform:none}30%{transform:rotate(-6deg) scale(1.06)}60%{transform:rotate(4deg) scale(1.06)}}
.conns{display:grid;gap:14px}
.conn{padding:16px 18px}
.c-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap} .c-title{display:grid;flex:1 1 220px} .c-title b{font-size:16px}
.c-meta{display:flex;flex-wrap:wrap;gap:6px 18px;margin:12px 0 2px} .c-check{margin:2px 0 8px}
.c-table{overflow-x:auto} table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{padding:7px 8px;border-top:1px solid var(--line);text-align:left} th{font-size:11px;letter-spacing:.06em;
text-transform:uppercase;color:var(--muted);font-weight:700} .num{text-align:right;font-variant-numeric:tabular-nums}
.c-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px} .c-actions form{margin:0}
"""

FORM_CSS = """
.layout{display:grid;grid-template-columns:200px 1fr;gap:24px;align-items:start}
.rail{position:sticky;top:76px;display:grid;gap:2px}
.rail a{display:flex;gap:10px;align-items:center;padding:8px 10px;border-radius:9px;color:var(--muted);text-decoration:none;
font-weight:600;font-size:14px} .rail a:hover{background:var(--sunken);color:var(--ink)}
.rail a.on{background:var(--accent-soft);color:var(--ink)}
.n{display:inline-grid;place-items:center;width:24px;height:24px;border-radius:50%;background:var(--sunken);font-size:12px;
font-weight:700;color:var(--muted);flex:none} .rail a.on .n,.s-head .n{background:var(--accent);color:var(--accent-ink)}
.rail a.done .n{background:var(--good);color:#fff}
.steps{display:grid;gap:16px;min-width:0}
.step{padding:20px 22px;scroll-margin-top:76px} .s-head{display:flex;gap:12px;align-items:flex-start;margin-bottom:16px}
.s-head p{margin:2px 0 0}
.hero{padding:6px 6px 0} .hero-live{display:flex;gap:16px;align-items:center;justify-content:space-between;
padding:14px 18px;border-top:1px solid var(--line)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.jobs{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px} .jobs:empty{display:none}
.job{display:inline-flex;align-items:center;gap:8px;padding:7px 12px;border:1px solid var(--line);border-radius:9px;
cursor:pointer;font-size:13.5px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.job:has(input:checked){border-color:var(--accent);background:var(--accent-soft)} .job.missing{border-color:var(--bad);color:var(--bad)}
.auto{border:1px solid var(--line);border-radius:12px;padding:14px 16px;background:var(--sunken)}
.rules{margin:6px 0 0;padding-left:18px;columns:2;column-gap:24px}
.review{display:grid;grid-template-columns:max-content 1fr;gap:8px 18px;margin:0 0 18px;font-size:14px}
.review dt{color:var(--muted)} .review dd{margin:0;overflow-wrap:anywhere}
@media (max-width:820px){.layout{grid-template-columns:1fr}.rail{position:static;display:flex;overflow-x:auto}
.rail a{white-space:nowrap}.rules{columns:1}}
"""

LIVE_JS = r"""
const KEY=__KEY__;
document.querySelectorAll('[data-live]').forEach(sw=>sw.addEventListener('click',async()=>{
  const on=sw.getAttribute('aria-checked')!=='true', card=sw.closest('.conn'), pill=card.querySelector('[data-status]');
  sw.disabled=true;
  try{const r=await fetch('/connections/'+encodeURIComponent(sw.dataset.live)+'/live',{method:'POST',
      body:new URLSearchParams({form_key:KEY,live:on?'on':'',ajax:'1'})});
    const d=await r.json();sw.setAttribute('aria-checked',d.live);
    pill.className='pill '+(d.live?'info':'off');pill.textContent=d.live?'On · next poll':'Off';}
  catch(e){alert('Could not switch it: '+e);} finally{sw.disabled=false;}
}));
"""

FORM_JS = r"""
const form=document.getElementById('cform'), ad=document.getElementById('adapter'), msg=document.getElementById('test-msg');
const jobs=document.getElementById('jobs-found'), area=document.getElementById('f-pipelines');
const watched=()=>area.value.split(/[\n,]/).map(s=>s.trim()).filter(Boolean);
function renderJobs(available,missing){
  jobs.innerHTML='';const have=new Set(watched());
  [...new Set([...available,...have])].forEach(name=>{const l=document.createElement('label');
    l.className='job'+(missing.includes(name)?' missing':'');const c=document.createElement('input');c.type='checkbox';
    c.checked=have.has(name);c.onchange=()=>{const w=new Set(watched());c.checked?w.add(name):w.delete(name);
      area.value=[...w].join('\n');review();};l.append(c,document.createTextNode(name+(missing.includes(name)?' · not found':'')));
    jobs.appendChild(l);});
}
const btn=document.getElementById('test');
btn&&btn.addEventListener('click',async()=>{
  setAdapter(ad,'testing','connecting…');msg.textContent='';btn.disabled=true;
  const started=Date.now();
  try{const d=await postForm('/connections/test',form);
    await new Promise(r=>setTimeout(r,Math.max(0,900-(Date.now()-started))));
    if(d.ok){setAdapter(ad,'ok','connected');msg.textContent=d.message;}
    else{setAdapter(ad,'fail','not connected');msg.textContent=d.message;}
    if(d.available&&d.available.length)renderJobs(d.available,d.missing||[]);}
  catch(e){setAdapter(ad,'fail','not connected');msg.textContent='The console did not answer: '+e;}
  finally{btn.disabled=false;}
});
document.getElementById('f-live').closest('.hero').querySelector('.switch').addEventListener('change',e=>{
  const on=e.target.getAttribute('aria-checked')==='true';
  if(ad.dataset.state==='paused'||ad.dataset.state==='ok'||!on)setAdapter(ad,on?'idle':'paused',on?'switched on · save to apply':'switched off');
});
function review(){
  const v=n=>(form.elements[n]&&form.elements[n].value||'').trim();
  const rows=[['Name',v('name')||'—'],['Live',v('live')==='on'?'on':'off'],
    ['Watched',watched().join(', ')||'—'],['Approvers',v('approvers')||'default recipients'],
    ['Notify',v('notify')||'—'],['Auto-fix',v('auto_apply')==='on'?'on, after '+v('auto_apply_min_successes')+' verified fixes, '+(+v('auto_apply_cooldown_hours')?v('auto_apply_cooldown_hours')+' h cooldown':'no cooldown'):'off']];
  document.getElementById('review').innerHTML=rows.map(([k,x])=>'<dt>'+k+'</dt><dd>'+x.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))+'</dd>').join('');
}
form.addEventListener('input',review);form.addEventListener('change',review);review();
const links=[...document.querySelectorAll('.rail a')];
const io=new IntersectionObserver(es=>es.forEach(e=>{if(e.isIntersecting){links.forEach(a=>a.classList.toggle('on',
  a.dataset.step===e.target.id.slice(2)));}}),{rootMargin:'-30% 0px -60% 0px'});
document.querySelectorAll('.step').forEach(s=>io.observe(s));
"""
