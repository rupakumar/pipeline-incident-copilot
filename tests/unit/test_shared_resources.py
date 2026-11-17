from pipeline_incident_copilot.rag.vector_store import qdrant_store
from pipeline_incident_copilot.schemas.evidence import EvidenceBundle
from pipeline_incident_copilot.schemas.incident import FailedRun, IncidentStatus
from pipeline_incident_copilot.storage.database import connect
from pipeline_incident_copilot.storage.incident_repository import IncidentRepository
from pipeline_incident_copilot.workflow.diagnoser import IncidentDiagnoser


class FakeClient:
    opened = 0

    def __init__(self, path):
        FakeClient.opened += 1
        if FakeClient.opened == 1:  # the first try finds the folder held by another process
            raise RuntimeError("Storage folder x is already accessed by another instance of Qdrant client.")
        self.closed = False

    def close(self):
        self.closed = True


def test_qdrant_waits_for_a_busy_folder_and_releases_its_lock(monkeypatch):
    monkeypatch.setattr(qdrant_store, "QdrantClient", FakeClient)
    monkeypatch.setattr(qdrant_store, "LOCK_RETRY_SECONDS", 0)
    qdrant_store.get_qdrant.cache_clear()
    client = qdrant_store.get_qdrant()
    assert FakeClient.opened == 2 and qdrant_store.get_qdrant() is client  # waited once, then one shared client
    qdrant_store.release_qdrant()
    assert client.closed and qdrant_store.get_qdrant.cache_info().currsize == 0
    qdrant_store.release_qdrant()  # nothing open: no error


def test_batch_jobs_get_a_private_copy_of_the_knowledge_base_without_the_live_lock(tmp_path):
    live = tmp_path / "qdrant"
    (live / "collection" / "kb_structure").mkdir(parents=True)
    (live / "collection" / "kb_structure" / "storage.sqlite").write_text("points")
    (live / ".lock").write_text("held by the watcher")
    copy = qdrant_store.snapshot_store(tmp_path / "eval_qdrant", source=live)
    assert (copy / "collection" / "kb_structure" / "storage.sqlite").read_text() == "points"
    assert not (copy / ".lock").exists()  # the eval never shares (or steals) the live lock
    (copy / "stale.txt").write_text("from last night")
    assert not (qdrant_store.snapshot_store(copy, source=live) / "stale.txt").exists()  # fresh every run


class CrashingGraph:
    def invoke(self, *args, **kwargs):
        raise RuntimeError("model server unreachable")


class Notifier:
    def __init__(self):
        self.sent = []

    def send(self, event):
        self.sent.append(event.kind)
        return True


def test_a_crashing_diagnosis_escalates_the_incident_instead_of_stopping_the_watcher(tmp_path):
    repo = IncidentRepository(connect(tmp_path / "t.db"))
    inc = repo.open(FailedRun(job_id=1, job_name="loghub_etl", run_id=5, result_state="FAILED", parameters={}))
    repo.save_evidence(
        EvidenceBundle(
            incident_id=inc.incident_id,
            failed_task="t",
            error="e",
            error_trace_chars=0,
            distilled_log="e",
            parameters={},
        )
    )
    notifier = Notifier()
    diagnoser = IncidentDiagnoser(repo, CrashingGraph(), notifier, unload_after=False)
    assert diagnoser.diagnose_or_escalate(inc.incident_id) is None
    assert repo.get(inc.incident_id).status is IncidentStatus.ESCALATED and notifier.sent == ["escalated"]
    assert any("model server unreachable" in (e["detail"] or "") for e in repo.events(inc.incident_id))
