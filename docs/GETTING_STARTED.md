# Getting started: run the whole project

This guide takes you from a fresh clone to a working copilot: a real Databricks pipeline that fails, the
copilot that notices it, diagnoses it, emails you, waits for your approval (or auto-fixes a proven fix),
reruns the job, verifies it and learns. Allow about 1–2 hours the first time, most of it downloads and the
Databricks setup.

Steps 1–8 are the core. Steps 9–12 are optional extras (Jenkins, company documents, the Claude Code skill,
the nightly self-improvement loop).

---

## 0. What you need

| Need | Why | Notes |
|---|---|---|
| A Mac with Apple Silicon, 16 GB RAM | runs the local AI model, embeddings and Qdrant | Linux works for the core; Keychain and launchd parts are macOS-only (see "Not on a Mac") |
| Python **3.12** | the package | `brew install python@3.12` |
| [Homebrew](https://brew.sh) | installs the tools below | |
| [Ollama](https://ollama.com) | the local AI model (no API key, no cost) | download the app, open it once |
| A **Databricks Free Edition** account | the pipeline that fails | free, no credit card: search "Databricks Free Edition" on databricks.com |
| Grafana | the ticket desk | `brew install grafana` |
| Docker Desktop | *optional*: local Jenkins | only for step 9 |
| Gmail account + app password | *optional*: emails with Approve / Deny buttons | without it, notifications print in the terminal |
| Langfuse Cloud (free) | *optional*: traces of every agent step | off when the keys are missing |
| Claude Code | *optional*: the `/diagnose-incident` skill (step 11) | your own Claude login |

Keep **at least 45% memory free** while it runs (`memory_pressure | tail -1`). The copilot refuses to load
models below that, on purpose.

---

## 1. Get the code and install

```bash
git clone https://github.com/rupakumar/pipeline-incident-copilot.git
cd pipeline-incident-copilot
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"                 # add ,local-llm for the optional Apple MLX tier
pytest -q                               # 155 tests, about 10 s: the install works
```

From now on, run commands from the project folder with the virtual environment active (or prefix them with
`.venv/bin/`).

---

## 2. Your settings: the `.env` file

```bash
cp .env.example .env
```

Open `.env` and fill in **at least** the Databricks pair. Never commit `.env` (it is git-ignored).

| Setting | Required? | Where to get it |
|---|---|---|
| `DATABRICKS_HOST` | **yes** | your workspace URL, e.g. `https://dbc-xxxxxxxx-xxxx.cloud.databricks.com` |
| `DATABRICKS_TOKEN` | **yes** | Databricks → your profile → Settings → Developer → Access tokens → Generate |
| `GMAIL_SMTP_USER`, `GMAIL_APP_PASSWORD`, `NOTIFY_EMAIL_TO` | optional | Google Account → Security → 2-Step Verification → App passwords. You can also enter these later in the console (step 6) instead. |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` | optional | cloud.langfuse.com → project → Settings → API keys |
| `GRAFANA_URL`, `GRAFANA_SERVICE_ACCOUNT_TOKEN`, `DATABRICKS_GRAFANA_TOKEN` | optional | only for the Grafana **Cloud** pipeline-health dashboard (step 8b) |
| `ANTHROPIC_API_KEY` | optional | only if you switch a step to Claude; everything runs locally without it |

Other optional switches: `OLLAMA_MODEL` (default `qwen3:4b`), `COPILOT_AUTO_APPLY=off` (turn auto-fix off
everywhere), `COPILOT_APPROVAL_BASE_URL` (if the console is not on `http://localhost:8787`).

> After step 6 you can move these values out of `.env` into the console: secrets go to the macOS Keychain and
> the app reads them from there (`copilot settings import-env` does it for you, printing names only).

---

## 3. The local AI model

```bash
ollama pull qwen3:4b          # about 2.5 GB, once
```

Ollama must be running (the menu-bar app) whenever the copilot diagnoses.

---

## 4. Knowledge base: download and index

```bash
python -m scripts.download_sources     # Spark / Delta / Databricks error catalogs + LogHub sample logs → data/external
python -m scripts.build_index          # chunk + embed + index into the local Qdrant (about 1 minute)
```

The first run downloads the embedding and reranker models (about 400 MB) into the Hugging Face cache.

---

## 5. The Databricks pipeline (the thing that fails)

```bash
python -m scripts.check_connections    # can we reach Databricks with your token?
python databricks/provision.py         # schema, volume, audit tables, daily log drops (incl. broken ones)
git add -A && git commit -m "my setup" # bundle.py refuses to deploy uncommitted changes under databricks/
python databricks/bundle.py deploy     # creates the 5-task job "loghub_etl" in your workspace
```

`bundle.py` needs a current Databricks CLI (tested with v1.19): `brew install databricks/tap/databricks` or
the installer at docs.databricks.com/dev-tools/cli/install. The job lands in the `workspace` catalog, schema
`pipeline_demo` (Free Edition's default catalog).

Check: in Databricks → Jobs & Pipelines you now see **loghub_etl**.

---

## 6. Start the console, then set up email and the connection

```bash
copilot serve                          # http://localhost:8787  (leave it running in its own terminal)
```

In the browser:

1. **Settings** → enter the sender Gmail address, the app password and the default recipients → **Save** →
   **Send test email**. (Skip if you filled them in `.env`; the page shows where each value comes from.)
2. **Connections** → the **Databricks** card (created automatically from your `.env`) → **Edit**:
   - Workspace URL + token (optional: moves them into the Keychain),
   - tick the job **loghub_etl**,
   - **Approvers** (who gets the Approve / Deny email) and **Notify** (FYI only),
   - **Auto-fix known failures**: leave off at first; turn on once you trust it,
   - **Test connection** → the cable turns green → **Save changes**.

Or do it all at once from the terminal: `copilot settings import-env`.

---

## 7. Start watching

```bash
copilot watch --diagnose               # polls every 60 s, diagnoses every new failure (own terminal)
```

Run it unattended instead (macOS, restarts on crash and at login):

```bash
python -m scripts.launchd_services install    # watcher + console + nightly loop (status / uninstall)
```

If you use launchd, stop the manual `copilot serve` first (both want port 8787).

---

## 8. Dashboards

### 8a. Local ticket desk (Grafana on your Mac)

```bash
python -m scripts.local_grafana        # http://localhost:3000  (own terminal; admin / admin to edit)
```

Open **Incident Command** (tickets, approval queue with confidence, Approve / Deny, what it has learned),
**Ticket detail**, **Connections** and **Self-improvement**.

### 8b. Optional: pipeline health on Grafana Cloud

1. Edit `infra/grafana/cloud/dashboards/pipeline_health.json`: replace `dbc-xxxxxxxx-xxxx` (3 links) with
   **your** workspace host.
2. Fill the three Grafana values (`.env` or console Settings + the Databricks form's Dashboards step).
3. `python -m scripts.publish_grafana_dashboard`

---

## 9. Your first incident (end to end)

```bash
python -m scripts.demo_failure --scenario pe-04    # breaks one Databricks run on purpose (~2–6 min)
```

What you should see, in order:

1. Watcher log: a new `INC-0001` → email **"Investigating"**.
2. About a minute later: diagnosis → email **"Approval needed"** with the cause, the proposed fix and its
   **confidence**.
3. Click **Approve** (email, Grafana or `copilot approve INC-0001`) → **Confirm**.
4. The copilot reruns the job → verifies it → email **"Resolved"** → `copilot memories` shows what it learned.

Other scenarios: `evals/datasets/failure_scenarios.yaml` (e.g. `re-02` out of memory, `mi-01` missing input,
`dq-01` data quality). Repeat the same scenario twice more with **Auto-fix** on and the third one fixes itself.

Useful: `copilot status`, `copilot show INC-0001`, `copilot incidents`, `copilot notify INC-0001` (re-send).

---

## 10. Optional: Jenkins as a second platform

```bash
python -m scripts.local_jenkins start     # Docker, http://localhost:8080, about 1 GB
```

The admin password and the API token are in files under `data/runtime/jenkins/home/copilot/`. In the console:
**Connections → Jenkins → Connect**, URL `http://localhost:8080`, user `admin`, the API token, tick
`loghub-daily-export`. Break it: in Jenkins, **Build with Parameters** with `MIN_ROWS=5000`.
`python -m scripts.local_jenkins stop` frees the memory.

## 11. Optional: company documents and the Claude Code skill

- **Knowledge**: console → **Knowledge** → pick a type (architecture, runbook, product docs, postmortem) → drop
  PDF / Word / Markdown files. They are indexed in seconds and cited in later diagnoses.
- **Task 1 skill**: open the project in Claude Code, accept the workspace trust prompt (the subagents' safety
  hooks need it), then run `/diagnose-incident INC-0001`. Five read-only subagents investigate and file a fix
  for approval; they can never run it themselves.

## 12. Optional: the nightly self-improvement loop

Installed by `launchd_services install` (runs at 02:30). Try it now:

```bash
copilot loop nightly --quick            # ~4 min: scorecard, one proposed change, gate, digest email
```

Accepted changes appear as a git branch `improve/…` for you to review; nothing is merged automatically.

---

## Files you may need to change

| File | Change | When |
|---|---|---|
| `.env` | your Databricks host + token (+ optional email, Langfuse, Grafana) | always |
| `infra/grafana/cloud/dashboards/pipeline_health.json` | `dbc-xxxxxxxx-xxxx` → your workspace host | only for Grafana Cloud |
| `config/tunables.yaml` | thresholds and retrieval sizes (the nightly loop tunes these within bounds) | optional |
| `knowledge/runbooks/*.md` | your own runbooks (or upload them on the Knowledge page) | optional |
| `databricks/resources/loghub_etl.job.yml` | job schedule, notification emails | optional |

Nothing else is tied to a specific workspace: the job, catalog and schema names are created by step 5.

## Checks that everything works

```bash
pytest -q && ruff check && ruff format --check          # code quality gates
python -m evals.runners.diagnosis_eval --split test      # 9 held-out failures scored objectively (~3 min)
copilot connections --check                              # every connection reachable
python -m scripts.launchd_services status                # watcher / console running
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| "memory guard: only N% free" | close apps (Docker, browser tabs) until `memory_pressure` shows ≥ 45% free |
| Databricks "Invalid access token" | new token → console Databricks form (or `.env`), then **Check now** |
| `Address already in use` on 8787 | the launchd console is already running; don't start `copilot serve` by hand |
| Diagnosis stuck "investigating" | Ollama not running; open the app. The watcher retries stuck tickets after 15 min |
| No emails | console Settings → **Send test email**; check Gmail's Spam / Promotions tabs |
| Grafana panels empty | start the console and the watcher first; dashboards read the copilot's SQLite store |
| `bundle.py deploy` refuses | commit your changes under `databricks/` first (each run records the deployed commit) |

## Stop everything

```bash
python -m scripts.launchd_services uninstall   # or Ctrl+C the watch / serve terminals
python -m scripts.local_jenkins stop
# Ctrl+C the Grafana terminal; quit Ollama and Docker from the menu bar
```

## Not on a Mac

The core (steps 1–9) runs on Linux with Python 3.12 and Ollama. Two macOS-only parts: secrets use the system
keyring (on Linux install a keyring backend such as `gnome-keyring`, or keep them in `.env`), and the background
services use launchd (run `copilot watch --diagnose` and `copilot serve` under systemd or tmux instead).
