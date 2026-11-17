"""Versioned evaluation datasets (evals/datasets/) and the git-ignored reports folder."""

import json
from functools import lru_cache

import yaml

from pipeline_incident_copilot.config import EVALS_DIR

DATASETS_DIR = EVALS_DIR / "datasets"
REPORTS_DIR = EVALS_DIR / "reports"
SCENARIOS_FILE = DATASETS_DIR / "failure_scenarios.yaml"
RUN_MANIFEST = DATASETS_DIR / "run_manifest.jsonl"
FAILED_RUNS_FILE = DATASETS_DIR / "failed_runs.json"


@lru_cache(maxsize=1)
def load_scenario_catalog() -> dict:
    return yaml.safe_load(SCENARIOS_FILE.read_text())


def scenario_params(scenario: dict) -> dict[str, str]:
    volume = load_scenario_catalog()["volume"]
    return {k: str(v).replace("{volume}", volume) for k, v in scenario["params"].items()}


def scenarios_by_id() -> dict[str, dict]:
    catalog = load_scenario_catalog()
    out = {s["id"]: {**s, "expect_failure": True} for s in catalog["scenarios"]}
    out.update({b["id"]: {**b, "expect_failure": False, "split": "baseline"} for b in catalog["baselines"]})
    return out


def relevant_documents(failure_class: str) -> list[str]:
    return load_scenario_catalog()["failure_classes"][failure_class]["relevant_documents"]


@lru_cache(maxsize=1)
def ticket_index() -> list[dict]:
    """Metadata of the generated support tickets (parent_id, failure_class, source_run_id)."""
    from pipeline_incident_copilot.rag.ingestion.loaders import KNOWLEDGE_DIR
    from pipeline_incident_copilot.rag.ingestion.loaders.support_ticket_loader import SupportTicketLoader

    return [d.metadata for d in SupportTicketLoader(KNOWLEDGE_DIR / "tickets").load()]


def own_ticket_ids(run: dict) -> set[str]:
    """Tickets written from this very run — excluded from its results (it can't answer itself)."""
    return {t["parent_id"] for t in ticket_index() if t.get("source_run_id") == str(run["run_id"])}


def relevant_for_run(run: dict) -> set[str]:
    """Class-level labels, scenario extras, and past tickets of the same class (minus the run's own)."""
    same_class = {t["parent_id"] for t in ticket_index() if t.get("failure_class") == run["failure_class"]}
    extras = set(scenarios_by_id()[run["scenario_id"]].get("extra_relevant_documents", []))
    return set(relevant_documents(run["failure_class"])) | extras | (same_class - own_ticket_ids(run))


def load_failed_runs(split: str | None = None) -> list[dict]:
    runs = json.loads(FAILED_RUNS_FILE.read_text())
    return [r for r in runs if split is None or r["split"] == split]


def write_report(name: str, data) -> None:
    REPORTS_DIR.mkdir(exist_ok=True)
    (REPORTS_DIR / name).write_text(json.dumps(data, indent=2))
