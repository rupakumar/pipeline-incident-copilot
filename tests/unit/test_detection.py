from types import SimpleNamespace as NS

from databricks.sdk.service.jobs import RunLifeCycleState, RunResultState

from pipeline_incident_copilot.connectors.databricks.connector import DatabricksConnector
from pipeline_incident_copilot.detection.failure_watcher import FailureWatcher
from pipeline_incident_copilot.notifications.base import EventKind, IncidentEvent
from pipeline_incident_copilot.notifications.templates import render
from pipeline_incident_copilot.storage.database import connect
from pipeline_incident_copilot.storage.incident_repository import IncidentRepository
from pipeline_incident_copilot.tools.run_history_tools import changed_parameters


def _task(key, result, start):
    return NS(task_key=key, run_id=start, start_time=start, state=NS(result_state=result))


def _run(run_id, result, failed_task=None):
    tasks = [_task("bronze_ingest", RunResultState.FAILED if failed_task else RunResultState.SUCCESS, 1)]
    return NS(
        run_id=run_id,
        run_page_url=f"https://x/run/{run_id}",
        start_time=run_id * 1000,
        end_time=None,
        state=NS(result_state=result, life_cycle_state=RunLifeCycleState.TERMINATED),
        tasks=tasks,
        job_parameters=[
            NS(name="source", value="spark", default="spark"),
            NS(name="run_date", value=None, default="d"),
        ],
    )


class FakeWorkspace:
    def __init__(self, runs):
        self.jobs = NS(list=lambda name: iter([NS(job_id=7)]), list_runs=lambda **kw: iter(runs))


def test_watcher_returns_only_new_failures_oldest_first(tmp_path):
    from datetime import UTC, datetime

    repo = IncidentRepository(connect(tmp_path / "t.db"))
    runs = [
        _run(3, RunResultState.FAILED, "bronze_ingest"),
        _run(2, RunResultState.SUCCESS),
        _run(1, RunResultState.FAILED, "bronze_ingest"),
    ]
    watcher = watcher_for(repo, lambda c: DatabricksConnector(c.connection_id, FakeWorkspace(runs), None, c.pipelines))
    found = list(watcher.poll(since=datetime(1970, 1, 1, tzinfo=UTC)))
    assert [r.run_id for r in found] == [1, 3]
    assert found[0].failed_task == "bronze_ingest" and found[0].parameters == {"source": "spark", "run_date": "d"}
    repo.open(found[0])
    assert [r.run_id for r in watcher.poll()] == [3]  # already an incident: skipped


def watcher_for(repo, make_connector, platform="databricks", pipelines=("loghub_etl",), cid="databricks-loghub"):
    """A FailureWatcher over one saved connection whose connector is built by `make_connector(connection)`."""
    from dataclasses import replace

    from pipeline_incident_copilot.connectors.catalog import PLATFORMS
    from pipeline_incident_copilot.connectors.hub import ConnectorHub
    from pipeline_incident_copilot.schemas.connection import Connection
    from pipeline_incident_copilot.security.secret_store import MemorySecretStore
    from pipeline_incident_copilot.storage.connection_repository import ConnectionRepository

    connections = ConnectionRepository(repo.conn)
    connections.save(Connection(connection_id=cid, name=cid, platform=platform, pipelines=list(pipelines)))
    platforms = {**PLATFORMS, platform: replace(PLATFORMS[platform], build=lambda c, s: make_connector(c))}
    return FailureWatcher(repo, connections, ConnectorHub(connections, MemorySecretStore(), platforms))


def test_changed_parameters_lists_only_differences():
    good = {"source": "spark", "run_date": "2017-06-09", "gold_table": ""}
    bad = {"source": "spark", "run_date": "2017-06-12", "gold_table": "", "input_path": "x"}
    assert changed_parameters(good, bad) == {"input_path": ("", "x"), "run_date": ("2017-06-09", "2017-06-12")}


def test_incident_opened_email_names_the_failed_task_and_run(tmp_path):
    repo = IncidentRepository(connect(tmp_path / "t.db"))
    inc = repo.open(FailedRun_from_watch())
    subject, body = render(IncidentEvent(kind=EventKind.INCIDENT_OPENED, incident=inc))
    assert subject == "[INC-0001] loghub_etl: bronze_ingest failed — investigating"
    assert "https://x/run/1" in body and "No action is needed" in body


def FailedRun_from_watch():  # noqa: N802 — small factory used above
    from pipeline_incident_copilot.connectors.databricks.connector import to_failed_run

    return to_failed_run(7, "loghub_etl", _run(1, RunResultState.FAILED, "bronze_ingest"))


def test_volume_path_is_taken_from_the_error_message():
    from pipeline_incident_copilot.connectors.databricks.evidence import volume_path_in

    path = "/Volumes/workspace/pipeline_demo/raw_logs/hdfs/2008-11-13/HDFS_2k.log"
    err = f"[PATH_NOT_FOUND] Path does not exist: dbfs:{path}. SQLSTATE: 42K03"
    assert volume_path_in(err) == path
    assert volume_path_in("[UNAUTHORIZED_ACCESS] Unauthorized access:") is None
