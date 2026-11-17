"""Settings from the console (not .env), per-connection approvers, the Live switch and confidence in the email."""

import json

from pipeline_incident_copilot.connectors.hub import ConnectorHub
from pipeline_incident_copilot.interface.approval_server import ApprovalService
from pipeline_incident_copilot.interface.connections_page import ConnectionsPage
from pipeline_incident_copilot.interface.settings_page import SettingsPage
from pipeline_incident_copilot.notifications.approval_links import ApprovalLinks
from pipeline_incident_copilot.notifications.email_notifier import EmailNotifier
from pipeline_incident_copilot.notifications.html_templates import render_html
from pipeline_incident_copilot.schemas.connection import Connection, ConnectionTest
from pipeline_incident_copilot.security.app_settings import AppSettings
from pipeline_incident_copilot.security.secret_store import MemorySecretStore
from pipeline_incident_copilot.storage.connection_repository import ConnectionRepository
from tests.unit.test_email_approval import _event, _incident

LINKS = ApprovalLinks(b"test-secret", "http://localhost:8787", ttl=600)
CONF = {
    "model": 0.95,
    "calibrated": 0.83,
    "signals": {
        "explained_by_change": True,
        "confirmed_by_check": True,
        "exact_error_doc": False,
        "knowledge_ok": True,
    },
}


class FakeSmtp:
    sent: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        self.account = (user, password)

    def send_message(self, msg):
        FakeSmtp.sent.append(msg)


def settings(repo, monkeypatch, **env):
    for k in ("GMAIL_SMTP_USER", "GMAIL_APP_PASSWORD", "NOTIFY_EMAIL_TO", "GRAFANA_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return AppSettings(repo.conn, MemorySecretStore())


def test_console_settings_win_over_env_and_secrets_stay_out_of_sqlite(tmp_path, monkeypatch):
    repo, _ = _incident(tmp_path)
    s = settings(repo, monkeypatch, GMAIL_SMTP_USER="old@gmail.com", GMAIL_APP_PASSWORD="envpass")
    assert (s.get("GMAIL_SMTP_USER"), s.source("GMAIL_SMTP_USER")) == ("old@gmail.com", ".env")
    s.save(
        {
            "GMAIL_SMTP_USER": "robot@gmail.com",
            "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop",
            "NOTIFY_EMAIL_TO": "a@x.io; b@x.io",
        }
    )
    assert (s.get("GMAIL_SMTP_USER"), s.source("GMAIL_SMTP_USER")) == ("robot@gmail.com", "console")
    assert s.get("GMAIL_APP_PASSWORD") == "abcdefghijklmnop" and s.get("NOTIFY_EMAIL_TO") == "a@x.io, b@x.io"
    s.save({"GMAIL_APP_PASSWORD": ""})  # an empty secret keeps the stored one
    assert s.get("GMAIL_APP_PASSWORD") == "abcdefghijklmnop"
    assert "abcdefghijklmnop" not in str(repo.conn.execute("SELECT * FROM app_settings").fetchall())


def test_each_approver_gets_their_own_links_and_notify_gets_no_buttons(tmp_path, monkeypatch):
    repo, inc = _incident(tmp_path)
    s = settings(repo, monkeypatch, GMAIL_SMTP_USER="robot@gmail.com", GMAIL_APP_PASSWORD="pw")
    connections = ConnectionRepository(repo.conn)
    connections.save(
        Connection(
            connection_id=inc.run.connection_id, name="x", platform="databricks", pipelines=["loghub_etl"],
            approvers=["anna@x.io", "ben@x.io"], notify=["team@x.io"],
        )
    )  # fmt: skip
    FakeSmtp.sent = []
    notifier = EmailNotifier(s, connections, LINKS, smtp=FakeSmtp)
    event = _event(inc).model_copy(update={"confidence": CONF})
    assert notifier.send(event)
    to = [m["To"] for m in FakeSmtp.sent]
    assert to == ["anna@x.io", "ben@x.io", "team@x.io"]
    html = [m.get_body(("html",)).get_content() for m in FakeSmtp.sent]
    assert "Approve fix" in html[0] and "Approve fix" not in html[2]
    token = html[0].split("token=")[1].split('"')[0]
    from urllib.parse import unquote

    assert LINKS.verify(unquote(token))["who"] == "anna@x.io"
    assert "83%" in html[0] and "Medium confidence" in html[0]  # the fix's confidence and its band
    assert "83%" in FakeSmtp.sent[0].get_body(("plain",)).get_content()


def test_a_connection_without_lists_uses_the_default_recipients(tmp_path, monkeypatch):
    repo, inc = _incident(tmp_path)
    s = settings(
        repo, monkeypatch, GMAIL_SMTP_USER="robot@gmail.com", GMAIL_APP_PASSWORD="pw", NOTIFY_EMAIL_TO="oncall@x.io"
    )
    notifier = EmailNotifier(s, ConnectionRepository(repo.conn), LINKS, smtp=FakeSmtp)
    assert notifier.routes(_event(inc)) == [(["oncall@x.io"], "oncall@x.io")]
    unset = EmailNotifier(settings(repo, monkeypatch), None, LINKS, smtp=FakeSmtp)
    FakeSmtp.sent = []
    assert unset.send(_event(inc)) and FakeSmtp.sent == []  # not set up: printed, nothing sent


def test_the_approvers_name_goes_into_the_history(tmp_path):
    repo, inc = _incident(tmp_path)
    decided = []
    service = ApprovalService(repo, LINKS, lambda i: _event(inc), lambda i, d: decided.append(d))
    service.decide(LINKS.token(inc.incident_id, "approve", who="anna@x.io"), "")
    assert decided[0].by == "anna@x.io (email link)"


def test_confidence_block_names_the_band_and_the_known_fix():
    import tempfile
    from pathlib import Path

    from tests.unit.test_email_approval import _incident as make

    _, inc = make(Path(tempfile.mkdtemp()))
    auto = {"approved": False, "reason": "confidence 0.83 is below 0.85", "memory_id": 4,
            "from_incidents": ["INC-0023", "INC-0034"], "successes": 3}  # fmt: skip
    html = render_html(_event(inc).model_copy(update={"confidence": CONF, "auto_approval": auto}), "a", "d")
    assert "Confidence in this fix" in html and "worked 3× before" in html and "below 0.85" in html
    assert "✗" in html and "documentation was found" in html


class FakeConnector:
    def __init__(self, ok=True):
        self.ok = ok

    def test(self):
        return ConnectionTest(ok=self.ok, message="ok" if self.ok else "401", available=["loghub_etl", "nightly"])


def page(tmp_path, ok=True):
    from pipeline_incident_copilot.storage.database import connect

    conn = connect(tmp_path / "c.db")
    connections, secrets = ConnectionRepository(conn), MemorySecretStore()
    hub = ConnectorHub(connections, secrets)
    hub.build = lambda connection, s: FakeConnector(ok)
    return ConnectionsPage(connections, secrets, hub, "key"), connections, secrets


def form(**kw):
    base = {"form_key": "key", "platform": "databricks", "name": "Prod", "host": "https://dbc.example.com",
            "token": "dapi-secret", "pipelines": "loghub_etl", "live": "on"}  # fmt: skip
    return {k: [v] for k, v in {**base, **kw}.items()}


def test_the_form_saves_approvers_and_the_live_switch_and_can_switch_off(tmp_path):
    p, connections, secrets = page(tmp_path)
    status, where = p.post("/connections/save", form(approvers="anna@x.io, ben@x.io", notify="team@x.io"))
    assert status == 303
    c = connections.get("databricks-prod")
    assert c.approvers == ["anna@x.io", "ben@x.io"] and c.notify == ["team@x.io"] and c.enabled
    assert secrets.get("databricks-prod", "token") == "dapi-secret"
    status, body, kind = p.post("/connections/databricks-prod/live", {"form_key": ["key"], "live": [""], "ajax": ["1"]})
    assert json.loads(body) == {"live": False} and not connections.get("databricks-prod").enabled


def test_test_button_answers_json_with_the_jobs_it_can_see_and_saves_nothing(tmp_path):
    p, connections, _ = page(tmp_path)
    status, body, kind = p.post("/connections/test", form(name="", pipelines=""))
    assert kind == "application/json" and json.loads(body)["available"] == ["loghub_etl", "nightly"]
    assert connections.all() == []
    assert p.post("/connections/save", form(approvers="not-an-email"))[0] == 400


def test_env_backed_databricks_moves_into_the_keychain_when_typed_in(tmp_path):
    p, connections, secrets = page(tmp_path)
    connections.save(
        Connection(connection_id="databricks-loghub", name="LogHub", platform="databricks",
                   settings={"credentials": "env"}, pipelines=["loghub_etl"])
    )  # fmt: skip
    edit = {"connection_id": "databricks-loghub", "name": "LogHub"}
    p.post("/connections/save", form(host="", token="", **edit))  # blank: stays on .env
    assert connections.get("databricks-loghub").settings == {"credentials": "env"}
    p.post("/connections/save", form(**edit))
    c = connections.get("databricks-loghub")
    assert (
        c.settings["host"] == "https://dbc.example.com" and secrets.get("databricks-loghub", "token") == "dapi-secret"
    )


def test_gallery_lists_live_paid_and_coming_platforms_and_paid_forms_are_read_only(tmp_path):
    p, _, _ = page(tmp_path)
    status, html, *_ = p.get("/connections", {})
    assert "Databricks" in html and "Needs paid account" in html and "Apache Airflow" in html
    status, html = p.get("/connections/new", {"platform": ["snowflake"]})
    assert status == 200 and "read-only" in html and 'name="account"' in html and " disabled" in html
    assert p.get("/connections/new", {"platform": ["airflow"]})[0] == 404


def test_settings_page_saves_and_refuses_a_bad_address(tmp_path, monkeypatch):
    repo, _ = _incident(tmp_path)
    s = settings(repo, monkeypatch)
    sp = SettingsPage(s, EmailNotifier(s, None, LINKS, smtp=FakeSmtp), "key")
    assert sp.post("/settings/save", {"form_key": ["key"], "GMAIL_SMTP_USER": ["nope"]})[0] == 400
    assert sp.post("/settings/save", {"form_key": ["key"], "GMAIL_SMTP_USER": ["r@gmail.com"]})[0] == 303
    assert s.get("GMAIL_SMTP_USER") == "r@gmail.com"
    assert json.loads(sp.post("/settings/test-email", {"form_key": ["key"]})[1])["ok"] is False  # no app password yet


def test_import_env_moves_values_after_a_passing_test_and_prints_no_secrets(tmp_path, monkeypatch):
    from pipeline_incident_copilot.security.env_import import import_env

    repo, _ = _incident(tmp_path)
    s = settings(repo, monkeypatch, GMAIL_SMTP_USER="robot@gmail.com", GMAIL_APP_PASSWORD="pw-secret")
    monkeypatch.setenv("DATABRICKS_HOST", "https://dbc.example.com/")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-secret")
    connections, secrets = ConnectionRepository(repo.conn), MemorySecretStore()
    connections.save(
        Connection(connection_id="databricks-loghub", name="LogHub", platform="databricks",
                   settings={"credentials": "env"}, pipelines=["loghub_etl"], auto_apply=True)
    )  # fmt: skip
    failing = import_env(s, connections, secrets, lambda h, t: ConnectionTest(ok=False, message="401"))
    assert connections.get("databricks-loghub").settings == {"credentials": "env"} and "failed" in failing[-1]
    lines = import_env(s, connections, secrets, lambda h, t: ConnectionTest(ok=True, message="ok"))
    c = connections.get("databricks-loghub")
    assert (
        c.settings == {"host": "https://dbc.example.com"} and secrets.get("databricks-loghub", "token") == "dapi-secret"
    )
    assert c.auto_apply and s.source("GMAIL_APP_PASSWORD") == "console"
    assert "secret" not in "\n".join(lines)


def test_only_the_first_decision_on_a_ticket_counts(tmp_path):
    from pipeline_incident_copilot.schemas.incident import IncidentStatus

    repo, inc = _incident(tmp_path)
    started = []
    service = ApprovalService(repo, LINKS, lambda i: _event(inc), lambda i, d: started.append(d.by))
    service.decide(LINKS.token(inc.incident_id, "approve", who="anna@x.io"), "")
    status, html = service.decide(LINKS.token(inc.incident_id, "reject", who="ben@x.io"), "")
    assert status == 409 and "approve by anna@x.io (email link)" in html and started == ["anna@x.io (email link)"]
    assert repo.claim_decision(inc.incident_id, "anna@x.io (email link)", "approve") is None  # her worker's retry
    assert "anna@x.io" in repo.claim_decision(inc.incident_id, "copilot (auto-apply)", "approve")  # watcher loses
    # a failed rerun was diagnosed again: the new pause is a new decision
    repo.set_status(inc.incident_id, IncidentStatus.REMEDIATING, "rerun")
    repo.set_status(inc.incident_id, IncidentStatus.AWAITING_APPROVAL, "second fix")
    assert repo.claim_decision(inc.incident_id, "ben@x.io (email link)", "approve") is None
