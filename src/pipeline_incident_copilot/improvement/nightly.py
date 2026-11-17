"""NightlyLoop: the self-running part of the copilot (launchd starts it at night; `copilot loop nightly`).

guards (one copy, AC power, memory) → heavy-work lock (the watcher waits with its diagnoses)
→ scorecard: tonight's config on the tuning split (N runs, for noise) and on the holdout split
→ propose ONE change (a tunable step, or a prompt rule when enabled) → evaluate it the same way
→ validator: better beyond noise on tuning, no regression anywhere, holdout passes the gate
→ accepted: a branch for a person to merge (nothing is merged) · every decision logged with its reason
→ digest email
"""

import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pipeline_incident_copilot.automation.guards import (
    AlreadyRunning,
    enough_memory,
    heavy_work,
    on_ac_power,
    single_instance,
)
from pipeline_incident_copilot.config import EVALS_DIR, PROJECT_ROOT, RUNTIME_DIR
from pipeline_incident_copilot.improvement.evaluator import Evaluator
from pipeline_incident_copilot.improvement.proposals import PromptProposer, Proposal, TunableProposer
from pipeline_incident_copilot.improvement.publisher import BranchPublisher
from pipeline_incident_copilot.improvement.validator import judge_holdout, judge_tuning
from pipeline_incident_copilot.notifications.digest import SCORE_KEYS, build_digest, render_html, render_text
from pipeline_incident_copilot.storage.improvement_repository import ImprovementRepository
from pipeline_incident_copilot.tuning.tunables import tunables_path

CANDIDATES = RUNTIME_DIR / "improvement" / "candidates"
VERSIONED_BASELINE = EVALS_DIR / "baselines" / "diagnosis_test_memory-off.json"
QUICK_LIMIT = 4  # demo / smoke run: 4 incidents per split, one run each


@dataclass
class NightlyOptions:
    repeats: int = 2  # runs per configuration on the tuning split (measures noise)
    quick: bool = False
    improve: bool = True
    prompts: bool = False  # also let the local model propose prompt rules
    force: bool = False  # run on battery power (never overrides the memory check)
    send: bool = True


@dataclass
class NightlyResult:
    notes: list[str] = field(default_factory=list)
    scorecard: dict = field(default_factory=dict)
    decision: dict | None = None
    digest_sent: bool = False


class NightlyLoop:
    def __init__(self, conn, notifier, evaluator: Evaluator | None = None, publisher=None, text_model=None):
        self.conn = conn
        self.log = ImprovementRepository(conn)
        self.notifier = notifier
        self.evaluator = evaluator
        self.publisher = publisher or BranchPublisher()
        self.text_model = text_model

    def run(self, opts: NightlyOptions) -> NightlyResult:
        result = NightlyResult()
        try:
            with single_instance("nightly"):
                ok_mem, free = enough_memory()
                if not (opts.force or on_ac_power()):
                    result.notes.append("nightly eval skipped: the Mac is on battery power")
                elif not ok_mem:
                    result.notes.append(f"nightly eval skipped: only {free}% memory free (need 45%)")
                else:
                    with heavy_work():
                        self._evaluate_and_improve(opts, result)
        except AlreadyRunning:
            result.notes.append("another nightly run was still going: skipped")
        except Exception as e:  # noqa: BLE001 — the digest must still go out and say what failed
            result.notes.append(f"nightly run failed: {e.__class__.__name__}: {str(e)[:300]}")
        digest = build_digest(self.conn, scorecard=result.scorecard, notes=result.notes)
        if opts.send:
            result.digest_sent = self.notifier.send_message(digest.subject, render_text(digest), render_html(digest))
        return result

    # ---------------------------------------------------------------------------------------------------
    def _evaluate_and_improve(self, opts: NightlyOptions, result: NightlyResult) -> None:
        evaluator = self.evaluator or Evaluator(limit=QUICK_LIMIT if opts.quick else None)
        repeats = 1 if opts.quick else opts.repeats
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
        config = f"tunables {tunables_path().name}"
        if opts.quick:
            result.notes.append(f"quick run: {QUICK_LIMIT} incidents per split, 1 run each (demo; not a real gate)")

        base_runs = []
        for n in range(repeats):
            r = evaluator.run("train", f"loop_{stamp}_baseline_train_{n}")
            self.log.add_eval_run("baseline", "train", config, r.summary)
            base_runs.append(r)
        holdout_base = evaluator.run("test", f"loop_{stamp}_baseline_test")
        self.log.add_eval_run("baseline", "test", config, holdout_base.summary)
        result.scorecard = self._scorecard(holdout_base.summary, opts.quick)

        if not opts.improve:
            return
        proposal = self._propose(opts, [row for r in base_runs for row in r.rows if not row.get("all_ok", True)])
        if proposal is None:
            result.decision = self._decide(
                "tunable", "-", "nothing left to try", "skipped", "all nearby values tried", {}
            )
            return
        tunables, prompts = self._materialize(proposal, stamp)
        cand_runs = []
        for n in range(repeats):
            r = evaluator.run("train", f"loop_{stamp}_candidate_train_{n}", tunables, prompts)
            self.log.add_eval_run("candidate", "train", proposal.change, r.summary)
            cand_runs.append(r)
        verdict = judge_tuning([r.summary for r in base_runs], [r.summary for r in cand_runs])
        if verdict.accepted:
            holdout = evaluator.run("test", f"loop_{stamp}_candidate_test", tunables, prompts)
            self.log.add_eval_run("candidate", "test", proposal.change, holdout.summary)
            verdict = judge_holdout(verdict, holdout.summary, holdout_base.summary)
        branch = self.publisher.publish(proposal, verdict.reason) if verdict.accepted else None
        result.decision = self._decide(
            proposal.kind,
            proposal.target,
            proposal.change,
            "accepted" if verdict.accepted else "rejected",
            verdict.reason,
            verdict.deltas,
            branch,
        )

    def _propose(self, opts: NightlyOptions, failed_rows: list[dict]) -> Proposal | None:
        if opts.prompts and failed_rows and self.text_model is not None:
            proposal = PromptProposer(self.text_model, self.log.tried).propose(failed_rows)
            if proposal:
                return proposal
        return TunableProposer(self.log.tried).propose()

    def _materialize(self, proposal: Proposal, stamp: str) -> tuple[Path | None, Path | None]:
        """Write the candidate outside the repo: a tunables file, or a full copy of the prompts with one change."""
        folder = CANDIDATES / stamp
        folder.mkdir(parents=True, exist_ok=True)
        tunables = prompts = None
        for rel, content in proposal.files.items():
            if rel.endswith("tunables.yaml"):
                tunables = folder / "tunables.yaml"
                tunables.write_text(content)
            else:
                prompts = folder / "prompts"
                if not prompts.exists():
                    shutil.copytree(PROJECT_ROOT / Path(rel).parent, prompts)
                (prompts / Path(rel).name).write_text(content)
        return tunables, prompts

    def _decide(self, kind, target, change, verdict, reason, deltas, branch=None) -> dict:
        self.log.add_decision(kind, target, change, verdict, reason, deltas, branch)
        return {"kind": kind, "change": change, "verdict": verdict, "reason": reason, "branch": branch}

    @staticmethod
    def _scorecard(tonight: dict, quick: bool) -> dict:
        base = (
            json.loads(VERSIONED_BASELINE.read_text())["summary"] if VERSIONED_BASELINE.exists() and not quick else {}
        )
        return {k: (tonight.get(k), base.get(k)) for k in SCORE_KEYS if k in tonight}
