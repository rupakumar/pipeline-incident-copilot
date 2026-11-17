# Pipeline Incident Copilot

Multi-agent RAG copilot that diagnoses failed data pipelines the way an on-call engineer would:
it notices the failed run, collects evidence (error, trace, what changed since the last good run,
live checks), searches known errors, runbooks, past tickets and its own memory of verified fixes,
names the root cause, proposes a safe fix, and runs it **only after a person approves** — then
verifies the rerun and learns from it. Everything runs locally (Ollama `qwen3:4b`); Claude is an
optional tier.

**New here? Follow [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)**: install, settings, the Databricks
pipeline, the console, dashboards and your first incident, step by step.

## How an incident flows

```
A pipeline fails on any connected platform (Databricks job, Jenkins build, ...)
 → copilot watch        FailureWatcher polls every enabled connection → incident saved (SQLite) → evidence from
                        that platform's connector → email "investigating"
 → agents (LangGraph)   incident (rules) → log analysis (rules) → knowledge (corrective RAG + memory)
                        → root cause (one model call; tools + retry only when unsure; evidence-calibrated confidence)
                        → resolution (safe-action guardrail: only resets or last-working values)
 → email "approval needed" with Approve / Deny buttons      (no safe fix or unsure → escalated to a person)
 → you approve          button → confirm page (copilot serve) or `copilot approve INC-x`
 → remediate + verify   rerun on the same platform → succeeded? → resolved + learned (memory) → email "resolved"
                                                → failed?   → re-diagnose once with the new error → else escalated
```

## Quick start (day to day)

```bash
copilot watch --diagnose          # watch every connection; diagnose every new failure (Ctrl+C to stop)
copilot connections --check       # connected platforms, their pipelines and health
copilot serve                     # the console + approval page: http://localhost:8787
python -m scripts.local_grafana   # the ticket desk (local Grafana): http://localhost:3000
copilot status                    # summary: incidents by status, memories, watcher
copilot show INC-0020             # one incident: evidence, diagnosis, fix, history
copilot approve INC-0020 --note "ok"   /   copilot reject INC-0020 --note "not now"
copilot notify INC-0020           # re-send an incident's email
copilot memories                  # what it has learned from verified fixes
```

Email: enter the sender Gmail address, a Google app password and the default recipients in the console's
**Settings** page (stored in the Keychain; `.env` works as a fallback). Each connection can name its own
approvers (each gets their own signed links) and notify list. The Approve / Deny buttons open the console on the Mac that runs `copilot serve`; links are
signed, single use, and expire after 24 hours; nothing changes until you confirm on the page.

## Task 1 — Diagnose with Claude Code (`/diagnose-incident`)

The same incident, investigated by Claude Code instead of the LangGraph graph: run `/diagnose-incident INC-x`
in this repo. Three subagents (incident-triage, log-analyst, knowledge-retriever) work in parallel, then
root-cause-analyst and resolution-planner; the skill writes a one-page report to `data/runtime/claude_reports/`.
If a safe rerun exists it **files** it with `copilot request-approval`; a person approves it in the console,
Grafana, the email or with `copilot approve INC-x`, and only then does the copilot rerun, verify and learn.

Safety, enforced (not just prompted):
- **Read-only subagents**: a `PreToolUse` hook in each agent (`guardrails/agent/`) allows only that agent's
  `copilot` read commands, no shell operators, and reads of `databricks/notebooks/`, `knowledge/`,
  `infra/jenkins/jobs/` only (never `.env`, `data/`, tokens). It fails closed. Every call is audited
  (`data/runtime/claude_skill/audit/`) and attached to the incident's Langfuse trace.
- **No fix without a person**: the skill cannot run or approve anything; `request-approval` re-checks the guardrail.
- **Prompt injection**: log text is marked untrusted and instruction-like lines are flagged (`injection_suspected`);
  the skill escalates instead of proposing when they appear.
- **Low confidence escalates** (< 0.7, needs_human, no safe action). Models pinned per agent, turn limits set.

```bash
copilot evidence INC-0020                            # what the subagents read (JSON, secrets masked, untrusted marked)
copilot search "UNAUTHORIZED_ACCESS CREATE TABLE"    # knowledge search (JSON)
copilot check-fix INC-0020 --reset gold_table=       # would this rerun pass the guardrail? (changes nothing)
copilot request-approval INC-0020 --reset gold_table= --report data/runtime/claude_reports/INC-0020.json
copilot approve INC-0020                             # a person; also the console / Grafana / email buttons
python -m scripts.demo_failure --scenario pe-04      # a fresh failing run + its incident, for a demo
python -m evals.runners.claude_skill_eval            # score the skill like the graph (+ prompt-injection case)
```

## Task 2 — The self-running loop

launchd runs the copilot unattended: the **watcher** and the **console** stay up (restarted if they stop), and a
**nightly job** (02:30) scores the system, tries **one** improvement and emails a **digest**.

```bash
python -m scripts.launchd_services install      # watcher + console + nightly (uninstall / status / run-nightly-now)
copilot loop nightly --quick                    # the nightly job by hand (demo: 4 incidents per split, ~5 min)
copilot loop digest --print                     # the digest for the last 24 h
```

- **Improver**: changes only `config/tunables.yaml` (one knob one step, inside its bounds) or, with `--prompts`,
  adds one rule to an agent prompt (rules that say never/only/must are kept). Never code, guardrails, auto-apply
  or approvals. A change tried in the last 30 days is not proposed again.
- **Validator**: the diagnosis eval runs in its own process per configuration, on a copy of the store. Accepted only
  if quality rises beyond noise (2 runs each on the tuning split) with no metric regressing, and the held-out
  incidents (never used to choose) pass the regression gate.
- **Accepted** = a branch `improve/…` with one commit (made in a temporary worktree; your checkout is untouched).
  Nothing is merged: review with `git diff main..improve/…` and merge it yourself.
- **Safe to run unattended**: one copy at a time, a shared lock so the watcher's diagnoses and the nightly eval
  never load models together, AC power only, ≥45% free memory, logs in `data/runtime/logs/`.
- Every eval and decision is in SQLite and on the Grafana **Self-improvement** dashboard; the Connections
  dashboard shows the watcher heartbeat (minutes since its last poll).

## Connect pipelines (plug and play)

Each platform account is a **connection**; the jobs you list on it are the **applications** the copilot watches.
Add one from Grafana (**+ Connect a pipeline**) or at http://localhost:8787/connections (`copilot serve`): pick the
platform, fill in its fields, press **Test and save**. Nothing is saved unless the test reaches the platform and
finds every job. Secrets (tokens, keys) go to the **macOS Keychain**; SQLite and Grafana only see the settings.

| Platform | Status | What the connector does |
|---|---|---|
| Databricks | live | Jobs API runs, task error + trace, comparison with the last success, git diff, rerun with parameters |
| Jenkins | live | builds, console log, failed stage / build step, parameters vs last success, rebuild with parameters |
| Snowflake, AWS Glue, Azure Data Factory | adapter form ready | needs a paid account to test; connectors follow |
| Airflow, dbt Cloud, BigQuery, GitHub Actions, Kafka Connect, Fivetran | in the gallery | coming |

Reruns on a newly connected pipeline follow the strict rule: every changed parameter may only go back to its
default or to the value of the last successful run (the Databricks job also has its own value rules).
A local Jenkins for demos: `python -m scripts.local_jenkins start` (Docker; jobs `loghub-daily-export` and
`loghub-report-publish`, set up as code in `infra/jenkins/`); the admin password and the API token for the form
are in files under `data/runtime/jenkins/home/copilot/`. `stop` frees ~1 GB.

New platform = one class in `connectors/<platform>/` implementing `connectors/base.py` (FailureSource,
EvidenceSource, RerunTarget) + one entry in `connectors/catalog.py` (the form is built from its field list).

## Auto-apply known fixes

A failure the copilot has already fixed can be fixed again without waiting for approval, if you allow it:
switch on **Auto-apply known fixes** for a connection in its form (off by default; "minimum verified successes",
default 2). A fix then runs on its own only when all hold: same failure pattern on the same pipeline as a remembered
fix; the proposed fix is exactly that fix; it resolved at least the minimum number of incidents and never failed;
calibrated confidence ≥ 0.85; first attempt for this incident; outside the pipeline's cooldown (set per connection,
default 1 h, 0 = none); the guardrail passes. Tickets already waiting are re-checked every minute. History and Grafana show "approved automatically: known fix from INC-…", the email says
**Auto-fixed**. Anything else waits for approval, with a note when it matches a known fix. A failed auto-fix marks
the fix as failed, so it is never auto-applied again. `COPILOT_AUTO_APPLY=off` in `.env` switches it off everywhere.

## Ticket desk (local Grafana)

`python -m scripts.local_grafana` starts Grafana on this Mac (`brew install grafana` once; the SQLite plugin
installs itself). It reads `data/runtime/copilot.db` read-only and refreshes every 10 s:

- **Incident desk**: ticket counts (incl. auto-fixed), every ticket (with who decided: a person or "auto · known fix") (waiting for approval first) with its application and
  **Approve / Deny** beside each proposed fix, tickets by failure class, latest activity; filter by
  **Platform** and **Application**.
- **Ticket detail**: status, the decision buttons, diagnosis, evidence, history, links to the failed run on
  its platform and the Langfuse trace.
- **Connections**: every connected platform and application: last run, runs and failures in 24 h, open
  tickets, connection status (a bad token or a platform that is down shows as "check failed").

Approve / Deny open the approval console's confirm page (`copilot serve` must be running): opening changes
nothing, **Confirm** runs it, and the history records "approved by … (Grafana)". View without login;
`admin` / `admin` to edit. Dashboards are generated by `python -m scripts.build_local_dashboards` into
`infra/grafana/local/dashboards/` (edit the script, not the UI).
The pipeline-health dashboard (Databricks data) stays on Grafana Cloud: `python -m scripts.publish_grafana_dashboard`.

## Layout

| Folder | Purpose |
|---|---|
| `src/pipeline_incident_copilot/` | The product (installable package) |
| `  schemas/` | Shared data shapes: incident, evidence, agent outputs, triage state, remediation |
| `  connectors/` | One package per pipeline platform (`databricks/`, `jenkins/`) behind `base.py`; `catalog.py` (platforms + form fields), `hub.py` (live connector per connection) |
| `  integrations/` | Databricks (Jobs API, SQL warehouse), git |
| `  detection/` | Failure watcher (all connections), incident intake |
| `  security/` | Secret store (macOS Keychain) |
| `  tuning/` | Tunable settings as data (`config/tunables.yaml`) with their allowed bounds |
| `  improvement/` | Self-improvement loop: proposers, evaluator, validator (gate rules), branch publisher, nightly loop |
| `  automation/` | Guards for unattended runs: single instance, heavy-work lock, AC power, memory, log rotation |
| `  tools/` | Read-only investigation tools (path exists, table schema, permissions, run comparison, git diff) |
| `  rag/` | Retrieval: `ingestion/` → `embedding/` → `vector_store/` → `retrieval/` (query, routes incl. exact error code + memory, fusion, reranking) |
| `  agents/` | The five agents and their prompts (never import LangGraph) |
| `  workflow/` | The LangGraph graph, nodes, routing, corrective-RAG subgraph, checkpointer, diagnoser |
| `  remediation/` | Run the approved rerun, verify it |
| `  learning/` | Incident memory (verified fixes), confidence calibrator |
| `  llm/` | Model router: Ollama tier (default), local MLX, Claude |
| `  guardrails/` | Action allowlist, subagent tool policy (hooks), injection detector, secret redaction, memory guard |
| `  notifications/` | Email (HTML + text), signed approval links |
| `  interface/` | `copilot serve`: incident console, approval page, connect form |
| `  storage/` | SQLite: incidents, events, evidence, diagnoses, memories, connections, pipeline health (versioned schema, migrates with a backup) |
| `  cli/` | The `copilot` command |
| `evals/` | Datasets, metrics, runners, regression gate, baselines (outside the product) |
| `databricks/` | The pipeline we monitor: Asset Bundle, one notebook per task, audit-table DDL, `provision.py` |
| `infra/grafana/cloud/` | Pipeline-health dashboard for Grafana Cloud (Databricks data) |
| `infra/grafana/local/` | Local Grafana: config, provisioning, Incident desk + Ticket detail + Connections dashboards |
| `infra/launchd/` | launchd jobs: watcher, console, nightly loop |
| `config/` | `tunables.yaml`: what the self-improvement loop may change |
| `infra/jenkins/` | Local demo Jenkins: init script (login, API token, jobs) and job definitions |
| `knowledge/` | Architecture doc, runbooks, past tickets |
| `scripts/` | Developer utilities |
| `tests/` | Unit tests |
| `data/` | Git-ignored: `external/` downloads, `runtime/` indexes, caches, SQLite, checkpoints |

## Setup

Full walkthrough with prerequisites, what to change and troubleshooting: [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md).

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,local-llm]"        # local-llm = Apple Silicon MLX (optional now)
cp .env.example .env                     # Databricks host + token (others optional) — never commit
ollama pull qwen3:4b                     # the local model for root cause + resolution
python -m scripts.download_sources       # error catalogs + LogHub logs
python -m scripts.check_connections
python databricks/provision.py           # schema, volume, audit tables, daily log drops
python databricks/bundle.py deploy       # deploy the job (Databricks CLI; refuses uncommitted changes)
python -m scripts.build_index            # chunk + embed + index the knowledge into Qdrant
python -m scripts.publish_grafana_dashboard   # Grafana data source + pipeline status dashboard
```

## Evaluation

```bash
python -m evals.run_scenarios --split train        # break the pipeline on purpose (real Databricks runs)
python -m evals.export_failed_runs                 # snapshot failed runs as eval data
python -m evals.runners.retrieval_eval -v          # retrieval setups (hit@k, recall, MRR, nDCG)
python -m evals.runners.diagnosis_eval --split test --memory off        # end-to-end scorecard
python -m evals.runners.diagnosis_eval --memory from-train              # learn from train, score test
python -m evals.regression_gate evals/reports/diagnosis_test_memory-off.json   # fail if worse than baseline
pytest && ruff check . && ruff format --check .
```

Evals work on a copy of the incident store and a separate memory collection, so they never change
real incidents or what the copilot has learned. Scores are attached to the Langfuse traces.
