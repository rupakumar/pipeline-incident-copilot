"""Diagnosis eval — the end-to-end scorecard for the agent graph (Phase 8).

Runs the graph on incidents of known scenarios and scores every answer (see evals/metrics/
diagnosis_metrics.py): correctness (class · fact · action), RAGAS-style context recall / precision
and faithfulness, calibration (Brier, ECE) of the model's and the evidence-calibrated confidence,
escalations, memory use, seconds and tokens. Each incident's scores are attached to its Langfuse trace.

Memory modes (--memory):
  off         no memory (fair: a test incident's own fix may already be in the real memory) [default]
  from-train  learn only from TRAIN incidents (into a separate eval collection), then score TEST with it
  on          the real memory (not a fair score; to see what the live system would do)

Usage: python -m evals.runners.diagnosis_eval [--split test|train|all] [--memory off|from-train|on] [--limit N]
"""

import argparse
import json
import os
import shutil  # noqa: E402
import sqlite3
import time

from pipeline_incident_copilot.config import RUNTIME_DIR  # noqa: E402

os.environ["LLM_CACHE_ENABLED"] = "false"  # honest timings: every model call is real
EVAL_MEMORY_COLLECTION = "incident_memory_eval"
# Diagnosing changes incident statuses: work on a copy so the real store (and resolved incidents) stay as they are
EVAL_DB = RUNTIME_DIR / "eval_copilot.db"
shutil.copyfile(RUNTIME_DIR / "copilot.db", EVAL_DB)
os.environ["COPILOT_DB"] = str(EVAL_DB)
# An eval only diagnoses: a known fix must never be auto-applied to the real pipeline from here (kill switch +
# every connection's switch off in the copy, in case one of them is ever missed)
os.environ["COPILOT_AUTO_APPLY"] = "off"
with sqlite3.connect(EVAL_DB) as _conn:
    _conn.execute("UPDATE connections SET auto_apply = 0")
# ...and on a copy of the knowledge base, so a long eval (the nightly loop) never locks the watcher out of it
EVAL_QDRANT = RUNTIME_DIR / "eval_qdrant"
os.environ["COPILOT_QDRANT"] = str(EVAL_QDRANT)

from pipeline_incident_copilot.rag.vector_store.qdrant_store import snapshot_store  # noqa: E402

snapshot_store(EVAL_QDRANT)

from evals.dataset_loader import (  # noqa: E402
    FAILED_RUNS_FILE,
    RUN_MANIFEST,
    relevant_for_run,
    scenarios_by_id,
    write_report,
)
from evals.metrics.diagnosis_metrics import action_ok, brier, context_scores, ece, fact_ok, faithfulness  # noqa: E402

CITE_LIMIT = 4


def incidents_by_scenario(app) -> dict[str, str]:
    manifest = {
        json.loads(line)["run_id"]: json.loads(line)["scenario_id"] for line in RUN_MANIFEST.read_text().splitlines()
    }
    return {manifest[i.run.run_id]: i.incident_id for i in app.repo.recent(limit=500) if i.run.run_id in manifest}


def score_incident(app, scenario: dict, incident_id: str, failed_run: dict | None) -> tuple[dict, dict]:
    from pipeline_incident_copilot.agents.base import evidence_brief
    from pipeline_incident_copilot.agents.root_cause_agent import knowledge_brief
    from pipeline_incident_copilot.llm import model_router

    start, tokens_before = time.time(), len(model_router.token_usage)
    state = app.diagnoser.diagnose(incident_id)
    rc = state["root_cause"].model_dump()
    res = state["resolution"].model_dump() if "resolution" in state else None
    cited = [c.parent_id for c in state["knowledge"].citations]
    relevant = relevant_for_run(failed_run) if failed_run else set()
    if scenario["failure_class"] == rc["failure_class"]:  # a memory of the right kind counts as relevant
        relevant |= {pid for pid in cited if pid.startswith("memory:")}
    recall, precision = context_scores(cited, relevant, CITE_LIMIT)
    context = evidence_brief(state) + "\n" + knowledge_brief(state)
    usage = model_router.token_usage[tokens_before:]
    conf = state.get("confidence", {})
    row = {
        "scenario": scenario["id"],
        "incident": incident_id,
        "class_expected": scenario["failure_class"],
        "class_got": rc["failure_class"],
        "class_ok": rc["failure_class"] == scenario["failure_class"],
        "fact_ok": fact_ok(scenario, rc["cause"]),
        "action_ok": action_ok(scenario, res),
        "context_recall": recall,
        "context_precision": precision,
        "faithfulness": faithfulness(rc["cause"], context),
        "model_confidence": rc["confidence"],
        "calibrated_confidence": conf.get("calibrated", rc["confidence"]),
        "signals": conf.get("signals", {}),
        "status": app.repo.get(incident_id).status.value,
        "memory_cited": [p for p in cited if p.startswith("memory:")],
        "cited": cited,
        "cause": rc["cause"],
        "action": (res or {}).get("action"),
        "action_rejected": (res or {}).get("action_rejected_reason"),
        "seconds": round(time.time() - start, 1),
        "tokens_in": sum(u["input"] for u in usage),
        "tokens_out": sum(u["output"] for u in usage),
    }
    row["all_ok"] = row["class_ok"] and row["fact_ok"] and row["action_ok"]
    return row, state


def push_scores(app, row: dict) -> None:
    from pipeline_incident_copilot.observability.langfuse_tracer import get_langfuse

    trace_id = app.diagnoser.last_trace_id
    if not trace_id:
        return
    lf = get_langfuse()
    for name in ("class_ok", "fact_ok", "action_ok", "all_ok"):
        lf.create_score(
            name=f"diagnosis_{name}", value=1.0 if row[name] else 0.0, trace_id=trace_id, data_type="NUMERIC"
        )
    for name in ("context_recall", "context_precision", "faithfulness", "calibrated_confidence"):
        lf.create_score(name=name, value=float(row[name]), trace_id=trace_id, data_type="NUMERIC")


def summarize(rows: list[dict], mode: str) -> dict:
    scored = [r for r in rows if "error" not in r]
    n = len(scored) or 1
    mean = lambda key: round(sum(r[key] for r in scored) / n, 3)  # noqa: E731
    correct = [r["all_ok"] for r in scored]
    model_conf = [r["model_confidence"] for r in scored]
    calibrated = [r["calibrated_confidence"] for r in scored]
    return {
        "incidents": len(rows),
        "errors": len(rows) - len(scored),
        "class_ok": mean("class_ok"),
        "fact_ok": mean("fact_ok"),
        "action_ok": mean("action_ok"),
        "all_ok": mean("all_ok"),
        "context_recall": mean("context_recall"),
        "context_precision": mean("context_precision"),
        "faithfulness": mean("faithfulness"),
        "brier_model": brier(model_conf, correct) if scored else None,
        "brier_calibrated": brier(calibrated, correct) if scored else None,
        "ece_model": ece(model_conf, correct) if scored else None,
        "ece_calibrated": ece(calibrated, correct) if scored else None,
        "escalated": sum(r["status"] == "escalated" for r in scored),
        "memory_cited_incidents": sum(bool(r["memory_cited"]) for r in scored),
        "mean_seconds": mean("seconds"),
        "memory": mode,
    }


def run(app, ids: list[str], catalog: dict, mapping: dict, runs_by_id: dict, label: str) -> list[dict]:
    rows = []
    for sid in ids:
        incident_id = mapping[sid]
        run_info = runs_by_id.get(app.repo.get(incident_id).run.run_id)
        try:
            row, _ = score_incident(app, catalog[sid], incident_id, run_info)
        except Exception as e:  # keep going; record the failure
            rows.append({"scenario": sid, "incident": incident_id, "error": f"{e.__class__.__name__}: {e}"[:300]})
            print(f"{label} {sid:<6} {incident_id}  ERROR {e.__class__.__name__}: {str(e)[:120]}")
            continue
        push_scores(app, row)
        rows.append(row)
        marks = "".join("✓" if row[k] else "✗" for k in ("class_ok", "fact_ok", "action_ok"))
        mem = f" mem:{','.join(row['memory_cited'])}" if row["memory_cited"] else ""
        print(
            f"{label} {sid:<6} {incident_id}  {marks}"
            f"  conf {row['model_confidence']:.2f}→{row['calibrated_confidence']:.2f}"
            f"  ctx {row['context_recall']:.2f}/{row['context_precision']:.2f}  faith {row['faithfulness']:.2f}"
            f"  {row['seconds']:>5}s{mem}  {row['cause'][:60]}"
        )
    return rows


def learn_from_train(app, train_ids, catalog, mapping, runs_by_id) -> list[dict]:
    """Diagnose TRAIN incidents (no memory) and keep the fully correct ones as memories in a separate
    eval collection + SQLite file — what the learning loop would have stored after verified fixes."""
    from pipeline_incident_copilot.config import RUNTIME_DIR
    from pipeline_incident_copilot.learning.incident_memory import IncidentMemory
    from pipeline_incident_copilot.rag.vector_store.qdrant_store import get_qdrant
    from pipeline_incident_copilot.storage.database import connect
    from pipeline_incident_copilot.storage.memory_repository import MemoryRepository

    db = RUNTIME_DIR / "eval_memory.db"
    db.unlink(missing_ok=True)
    if get_qdrant().collection_exists(EVAL_MEMORY_COLLECTION):
        get_qdrant().delete_collection(EVAL_MEMORY_COLLECTION)
    memory = IncidentMemory(MemoryRepository(connect(db)), collection=EVAL_MEMORY_COLLECTION)
    learned = []
    for sid in train_ids:
        incident_id = mapping[sid]
        run_info = runs_by_id.get(app.repo.get(incident_id).run.run_id)
        row, state = score_incident(app, catalog[sid], incident_id, run_info)
        if row["all_ok"]:
            m = memory.record(state, verified_by="ground truth (evaluation, no rerun)")
            learned.append({"scenario": sid, "memory": m["id"], "pattern": m["pattern"]})
            print(f"train {sid:<6} learned memory:{m['id']}  {m['pattern']}")
        else:
            print(f"train {sid:<6} not learned (diagnosis not fully correct)")
    return learned


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="test", choices=["test", "train", "all"])
    parser.add_argument("--memory", default="off", choices=["off", "from-train", "on"])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--report-name", help="report file name (the self-improvement loop names its runs)")
    args = parser.parse_args()
    if args.memory == "from-train":
        os.environ["MEMORY_COLLECTION"] = EVAL_MEMORY_COLLECTION
    os.environ["KNOWLEDGE_USE_MEMORY"] = "true" if args.memory == "on" else "false"

    from pipeline_incident_copilot.bootstrap import App
    from pipeline_incident_copilot.llm import model_router
    from pipeline_incident_copilot.observability.langfuse_tracer import get_langfuse

    app = App()
    app.diagnoser.unload_after = False  # keep the model loaded across incidents; unloaded once at the end
    catalog = scenarios_by_id()
    mapping = incidents_by_scenario(app)
    runs_by_id = {r["run_id"]: r for r in json.loads(FAILED_RUNS_FILE.read_text())}

    def ids_for(split: str) -> list[str]:
        chosen = [
            s for s in mapping if catalog[s]["expect_failure"] and (split == "all" or catalog[s]["split"] == split)
        ]
        return sorted(chosen)[: args.limit]

    report = {}
    split = args.split
    if args.memory == "from-train":
        report["learned"] = learn_from_train(app, ids_for("train"), catalog, mapping, runs_by_id)
        os.environ["KNOWLEDGE_USE_MEMORY"] = "true"  # now score TEST using only what was learned from TRAIN
        split = "test"
    rows = run(app, ids_for(split), catalog, mapping, runs_by_id, split)
    get_langfuse().flush()
    model_router.unload_all()

    summary = summarize(rows, args.memory)
    print("\nsummary:", json.dumps(summary))
    report.update(summary=summary, rows=rows)
    write_report(args.report_name or f"diagnosis_{split}_memory-{args.memory}.json", report)


if __name__ == "__main__":
    main()
