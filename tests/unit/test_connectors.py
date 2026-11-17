"""Plug-and-play connectors: store migration, multi-connection watcher, Jenkins mapping, connect form, routing."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

from pipeline_incident_copilot.connectors.base import PollResult
from pipeline_incident_copilot.connectors.catalog import PLATFORMS
from pipeline_incident_copilot.connectors.hub import ConnectorHub
from pipeline_incident_copilot.connectors.jenkins.client import JenkinsError
from pipeline_incident_copilot.connectors.jenkins.connector import JenkinsConnector, error_from_console, failed_step
from pipeline_incident_copilot.interface.connections_page import ConnectionsPage
from pipeline_incident_copilot.remediation.action_executor import ActionExecutor
from pipeline_incident_copilot.schemas.agent_outputs import ProposedAction
from pipeline_incident_copilot.schemas.connection import Connection, ConnectionTest
from pipeline_incident_copilot.schemas.incident import FailedRun
from pipeline_incident_copilot.schemas.remediation import RemediationResult
from pipeline_incident_copilot.security.secret_store import MemorySecretStore
from pipeline_incident_copilot.storage import database
from pipeline_incident_copilot.storage.connection_repository import ConnectionRepository
from pipeline_incident_copilot.storage.database import connect
from pipeline_incident_copilot.storage.incident_repository import IncidentRepository

T0 = 1_760_000_000_000  # ms
CONSOLE = """Started by user admin
Building in workspace /var/jenkins_home/workspace/loghub-daily-export
[loghub-daily-export] $ /bin/bash /tmp/jenkins1.sh
[export] source=spark input=/data/loghub/Spark_2k.log
[export] read 2000 rows
ERROR: DATA_QUALITY_FAILED: min_row_count expected >= 5000, got 2000
Build step 'Execute shell' marked build as failure
Finished: FAILURE
"""


class FakeJenkins:
    """Answers the JenkinsClient calls the connector makes, from a dict of builds per job."""

    def __init__(self, builds: dict[str, list[dict]], last_success: dict | None = None):
        self.builds = builds
        self.last_success = last_success
        self.queued: list[tuple[str, dict]] = []

    @staticmethod
    def job_path(name):
        return f"job/{name}"

    def whoami(self):
        return "admin"

    def job_names(self):
        return list(self.builds)

    def json(self, path, tree=None):
        parts = path.split("/")
        job = parts[1]
        if job not in self.builds:
            raise JenkinsError(f"not found on Jenkins: /{path}")
        if len(parts) == 2 and tree and tree.startswith("property"):
            return {
                "property": [
                    {"parameterDefinitions": [{"name": "MIN_ROWS", "defaultParameterValue": {"value": "1000"}}]}
                ]
            }
        if len(parts) == 2:
            return {"name": job, "builds": self.builds[job]}
        if parts[2] == "lastSuccessfulBuild":
            if self.last_success is None:
                raise JenkinsError("not found")
            return self.last_success
        number = int(parts[2])
        return next(b for b in self.builds[job] if b["number"] == number)

    def console(self, job, number):
        return CONSOLE

    def build_with_parameters(self, job, params):
        self.queued.append((job, params))
        return "http://j/queue/item/9/"

    def wait_for_queue(self, url):
        return {"number": 13, "url": "http://j/job/loghub-daily-export/13/"}


def build(number, result, minutes=0, params=None):
    return {
        "number": number,
        "result": result,
        "building": False,
        "timestamp": T0 + minutes * 60_000,
        "duration": 4000,
        "url": f"http://j/job/loghub-daily-export/{number}/",
        "actions": [{"parameters": [{"name": k, "value": v} for k, v in (params or {}).items()]}, {}],
    }


def jenkins(fake: FakeJenkins) -> JenkinsConnector:
    return JenkinsConnector("jenkins-local", fake, ["loghub-daily-export"])


# --- storage -----------------------------------------------------------------------------------------
def test_v0_store_is_migrated_in_place_with_a_backup(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """CREATE TABLE incidents (seq INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT NOT NULL UNIQUE,
        run_id INTEGER NOT NULL UNIQUE, job_id INTEGER NOT NULL, status TEXT NOT NULL, failed_task TEXT, source TEXT,
        run_date TEXT, opened_at TEXT NOT NULL, updated_at TEXT NOT NULL, incident_json TEXT NOT NULL);
        CREATE TABLE incident_events (id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT NOT NULL
        REFERENCES incidents (incident_id), at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT);
        CREATE TABLE watcher_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO watcher_state VALUES ('watch_from:loghub_etl', '2026-10-04T10:40:00+00:00');"""
    )
    run = FailedRun(job_id=7, job_name="loghub_etl", run_id=55, result_state="FAILED")
    old.execute(
        "INSERT INTO incidents (incident_id, run_id, job_id, status, opened_at, updated_at, incident_json)"
        " VALUES ('INC-0001', 55, 7, 'resolved', 'x', 'x', ?)",
        ('{"incident_id": "INC-0001", "run": ' + run.model_dump_json(exclude={"connection_id", "platform"}) + "}",),
    )
    old.execute("INSERT INTO incident_events (incident_id, at, kind) VALUES ('INC-0001', 'x', 'opened')")
    old.commit()
    old.close()
    monkeypatch.setattr(database, "BACKUP_DIR", tmp_path / "backups")

    repo = IncidentRepository(connect(path))
    row = repo.conn.execute("SELECT connection_id, platform, pipeline, run_key FROM incidents").fetchone()
    assert tuple(row) == ("databricks-loghub", "databricks", "loghub_etl", "databricks-loghub:7:55")
    assert repo.get("INC-0001").run.connection_id == "databricks-loghub"
    assert repo.get_state("watch_from:databricks-loghub") == "2026-10-04T10:40:00+00:00"
    assert repo.events("INC-0001")[0]["kind"] == "opened"  # foreign keys still point at incidents
    assert len(list((tmp_path / "backups").glob("old-v0-*.db"))) == 1
    # the same build number on another connection is a different incident
    jenkins_run = run.model_copy(update={"connection_id": "jenkins-local", "platform": "jenkins"})
    assert not repo.exists_for_run(jenkins_run) and repo.exists_for_run(run)
    connect(path)  # second open: already current, no second backup
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


# --- watcher -------------------------------------------------------------------------------------------
class StubConnector:
    def __init__(self, cid, runs=(), broken=False):
        self.cid, self.runs, self.broken = cid, list(runs), broken

    def poll(self, since, lookback):
        if self.broken:
            raise JenkinsError("Jenkins rejected the user name or API token (HTTP 401)")
        return PollResult(failed_runs=self.runs, health=[])

    def complete(self, run):
        return run.model_copy(update={"failed_task": "step"})


def test_watcher_checks_every_connection_and_survives_a_broken_one(tmp_path):
    repo = IncidentRepository(connect(tmp_path / "t.db"))
    connections = ConnectionRepository(repo.conn)
    for cid, platform in (("broken", "jenkins"), ("jenkins-local", "jenkins")):
        connections.save(Connection(connection_id=cid, name=cid, platform=platform, pipelines=["a"]))
    run = FailedRun(
        connection_id="jenkins-local", platform="jenkins", job_id="a", job_name="a", run_id=5, result_state="FAILURE"
    )
    stubs = {"broken": StubConnector("broken", broken=True), "jenkins-local": StubConnector("jenkins-local", [run])}
    platforms = {**PLATFORMS, "jenkins": replace(PLATFORMS["jenkins"], build=lambda c, s: stubs[c.connection_id])}
    from pipeline_incident_copilot.detection.failure_watcher import FailureWatcher

    watcher = FailureWatcher(repo, connections, ConnectorHub(connections, MemorySecretStore(), platforms))
    found = list(watcher.poll(since=datetime(1970, 1, 1, tzinfo=UTC)))
    assert [(r.connection_id, r.run_id, r.failed_task) for r in found] == [("jenkins-local", 5, "step")]
    broken = connections.get("broken")
    assert broken.last_check_ok is False and "HTTP 401" in broken.last_check_message
    assert connections.get("jenkins-local").last_check_ok is True
    repo.open(found[0])
    assert list(watcher.poll()) == []


def test_watcher_reports_an_outage_once_and_the_recovery(tmp_path, capsys):
    from pipeline_incident_copilot.detection.failure_watcher import FailureWatcher

    repo = IncidentRepository(connect(tmp_path / "t.db"))
    connections = ConnectionRepository(repo.conn)
    connections.save(Connection(connection_id="j", name="j", platform="jenkins", pipelines=["a"]))
    stub = StubConnector("j", broken=True)
    platforms = {**PLATFORMS, "jenkins": replace(PLATFORMS["jenkins"], build=lambda c, s: stub)}
    watcher = FailureWatcher(repo, connections, ConnectorHub(connections, MemorySecretStore(), platforms))
    for _ in range(3):
        list(watcher.poll())
    stub.broken = False
    list(watcher.poll())
    out = capsys.readouterr().out
    assert out.count("check failed") == 1 and out.count("reachable again") == 1


# --- Jenkins connector ---------------------------------------------------------------------------------
def test_jenkins_poll_maps_failed_builds_and_health():
    builds = [
        build(12, "FAILURE", 30, {"SOURCE": "spark", "MIN_ROWS": "5000"}),
        build(11, "SUCCESS", 10),
        build(10, "ABORTED"),
    ]
    result = jenkins(FakeJenkins({"loghub-daily-export": builds})).poll(datetime(1970, 1, 1, tzinfo=UTC), 20)
    (run,) = result.failed_runs  # ABORTED is not an incident
    assert (run.platform, run.job_id, run.run_id, run.result_state) == ("jenkins", "loghub-daily-export", 12, "FAILURE")
    assert run.parameters == {"SOURCE": "spark", "MIN_ROWS": "5000"}
    assert run.run_key == "jenkins-local:loghub-daily-export:12"
    (h,) = result.health
    assert (h.last_run_id, h.last_state) == ("12", "FAILURE")


def test_jenkins_console_gives_error_and_failed_step():
    error, trace = error_from_console(CONSOLE)
    assert error == "ERROR: DATA_QUALITY_FAILED: min_row_count expected >= 5000, got 2000"
    assert failed_step(CONSOLE) == "build step 'Execute shell'"
    assert failed_step("[Pipeline] { (Checkout)\n[Pipeline] { (Export)\nERROR: x") == "Export"
    assert trace.endswith("Finished: FAILURE")


def test_jenkins_evidence_compares_with_the_last_successful_build(tmp_path):
    failed = build(12, "FAILURE", 30, {"SOURCE": "spark", "MIN_ROWS": "5000"})
    good = build(11, "SUCCESS", 10, {"SOURCE": "spark", "MIN_ROWS": "1000"})
    connector = jenkins(FakeJenkins({"loghub-daily-export": [failed, good]}, last_success=good))
    repo = IncidentRepository(connect(tmp_path / "t.db"))
    incident = repo.open(connector.to_failed_run("loghub-daily-export", failed))
    ev = connector.collect_evidence(incident)
    assert ev.failed_task == "build step 'Execute shell'" and "DATA_QUALITY_FAILED" in ev.error
    assert ev.comparison.changed_parameters == {"MIN_ROWS": ("1000", "5000")}
    assert ev.tool_results[0].summary.startswith("last success: build #11")


def test_executor_reruns_on_the_incidents_platform_with_defaults_from_it(tmp_path):
    failed = build(12, "FAILURE", 30, {"SOURCE": "spark", "MIN_ROWS": "5000"})
    fake = FakeJenkins({"loghub-daily-export": [failed]})
    connector = jenkins(fake)
    repo = IncidentRepository(connect(tmp_path / "t.db"))
    incident = repo.open(connector.to_failed_run("loghub-daily-export", failed))
    hub = ConnectorHub(ConnectionRepository(repo.conn), MemorySecretStore())
    hub.get = lambda connection_id: connector  # the live connector for the incident's connection
    action = ProposedAction(name="rerun_job", parameters={"MIN_ROWS": ""}, reason="undo the change")
    result = ActionExecutor(hub).execute(
        incident, action, incident.run.parameters, {"SOURCE": "spark", "MIN_ROWS": "1000"}
    )
    assert result.refused is None, result.refused
    assert fake.queued == [("loghub-daily-export", {"SOURCE": "spark"})]  # MIN_ROWS back to its default
    assert (result.connection_id, result.job_id, result.rerun_run_id) == ("jenkins-local", "loghub-daily-export", 13)
    assert isinstance(result, RemediationResult)


# --- connect form ----------------------------------------------------------------------------------------
class AlwaysTest:
    def __init__(self, ok):
        self.ok = ok

    def test(self):
        return ConnectionTest(ok=self.ok, message="signed in as admin; 1 of 1 job(s) found" if self.ok else "HTTP 401")


def form_page(tmp_path, ok=True):
    repo = IncidentRepository(connect(tmp_path / "t.db"))
    connections, secrets = ConnectionRepository(repo.conn), MemorySecretStore()
    platforms = {**PLATFORMS, "jenkins": replace(PLATFORMS["jenkins"], build=lambda c, s: AlwaysTest(ok))}
    hub = ConnectorHub(connections, secrets, platforms)
    page = ConnectionsPage(connections, secrets, hub, "key-1", lambda title, body, color: f"<h1>{title}</h1>{body}")
    return page, connections, secrets


FORM = {
    "form_key": ["key-1"],
    "platform": ["jenkins"],
    "action": ["save"],
    "name": ["Local Jenkins"],
    "url": ["http://localhost:8080/"],
    "username": ["admin"],
    "api_token": ["s3cret-token"],
    "pipelines": ["loghub-daily-export\nloghub-report-publish"],
}


def test_connect_form_saves_after_a_passing_test_and_keeps_the_secret_out_of_sqlite(tmp_path):
    page, connections, secrets = form_page(tmp_path)
    status, location = page.post("/connections/save", FORM)
    assert (status, location) == (303, "/connections?saved=local-jenkins")
    c = connections.get("local-jenkins")
    assert c.settings == {"url": "http://localhost:8080", "username": "admin"}
    assert c.pipelines == ["loghub-daily-export", "loghub-report-publish"] and c.last_check_ok
    assert secrets.get(c.connection_id, "api_token") == "s3cret-token"
    dump = "\n".join(connections.conn.iterdump())
    assert "s3cret-token" not in dump
    # edit with an empty token keeps the stored one; the page never shows it
    status, html = page.get(f"/connections/{c.connection_id}/edit", {})
    assert status == 200 and "s3cret-token" not in html and "saved in the Keychain" in html
    edit = {**FORM, "connection_id": [c.connection_id], "api_token": [""], "name": ["Jenkins (local)"]}
    assert page.post("/connections/save", edit)[0] == 303
    assert connections.get(c.connection_id).name == "Jenkins (local)"
    assert secrets.get(c.connection_id, "api_token") == "s3cret-token"


def test_connect_form_refuses_a_failed_test_a_missing_key_and_never_echoes_the_token(tmp_path):
    page, connections, _ = form_page(tmp_path, ok=False)
    status, html = page.post("/connections/save", FORM)
    assert status == 200 and "Test failed, nothing saved" in html and "s3cret-token" not in html
    assert connections.all() == []
    assert page.post("/connections/save", {**FORM, "form_key": ["forged"]})[0] == 403
    status, html = page.post("/connections/save", {**FORM, "url": ["ftp://x"], "form_key": ["key-1"]})
    assert status == 400 and "must start with http" in html


def test_pipelines_without_value_rules_may_only_undo_changes():
    from pipeline_incident_copilot.guardrails.action import safe_actions

    failed = {"SOURCE": "spark", "MIN_ROWS": "5000"}
    good = {"SOURCE": "spark", "MIN_ROWS": "1000"}

    def check(params, last_good=good):
        action = ProposedAction(name="rerun_job", parameters=params, reason="r")
        return safe_actions.check(action, failed, last_good, rules=None)

    assert check({"MIN_ROWS": "1000"}) is None  # back to the last working value
    assert check({"MIN_ROWS": ""}) is None  # back to the job default
    assert "new value" in check({"MIN_ROWS": "10"})
    assert "no earlier successful run" in check({"MIN_ROWS": ""}, last_good=None)
    incident = type(
        "I", (), {"run": FailedRun(platform="jenkins", job_id="x", job_name="x", run_id=1, result_state="F")}
    )
    assert safe_actions.rules_for(incident) is None and "Never invent" in safe_actions.description_for(incident)


def test_console_accepts_its_own_posts_and_refuses_other_origins_and_hosts():
    import threading
    from http.server import HTTPServer

    import requests

    from pipeline_incident_copilot.interface.approval_server import make_handler

    class Service:
        def decide(self, token, note):
            return 200, "decided"

    server = HTTPServer(("127.0.0.1", 0), make_handler(Service(), None, set()))
    port = server.server_address[1]
    server.RequestHandlerClass = make_handler(Service(), None, {f"127.0.0.1:{port}", f"localhost:{port}"})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/decide"
    try:
        own = requests.post(url, data={"token": "t"}, headers={"Origin": f"http://localhost:{port}"})
        assert own.status_code == 200
        assert own.headers["Referrer-Policy"] == "same-origin"  # never no-referrer: that makes Origin "null"
        assert requests.post(url, data={"token": "t"}, headers={"Origin": "https://evil.example"}).status_code == 403
        assert requests.post(url, data={"token": "t"}, headers={"Host": "evil.example"}).status_code == 421
    finally:
        server.shutdown()
