"""copilot — watch a pipeline for failures, diagnose them, and approve fixes.

copilot watch [--once] [--interval 60] [--lookback 50] [--since ISO] [--diagnose]   (every enabled connection)
copilot loop nightly [--quick] [--no-improve] [--prompts] [--force] [--no-email]   (eval → improve → digest)
copilot loop digest [--print]         (send / print the digest for the last 24 h)
copilot connections [--check]         (connected platforms and their pipelines; add them at /connections)
copilot diagnose INC-0001             (run the agents on one incident)
copilot approve INC-0001 [--note …]   (run the proposed fix, then verify it)
copilot reject INC-0001 [--note …]    (nothing is changed)
copilot notify INC-0001               (re-send the notification for its current status)
copilot serve [--port 8787]           (the page the email's Approve / Deny buttons open)
copilot memories [--consolidate]      (what the copilot has learned from verified fixes)
copilot learn INC-0001                (turn an already-resolved incident into a memory)
copilot settings import-env           (move .env values into the console's Settings / Keychain; names only)
copilot status                        (one-screen summary: incidents by status, memories, watcher)
copilot incidents [--status awaiting_approval]
copilot show INC-0001

For Claude Code (JSON output; nothing here runs a fix — `request-approval` only asks a person):
copilot evidence INC-0001
copilot search "<query>" [--k 6]
copilot check check_path_exists path=/Volumes/…
copilot check-fix INC-0001 --reset gold_table=     (guardrail only: would this rerun be allowed?)
copilot request-approval INC-0001 --reset gold_table= --report r.json   (file the fix; a person approves it)
copilot record INC-0001 --report r.json --decision deny|escalate   (history entry only)
"""

import argparse
import json
import sys
import time
from datetime import UTC, datetime, timedelta

from pipeline_incident_copilot.automation.guards import heavy_work
from pipeline_incident_copilot.bootstrap import App
from pipeline_incident_copilot.observability.langfuse_tracer import get_langfuse
from pipeline_incident_copilot.schemas.incident import IncidentStatus

SWEEP_HOURS = 24  # the watcher diagnoses open, undiagnosed incidents this recent; older ones are left for a person
STALL_MINUTES = 15  # 'investigating' with no activity this long: the diagnosing process died


def watch(app: App, once: bool, interval: int, lookback: int, since: datetime | None, diagnose: bool) -> None:
    mode = "checking once" if once else f"watching every {interval}s"
    enabled = app.connections.enabled()
    print(f"{mode}: {len(enabled)} connection(s)")
    for c in enabled:
        start = app.watcher.watch_from(c.connection_id, since)
        print(f"  {c.connection_id} ({c.platform}): {', '.join(c.pipelines)}; runs after {start:%Y-%m-%d %H:%M} UTC")
    try:
        while True:
            for run in app.watcher.poll(lookback=lookback):
                opened = app.intake.process(run)
                if opened is None:  # another process opened it between the poll and now
                    continue
                incident, evidence = opened
                print(
                    f"{incident.incident_id}  {run.platform} {run.job_name} run {run.run_id}  "
                    f"{run.failed_task or run.result_state}"
                )
                for r in evidence.tool_results:
                    print(f"    {r.tool}: {r.summary}")
                for p in evidence.problems:
                    print(f"    (not collected) {p}")
                if diagnose:
                    diagnose_in_watch(app, incident.incident_id)
            if diagnose:  # incidents another process opened (e.g. scripts.demo_failure) or a crash left behind
                since = datetime.now(UTC) - timedelta(hours=SWEEP_HOURS)
                for incident in app.repo.undiagnosed(since=since):
                    print(f"{incident.incident_id}  open and not diagnosed yet: diagnosing")
                    diagnose_in_watch(app, incident.incident_id, expect=IncidentStatus.OPEN)
                for incident in app.repo.waiting_with_known_fix(since=since):
                    auto_apply_in_watch(app, incident.incident_id)
                for incident in app.repo.stalled(since=since, quiet_for=timedelta(minutes=STALL_MINUTES)):
                    print(f"{incident.incident_id}  stuck investigating for {STALL_MINUTES}+ min: diagnosing again")
                    diagnose_in_watch(app, incident.incident_id, expect=IncidentStatus.INVESTIGATING)
            if once:
                return
            time.sleep(interval)
    except KeyboardInterrupt:
        print("stopped")
    finally:
        get_langfuse().flush()


def diagnose_in_watch(app: App, incident_id: str, expect: IncidentStatus | None = None) -> None:
    with heavy_work():  # never load models while the nightly eval runs (it holds this lock)
        if expect is not None and app.repo.get(incident_id).status is not expect:
            return  # another process handled it while we waited for the lock
        state = app.diagnoser.diagnose_or_escalate(incident_id)
    if state is None:
        print(f"    diagnosis failed: {incident_id} escalated (see copilot show)")
    else:
        print_diagnosis(state)


def auto_apply_in_watch(app: App, incident_id: str) -> None:
    """A waiting ticket whose known fix may now run on its own (cooldown over, settings changed)."""
    from pipeline_incident_copilot.learning.auto_approval import AutoApprovalPolicy

    policy = AutoApprovalPolicy(app.memory.repo, app.connections, app.repo)
    with heavy_work():
        try:
            if app.diagnoser.auto_apply_waiting(incident_id, policy):
                print(f"{incident_id}  waiting ticket auto-fixed: {app.repo.get(incident_id).status.value}")
        except Exception as e:  # noqa: BLE001 — one ticket must not stop the watcher; it stays waiting for a person
            print(f"{incident_id}  auto-apply re-check failed ({e.__class__.__name__}: {e}); left for a person")


def print_diagnosis(state) -> None:
    for line in state.get("trail", []):
        print(f"    · {line}")


def diagnose(app: App, incident_id: str) -> None:
    try:
        with heavy_work():  # waits for the nightly eval instead of fighting it for memory
            state = app.diagnoser.diagnose_or_escalate(incident_id)  # a crash escalates, never leaves it stuck
    finally:
        get_langfuse().flush()
    if state is None:
        print(f"diagnosis failed: {incident_id} is escalated (copilot show {incident_id} for the error)")
        return
    print_diagnosis(state)
    print(f"{incident_id} is now {app.repo.get(incident_id).status.value}")


def decide(app: App, incident_id: str, decision: str, note: str) -> None:
    import getpass

    from pipeline_incident_copilot.schemas.remediation import ApprovalDecision

    if app.agent_commands.pending_request(incident_id):  # a fix the Claude Code skill asked for
        try:
            out = app.agent_commands.decide_request(incident_id, decision, by=getpass.getuser(), note=note)
        finally:
            get_langfuse().flush()
        print(json.dumps(out, indent=2, default=str))
        print(f"{incident_id} is now {app.repo.get(incident_id).status.value}")
        return
    try:
        state = app.diagnoser.resume(incident_id, ApprovalDecision(decision=decision, by=getpass.getuser(), note=note))
    except ValueError as e:
        sys.exit(str(e))
    finally:
        get_langfuse().flush()
    print_diagnosis(state)
    r = state.get("remediation")
    if r and r.rerun_run_id:
        print(f"rerun {r.rerun_run_id}: {'SUCCEEDED' if r.succeeded else r.result_state}  {r.run_page_url}")
    print(f"{incident_id} is now {app.repo.get(incident_id).status.value}")


def status(app: App) -> None:
    rows = app.repo.recent(limit=1000)
    by_status: dict[str, list[str]] = {}
    for inc in rows:
        by_status.setdefault(inc.status.value, []).append(inc.incident_id)
    print(f"{len(rows)} incident(s)")
    for name, ids in sorted(by_status.items()):
        print(f"  {name:<18} {len(ids):>3}   {', '.join(ids[:6])}{' …' if len(ids) > 6 else ''}")
    memories = app.memory.repo.all()
    print(f"{len(memories)} memory(ies) from verified fixes; trusted: {sum(app.memory.trusted(m) for m in memories)}")
    for c in app.connections.all():
        state = (
            "paused"
            if not c.enabled
            else "ok"
            if c.last_check_ok
            else "never checked"
            if c.last_check_ok is None
            else "FAILED"
        )
        print(
            f"watcher: {c.connection_id} ({c.platform}, {state}) watching from "
            f"{app.repo.get_state(f'watch_from:{c.connection_id}') or '(never started)'}, "
            f"last check {app.repo.get_state(f'last_poll:{c.connection_id}') or '-'}"
        )
    if by_status.get("awaiting_approval"):
        print("next: approve or reject with the email buttons, the console (copilot serve), or copilot approve INC-x")


def loop(app: App, args) -> None:
    from pipeline_incident_copilot.improvement.nightly import NightlyLoop, NightlyOptions
    from pipeline_incident_copilot.llm import model_router
    from pipeline_incident_copilot.notifications.digest import build_digest, render_text

    if args.loop_command == "digest":
        digest = build_digest(app.repo.conn)
        if args.print:
            print(render_text(digest))
        else:
            from pipeline_incident_copilot.notifications.digest import render_html

            sent = app.notifier.send_message(digest.subject, render_text(digest), render_html(digest))
            print(f"digest {'sent' if sent else 'NOT sent'}: {digest.subject}")
        return
    opts = NightlyOptions(
        quick=args.quick, improve=not args.no_improve, prompts=args.prompts, force=args.force, send=not args.no_email
    )
    result = NightlyLoop(app.repo.conn, app.notifier, text_model=model_router.text).run(opts)
    for note in result.notes:
        print(f"note: {note}")
    for metric, (now, base) in result.scorecard.items():
        print(f"  {metric:<18} {now}  (baseline {base})")
    if result.decision:
        d = result.decision
        print(
            f"decision: {d['verdict']} {d['change']}: {d['reason']}"
            + (f"\n  branch: {d['branch']}" if d["branch"] else "")
        )
    print(f"digest {'sent' if result.digest_sent else 'not sent'}")


def connections(app: App, check: bool) -> None:
    for c in app.connections.all():
        if check and c.enabled:
            try:
                result = app.hub.get(c.connection_id).test()
                ok, message = result.ok, result.message
            except Exception as e:  # noqa: BLE001 — a connector that cannot even be built is a failed check
                ok, message = False, f"{e.__class__.__name__}: {e}"
            app.connections.record_check(c.connection_id, ok, message)
            c = app.connections.get(c.connection_id)
        state = "paused" if not c.enabled else {True: "ok", False: "FAILED", None: "not checked"}[c.last_check_ok]
        print(f"{c.connection_id:<24} {c.platform:<11} {state:<11} {c.name}")
        print(f"    {c.last_check_message or '-'}")
        health = {h.pipeline: h for h in app.connections.health(c.connection_id)}
        for p in c.pipelines:
            h = health.get(p)
            last = (
                f"last {h.last_state} #{h.last_run_id}, {h.runs_24h} runs / {h.failures_24h} failed in 24 h"
                if h
                else "not polled yet"
            )
            print(f"    - {p}: {last}")
    print("add or edit connections at http://localhost:8787/connections (copilot serve)")


def incidents(app: App, status: str | None) -> None:
    rows = app.repo.recent(IncidentStatus(status) if status else None)
    for inc in rows:
        print(
            f"{inc.incident_id}  {inc.status.value:<17} {inc.opened_at:%Y-%m-%d %H:%M}  "
            f"{inc.source or '?':<6} {inc.run_date or '?':<10} {inc.run.failed_task or inc.run.result_state}"
        )
    print(f"{len(rows)} incident(s)")


def show(app: App, incident_id: str) -> None:
    inc = app.repo.get(incident_id)
    if inc is None:
        sys.exit(f"{incident_id} not found")
    ev = app.repo.get_evidence(incident_id)
    print(f"{inc.incident_id}  {inc.status.value}\n  run {inc.run.run_id}  {inc.run.run_page_url}")
    print(f"  failed task: {inc.run.failed_task}   parameters: {inc.run.parameters}")
    if ev:
        print(f"  error: {(ev.error or '').strip().splitlines()[0][:200] if ev.error else '-'}")
        print("  distilled log:\n    " + ev.distilled_log.replace("\n", "\n    "))
        print("  evidence:")
        for r in ev.tool_results:
            print(f"    - {r.tool}: {r.summary}")
        for p in ev.problems:
            print(f"    - (not collected) {p}")
    d = app.repo.latest_diagnosis(incident_id)
    if d:
        rc, res = d.get("root_cause"), d.get("resolution")
        if rc:
            print(f"  root cause ({rc['confidence']:.2f}, {rc['failure_class']}): {rc['cause']}")
            print(f"    based on: {', '.join(rc['evidence_refs'])}")
        if res:
            print(f"  fix: {res['summary']}")
            for i, step in enumerate(res["steps"], 1):
                print(f"    {i}. {step}")
            if res["action"]:
                print(f"    automated action (needs approval): rerun_job {res['action']['parameters']}")
    print("  history:")
    for e in app.repo.events(incident_id):
        print(f"    {e['at']}  {e['kind']}  {e['detail'] or ''}")


def parse_pairs(pairs: list[str]) -> dict[str, str]:
    out = {}
    for pair in pairs:
        if "=" not in pair:
            sys.exit(f"expected key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def agent_command(app: App, args) -> None:
    """Run one subagent command and print its JSON (errors as {"error": …}, exit code 1)."""
    import getpass
    import json
    from pathlib import Path

    from langfuse import propagate_attributes

    from pipeline_incident_copilot.interface.agent_commands import CommandError
    from pipeline_incident_copilot.schemas.skill_report import SkillReport

    cmds = app.agent_commands
    session = getattr(args, "incident_id", None)
    try:
        with get_langfuse().start_as_current_observation(as_type="chain", name=f"skill-{args.command}") as root:
            with propagate_attributes(
                session_id=session, trace_name=f"skill-{args.command}", tags=["claude-skill", args.command]
            ):
                if args.command == "evidence":
                    out = cmds.evidence(args.incident_id)
                elif args.command == "search":
                    out = cmds.search(args.query, args.k)
                elif args.command == "check":
                    out = cmds.check(args.tool, parse_pairs(args.args))
                elif args.command == "record":
                    report = SkillReport.model_validate_json(Path(args.report).read_text())
                    out = cmds.record(args.incident_id, report, args.decision, by=f"{getpass.getuser()} (Claude Code)")
                elif args.command == "check-fix":
                    out = cmds.apply(args.incident_id, parse_pairs(args.reset), by="", dry_run=True)
                    if args.report:  # raises on an invalid report
                        SkillReport.model_validate_json(Path(args.report).read_text())
                        out["report_valid"] = True
                else:  # request-approval: files the fix; a person approves it elsewhere
                    report = SkillReport.model_validate_json(Path(args.report).read_text())
                    out = cmds.request_approval(
                        args.incident_id, parse_pairs(args.reset), report, by=f"{getpass.getuser()} (Claude Code)"
                    )
                root.update(output=out)
    except (CommandError, KeyError, ValueError, OSError) as e:  # pydantic ValidationError is a ValueError
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    finally:
        get_langfuse().flush()
    print(json.dumps(out, indent=2, default=str))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="copilot", description="Pipeline Incident Copilot")
    sub = parser.add_subparsers(dest="command", required=True)
    w = sub.add_parser("watch", help="poll the job for failed runs and open incidents")
    w.add_argument("--once", action="store_true", help="check once and exit")
    w.add_argument("--interval", type=int, default=60, help="seconds between checks")
    w.add_argument("--lookback", type=int, default=50, help="how many recent completed runs to check")
    w.add_argument(
        "--since",
        type=datetime.fromisoformat,
        help="only runs started after this ISO time (default: when the watcher first started)",
    )
    w.add_argument("--diagnose", action="store_true", help="run the agents on each new incident")
    d = sub.add_parser("diagnose", help="run the agents on one incident")
    d.add_argument("incident_id")
    for name, help_text in (("approve", "run the proposed fix and verify it"), ("reject", "reject the fix")):
        a = sub.add_parser(name, help=help_text)
        a.add_argument("incident_id")
        a.add_argument("--note", default="")
    sv = sub.add_parser("serve", help="run the approval page the email buttons open (localhost only)")
    sv.add_argument("--port", type=int, default=8787)
    mm = sub.add_parser("memories", help="list what the copilot has learned")
    mm.add_argument("--consolidate", action="store_true", help="merge memories of the same lesson (one-off)")
    sub.add_parser("status", help="one-screen summary")
    st = sub.add_parser("settings", help="console settings")
    st.add_argument("action", choices=["import-env"], help="import-env: move .env values into the console (names only)")
    lp = sub.add_parser("loop", help="the self-running part: nightly eval + one improvement + digest")
    lsub = lp.add_subparsers(dest="loop_command", required=True)
    ln = lsub.add_parser("nightly", help="scorecard, try one change, branch it if it is better, email the digest")
    ln.add_argument("--quick", action="store_true", help="demo: 4 incidents per split, one run each")
    ln.add_argument("--no-improve", action="store_true", help="scorecard and digest only")
    ln.add_argument("--prompts", action="store_true", help="let the local model propose a prompt rule")
    ln.add_argument("--force", action="store_true", help="run on battery power too (the memory check still applies)")
    ln.add_argument("--no-email", action="store_true", help="do not send the digest")
    ld = lsub.add_parser("digest", help="send the digest for the last 24 h")
    ld.add_argument("--print", action="store_true", help="print instead of sending")
    cn = sub.add_parser("connections", help="connected pipeline platforms and the health of their pipelines")
    cn.add_argument("--check", action="store_true", help="test every enabled connection now")
    lr = sub.add_parser("learn", help="turn an already-resolved incident into a memory")
    lr.add_argument("incident_id")
    n = sub.add_parser("notify", help="re-send the notification for an incident's current status")
    n.add_argument("incident_id")
    i = sub.add_parser("incidents", help="list incidents")
    i.add_argument("--status", choices=[s.value for s in IncidentStatus])
    s = sub.add_parser("show", help="show one incident with its evidence")
    s.add_argument("incident_id")
    ev = sub.add_parser("evidence", help="(JSON) an incident's evidence, for Claude Code subagents")
    ev.add_argument("incident_id")
    se = sub.add_parser("search", help="(JSON) search runbooks, tickets, error docs, code and memory")
    se.add_argument("query")
    se.add_argument("--k", type=int, default=6)
    ck = sub.add_parser("check", help="(JSON) run one read-only check, e.g. check_path_exists path=…")
    ck.add_argument("tool")
    ck.add_argument("args", nargs="*", help="key=value")
    cf = sub.add_parser("check-fix", help="(JSON) would this rerun pass the guardrail? changes nothing")
    cf.add_argument("incident_id")
    cf.add_argument("--reset", nargs="*", default=[], metavar="PARAM=VALUE", help='"" (empty) = job default')
    cf.add_argument("--report", help="also validate the skill's report JSON")
    rc = sub.add_parser("record", help="(JSON) note the skill's outcome in the incident history")
    rc.add_argument("incident_id")
    rc.add_argument("--report", required=True)
    rc.add_argument("--decision", required=True, choices=["deny", "escalate", "report"])
    ap = sub.add_parser("request-approval", help="(JSON) file a guardrail-checked fix for a person to approve")
    ap.add_argument("incident_id")
    ap.add_argument("--reset", nargs="+", required=True, metavar="PARAM=VALUE", help='"" (empty) = job default')
    ap.add_argument("--report", required=True, help="the skill's report JSON")
    args = parser.parse_args(argv)

    if args.command == "serve":
        from pipeline_incident_copilot.interface.approval_server import serve

        return serve(args.port)
    app = App()
    if args.command in ("evidence", "search", "check", "check-fix", "record", "request-approval"):
        return agent_command(app, args)
    if args.command == "watch":
        watch(app, args.once, args.interval, args.lookback, args.since, args.diagnose)
    elif args.command == "diagnose":
        diagnose(app, args.incident_id)
    elif args.command == "settings":
        from pipeline_incident_copilot.integrations.databricks_client import get_client
        from pipeline_incident_copilot.security.env_import import import_env

        def test_databricks(host: str, token: str):
            from pipeline_incident_copilot.connectors.databricks.connector import DatabricksConnector
            from pipeline_incident_copilot.tools.registry import ToolRegistry

            return DatabricksConnector("import", get_client(host, token), ToolRegistry([]), []).test()

        for line in import_env(app.settings, app.connections, app.secrets, test_databricks):
            print(line)
        print("Done. The app now reads these from the console; the matching .env lines can be deleted.")
    elif args.command == "status":
        status(app)
    elif args.command == "loop":
        loop(app, args)
    elif args.command == "connections":
        connections(app, args.check)
    elif args.command == "memories":
        if args.consolidate:
            from pipeline_incident_copilot.rag.vector_store.qdrant_store import release_qdrant

            try:
                merged = app.memory.consolidate(lambda cid: (c := app.connections.get(cid)) and c.platform)
            finally:
                release_qdrant()
            print("merged: " + (", ".join(f"memory:{o} → memory:{k}" for k, o in merged) or "nothing to merge"))
        for m in app.memory.repo.all():
            trust = "trusted" if app.memory.trusted(m) else "NOT trusted"
            print(
                f"memory:{m['id']}  seen {m['seen_count']}×  worked {m['success_count']} / failed {m['failure_count']}"
                f"  ({trust})  {m['pattern']}  ← {', '.join(m['incident_ids'])}"
            )
            print("    " + m["text"].replace("\n", "\n    "))
    elif args.command == "learn":
        from pipeline_incident_copilot.schemas.incident import IncidentStatus as Status

        if app.repo.get(args.incident_id).status is not Status.RESOLVED:
            sys.exit(f"{args.incident_id} is not resolved: only verified fixes become memories")
        m = app.memory.record(app.diagnoser.saved_state(args.incident_id))
        app.repo.add_event(args.incident_id, "learned", f"memory:{m['id']} (backfilled)")
        print(f"{args.incident_id} → memory:{m['id']} (seen {m['seen_count']}×)")
    elif args.command == "notify":
        delivered = app.diagnoser.renotify(args.incident_id)
        print(f"{args.incident_id}: {'sent' if delivered else 'NOT sent'} via {type(app.notifier).__name__}")
    elif args.command in ("approve", "reject"):
        decide(app, args.incident_id, args.command, args.note)
    elif args.command == "incidents":
        incidents(app, args.status)
    else:
        show(app, args.incident_id)


if __name__ == "__main__":
    main()
