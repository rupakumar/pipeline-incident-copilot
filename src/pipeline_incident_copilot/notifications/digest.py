# ruff: noqa: E501 — email layout strings read better unwrapped
"""Nightly digest: what the copilot did in the last 24 h, how it scores, and what it tried to improve.

Sections: incidents (opened, resolved by a person or auto-fixed, escalated, waiting), connections and the watcher
heartbeat, the scorecard (tonight's eval vs the versioned baseline), self-improvement decisions, learned fixes.
Plain text + HTML, sent through the same notifier as incident emails.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html import escape

from pipeline_incident_copilot.notifications.html_templates import AMBER, BLUE, FONT, GREEN, INK, MUTED, RED, RULE

STALE_WATCHER_MINUTES = 10
SCORE_KEYS = ("all_ok", "class_ok", "fact_ok", "action_ok", "context_recall", "context_precision", "faithfulness",
              "brier_calibrated", "escalated", "mean_seconds")  # fmt: skip


@dataclass
class Digest:
    since: datetime
    opened: list[dict] = field(default_factory=list)
    resolved: list[dict] = field(default_factory=list)
    escalated: list[dict] = field(default_factory=list)
    waiting: list[dict] = field(default_factory=list)
    connections: list[dict] = field(default_factory=list)
    watcher_minutes: float | None = None
    scorecard: dict = field(default_factory=dict)  # metric -> (tonight, baseline)
    decisions: list[dict] = field(default_factory=list)
    memories: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)  # e.g. "nightly eval skipped: on battery"

    @property
    def subject(self) -> str:
        auto = sum(1 for r in self.resolved if r["auto"])
        accepted = sum(1 for d in self.decisions if d["verdict"] == "accepted")
        parts = [
            f"{len(self.opened)} new",
            f"{len(self.resolved)} resolved ({auto} auto)",
            f"{len(self.escalated)} escalated",
        ]
        if accepted:
            parts.append(f"{accepted} improvement ready to merge")
        if self.watcher_minutes is None or self.watcher_minutes > STALE_WATCHER_MINUTES:
            parts.append("WATCHER NOT POLLING")
        return f"[copilot digest {datetime.now(UTC):%Y-%m-%d}] " + ", ".join(parts)


def build_digest(conn: sqlite3.Connection, hours: int = 24, scorecard: dict | None = None, notes=()) -> Digest:
    since = datetime.now(UTC) - timedelta(hours=hours)
    s = since.isoformat(timespec="seconds")
    q = lambda sql, *a: [dict(r) for r in conn.execute(sql, a).fetchall()]  # noqa: E731
    d = Digest(since=since, scorecard=scorecard or {}, notes=list(notes))
    d.opened = q("SELECT incident_id, platform, pipeline, failed_task, status FROM incidents WHERE opened_at >= ?"
                 " ORDER BY seq", s)  # fmt: skip
    d.resolved = q(
        "SELECT e.incident_id, i.platform, i.pipeline, e.detail,"
        " EXISTS (SELECT 1 FROM incident_events a WHERE a.incident_id = e.incident_id AND a.kind = 'auto_approved')"
        " AS auto FROM incident_events e JOIN incidents i USING (incident_id)"
        " WHERE e.kind = 'status:resolved' AND e.at >= ? ORDER BY e.id",
        s,
    )
    d.escalated = q(
        "SELECT e.incident_id, i.platform, i.pipeline, e.detail FROM incident_events e JOIN incidents i"
        " USING (incident_id) WHERE e.kind = 'status:escalated' AND e.at >= ? ORDER BY e.id",
        s,
    )
    d.waiting = q("SELECT incident_id, platform, pipeline, updated_at FROM incidents WHERE status = 'awaiting_approval'"
                  " ORDER BY seq")  # fmt: skip
    d.connections = q("SELECT connection_id, platform, enabled, last_check_ok, last_check_message FROM connections")
    last = conn.execute("SELECT max(value) FROM watcher_state WHERE key LIKE 'last_poll:%'").fetchone()[0]
    if last:
        d.watcher_minutes = round((datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds() / 60, 1)
    d.decisions = q("SELECT at, kind, change, verdict, reason, branch FROM improvement_decisions WHERE at >= ?"
                    " ORDER BY id", s)  # fmt: skip
    m = conn.execute("SELECT count(*), sum(failure_count = 0 AND fix_successes >= 2) FROM memories").fetchone()
    d.memories = {"total": m[0], "auto_eligible": m[1] or 0}
    return d


def _fmt(v) -> str:
    return "-" if v is None else f"{v:g}" if isinstance(v, float) else str(v)


def render_text(d: Digest) -> str:
    lines = [d.subject, ""]
    lines += [f"Note: {n}" for n in d.notes]
    lines += ["", f"INCIDENTS since {d.since:%Y-%m-%d %H:%M} UTC"]
    lines += [
        f"  new        {len(d.opened)}: "
        + ", ".join(f"{r['incident_id']} ({r['platform']} {r['pipeline']})" for r in d.opened)
    ]
    lines += [
        f"  resolved   {r['incident_id']} {r['pipeline']}{' — AUTO-FIXED (known fix)' if r['auto'] else ''}"
        for r in d.resolved
    ]
    lines += [f"  escalated  {r['incident_id']} {r['pipeline']}: {(r['detail'] or '')[:120]}" for r in d.escalated]
    lines += [f"  waiting    {r['incident_id']} {r['pipeline']} (since {r['updated_at'][:16]})" for r in d.waiting]
    lines += ["", "CONNECTIONS"]
    watcher = "never polled" if d.watcher_minutes is None else f"last poll {d.watcher_minutes:g} min ago"
    lines += [
        f"  watcher: {watcher}"
        + ("  ← NOT POLLING" if d.watcher_minutes is None or d.watcher_minutes > STALE_WATCHER_MINUTES else "")
    ]
    lines += [
        f"  {c['connection_id']} ({c['platform']}): {'paused' if not c['enabled'] else 'ok' if c['last_check_ok'] else 'CHECK FAILED'} {c['last_check_message'] or ''}"[
            :160
        ]
        for c in d.connections
    ]
    if d.scorecard:
        lines += ["", "SCORECARD (tonight vs accepted baseline, holdout incidents)"]
        lines += [f"  {k:<18} {_fmt(now):>8}  baseline {_fmt(base):>8}" for k, (now, base) in d.scorecard.items()]
    lines += ["", "SELF-IMPROVEMENT"]
    lines += [
        f"  {x['verdict'].upper():<9} {x['change']}: {x['reason']}"
        + (f"\n            branch {x['branch']} (review, then merge)" if x["branch"] else "")
        for x in d.decisions
    ] or ["  no change tried"]
    lines += [
        "",
        f"LEARNED FIXES: {d.memories.get('total', 0)} ({d.memories.get('auto_eligible', 0)} eligible for auto-apply)",
    ]
    return "\n".join(lines) + "\n\n— Pipeline Incident Copilot\n"


def render_html(d: Digest) -> str:
    def section(title: str, body: str) -> str:
        return (f'<h2 style="font-size:15px;margin:22px 0 8px;color:{INK}">{escape(title)}</h2>'
                f'<div style="background:#fff;border:1px solid {RULE};border-radius:10px;padding:10px 14px">{body}</div>')  # fmt: skip

    def row(left: str, right: str, color: str = INK) -> str:
        return (f'<div style="display:flex;gap:10px;padding:4px 0;border-top:1px solid {RULE}">'
                f'<span style="min-width:110px;font-weight:600;color:{color}">{left}</span><span>{right}</span></div>')  # fmt: skip

    inc = "".join(
        [row("new", escape(", ".join(f"{r['incident_id']} {r['pipeline']}" for r in d.opened) or "none"), BLUE)]
        + [
            row("auto-fixed" if r["auto"] else "resolved", escape(f"{r['incident_id']} {r['pipeline']}"), GREEN)
            for r in d.resolved
        ]
        + [
            row("escalated", escape(f"{r['incident_id']} {r['pipeline']}: {(r['detail'] or '')[:140]}"), RED)
            for r in d.escalated
        ]
        + [row("waiting", escape(f"{r['incident_id']} {r['pipeline']}"), AMBER) for r in d.waiting]
    )
    stale = d.watcher_minutes is None or d.watcher_minutes > STALE_WATCHER_MINUTES
    conns = row(
        "watcher",
        escape("never polled" if d.watcher_minutes is None else f"last poll {d.watcher_minutes:g} min ago"),
        RED if stale else GREEN,
    )
    conns += "".join(
        row(
            escape(c["connection_id"]),
            escape(c["last_check_message"] or ""),
            GREEN if c["last_check_ok"] else RED if c["enabled"] else MUTED,
        )
        for c in d.connections
    )
    score = "".join(
        row(escape(k), escape(f"{_fmt(now)}  (baseline {_fmt(base)})")) for k, (now, base) in d.scorecard.items()
    )
    imp = "".join(
        row(
            escape(x["verdict"]),
            escape(f"{x['change']}: {x['reason']}")
            + (f"<br><code>{escape(x['branch'])}</code> ready to review and merge" if x["branch"] else ""),
            GREEN if x["verdict"] == "accepted" else MUTED,
        )
        for x in d.decisions
    ) or row("none", "no change tried tonight", MUTED)
    notes = "".join(f'<p style="color:{AMBER};font-weight:600">{escape(n)}</p>' for n in d.notes)
    return (
        f'<div style="font-family:{FONT};color:{INK};max-width:680px;margin:0 auto;padding:20px 16px;background:#F2F5F1">'
        f'<div style="font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:{BLUE};font-weight:700">Pipeline Incident Copilot · nightly digest</div>'
        f'<h1 style="font-size:20px;margin:6px 0 10px">{escape(d.subject)}</h1>{notes}'
        + section("Incidents (last 24 h)", inc)
        + section("Connections", conns)
        + (section("Scorecard: holdout incidents vs accepted baseline", score) if score else "")
        + section("Self-improvement", imp)
        + section(
            "Learned fixes",
            escape(
                f"{d.memories.get('total', 0)} fixes remembered, {d.memories.get('auto_eligible', 0)} eligible for auto-apply"
            ),
        )
        + "</div>"
    )
