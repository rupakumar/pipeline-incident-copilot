"""IncidentMemory: turn a verified fix into a searchable memory, and keep each memory's track record.

- one memory per failure pattern (error code · failed task · which parameters changed); a repeat
  updates it ("seen 2×") instead of adding a near-duplicate. Only changed parameters that can cause the failure
  class count (guardrail CAUSES), so an unrelated change in the same run doesn't split one lesson in two
- stored in SQLite (MemoryRepository) and in the Qdrant collection `incident_memory` — separate from
  the knowledge collection, which `build_index` rebuilds from scratch
- track record: each time a memory is cited in a diagnosis whose fix then works / fails, it is
  counted; a memory whose fixes failed more often than they worked is no longer offered (trusted=False)
"""

from qdrant_client import models

from pipeline_incident_copilot.guardrails.action.safe_actions import causes_for, causes_for_job
from pipeline_incident_copilot.rag.embedding.bge_dense_embedder import get_embeddings
from pipeline_incident_copilot.rag.embedding.bm25_sparse_encoder import doc_vector, tokenize
from pipeline_incident_copilot.rag.retrieval.routes.error_code_route import error_codes_in
from pipeline_incident_copilot.rag.retrieval.routes.memory_route import memory_collection
from pipeline_incident_copilot.rag.vector_store.qdrant_store import get_qdrant
from pipeline_incident_copilot.schemas.triage_state import TriageState
from pipeline_incident_copilot.storage.memory_repository import MemoryRepository

DENSE_DIM = 384  # bge-small-en-v1.5
AVG_DOC_TOKENS = 90.0  # BM25 length normalisation for memory documents (they are short and similar in size)


def ensure_collection(name: str) -> None:
    client = get_qdrant()
    if not client.collection_exists(name):
        client.create_collection(
            name,
            vectors_config={"dense": models.VectorParams(size=DENSE_DIM, distance=models.Distance.COSINE)},
            sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )


def relevant_changes(changed, failure_class: str | None, causes: dict | None) -> list[str]:
    """The changed parameters that can cause this failure class. All of them when the job has no cause map, the
    class is unknown, or none of the changes is a known cause (then nothing is hidden)."""
    known = (causes or {}).get(failure_class or "")
    picked = [k for k in changed if k in known] if known else []
    return sorted(picked or changed)


def pattern_of(state: TriageState) -> str:
    ev = state["evidence"]
    codes = error_codes_in(ev.error or "")
    rc, inc = state.get("root_cause"), state.get("incident")
    changed = relevant_changes(
        (ev.comparison.changed_parameters if ev.comparison else {}).keys(),
        rc.failure_class if rc else None,
        causes_for(inc) if inc else None,
    )
    return "|".join([codes[0] if codes else "-", ev.failed_task or "-", ",".join(changed) or "-"])


def narrowed_pattern(memory: dict, platform: str | None) -> str:
    """A stored memory's pattern under the same rule as pattern_of (for memories saved before the rule)."""
    code, task, changed = memory["pattern"].split("|", 2)
    job = (memory.get("pipeline_key") or "").split(":", 1)[-1]
    keys = relevant_changes(
        [c for c in changed.split(",") if c != "-"], memory["failure_class"], causes_for_job(platform, job)
    )
    return "|".join([code, task, ",".join(keys) or "-"])


def pipeline_key_of(state: TriageState) -> str:
    run = state["incident"].run
    return f"{run.connection_id}:{run.job_name}"


def fix_of(state: TriageState) -> dict[str, str]:
    """The fix itself (the action's parameters), not the full rerun parameters."""
    action = state["resolution"].action if state.get("resolution") else None
    return {k: str(v) for k, v in action.parameters.items()} if action else {}


def memory_text(state: TriageState, seen: int, verified_by: str | None = None) -> str:
    inc, ev, rc, res, rem = (
        state["incident"],
        state["evidence"],
        state["root_cause"],
        state["resolution"],
        state.get("remediation"),
    )
    approval = state.get("approval")
    changed = "; ".join(
        f"{k}: '{a}' → '{b}'" for k, (a, b) in (ev.comparison.changed_parameters if ev.comparison else {}).items()
    )
    return "\n".join(
        [
            f"Past incident {inc.incident_id} — resolved, verified by {verified_by or f'rerun {rem.rerun_run_id}'}",
            f"Job {inc.run.job_name} · source {inc.source} · failed task {ev.failed_task}",
            f"Symptom: {(ev.error or '').strip().splitlines()[0][:300] if ev.error else '-'}",
            f"What had changed since the last success: {changed or 'nothing in the job parameters'}",
            f"Root cause: {rc.cause}",
            f"Fix that worked: rerun_job {res.action.parameters if res.action else {}}"
            + (f" (approved by {approval.by})" if approval else ""),
            f"Seen {seen} time(s).",
        ]
    )


class IncidentMemory:
    def __init__(self, repo: MemoryRepository, collection: str | None = None):
        self.repo = repo
        self.collection = collection or memory_collection()

    def _upsert_point(self, memory: dict, trusted: bool) -> None:
        ensure_collection(self.collection)
        vec = get_embeddings().embed_documents([memory["text"]])[0]
        idx, vals = doc_vector(tokenize(memory["text"]), AVG_DOC_TOKENS)
        get_qdrant().upsert(
            self.collection,
            points=[
                models.PointStruct(
                    id=memory["id"],
                    vector={"dense": vec, "bm25": models.SparseVector(indices=idx, values=vals)},
                    payload={
                        "text": memory["text"],
                        "parent_id": f"memory:{memory['id']}",
                        "source": "incident_memory",
                        "source_type": "incident",
                        "title": f"past incident(s) {', '.join(memory['incident_ids'])}",
                        "memory_id": memory["id"],
                        "failure_class": memory["failure_class"],
                        "error_class": memory["error_code"],
                        "failed_task": memory["failed_task"],
                        "trusted": trusted,
                    },
                )
            ],
        )

    @staticmethod
    def trusted(memory: dict) -> bool:
        return memory["failure_count"] <= memory["success_count"]

    def record(self, state: TriageState, verified_by: str | None = None) -> dict:
        """Save (or update) the memory for a verified fix. Returns the memory row."""
        pattern = pattern_of(state)
        iid = state["incident"].incident_id
        existing = self.repo.by_pattern(pattern)
        fix = fix_of(state)
        if existing:
            seen = len(set(existing["incident_ids"]) | {iid})
            self.repo.add_occurrence(existing["id"], iid, memory_text(state, seen, verified_by), fix)
            memory = self.repo.get(existing["id"])
        else:
            codes = error_codes_in(state["evidence"].error or "")
            memory_id = self.repo.insert(
                pattern,
                iid,
                memory_text(state, 1, verified_by),
                state["root_cause"].failure_class,
                codes[0] if codes else None,
                state["evidence"].failed_task,
                pipeline_key_of(state),
                fix,
            )
            memory = self.repo.get(memory_id)
        self._upsert_point(memory, self.trusted(memory))
        return memory

    def consolidate(self, platform_of) -> list[tuple[int, int]]:
        """Merge memories that now share a pattern into the one with the longest fix record, when their fix is the
        same (a different fix stays a separate memory under its old pattern). `platform_of(connection_id)`.
        Returns (kept, merged) pairs; idempotent."""
        groups: dict[str, list[dict]] = {}
        for m in self.repo.all():
            platform = platform_of((m.get("pipeline_key") or "").split(":", 1)[0])
            groups.setdefault(narrowed_pattern(m, platform), []).append(m)
        merged = []
        for pattern, members in groups.items():
            keep = max(members, key=lambda m: (m["fix_successes"], -m["id"]))
            others = [m["id"] for m in members if m is not keep and m["fix"] == keep["fix"]]
            if pattern == keep["pattern"] and not others:
                continue
            self.repo.merge(keep["id"], others, pattern)
            if others and get_qdrant().collection_exists(self.collection):
                get_qdrant().delete(self.collection, points_selector=models.PointIdsList(points=others))
            memory = self.repo.get(keep["id"])
            self._upsert_point(memory, self.trusted(memory))
            merged += [(keep["id"], o) for o in others]
        return merged

    def record_outcome(self, cited_parent_ids: list[str], worked: bool) -> list[int]:
        """Update the track record of every memory cited in a diagnosis whose fix worked / failed."""
        updated = []
        for pid in cited_parent_ids:
            if not pid.startswith("memory:"):
                continue
            memory_id = int(pid.split(":", 1)[1])
            if self.repo.get(memory_id) is None:
                continue
            self.repo.record_outcome(memory_id, worked)
            memory = self.repo.get(memory_id)
            self._upsert_point(memory, self.trusted(memory))
            updated.append(memory_id)
        return updated
