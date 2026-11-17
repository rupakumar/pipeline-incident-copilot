"""HTML email for incident events: every detail, plus Approve / Deny buttons when a fix awaits approval.

Email-safe: table layout and inline styles only (no CSS classes, scripts or external assets),
because Gmail and Outlook strip most of the rest. The plain-text version is always sent alongside.
"""

from html import escape

from pipeline_incident_copilot.connectors.catalog import platform_label
from pipeline_incident_copilot.notifications.base import EventKind, IncidentEvent
from pipeline_incident_copilot.notifications.templates import is_auto_fixed

INK, MUTED, RULE, BG, CARD = "#1A2433", "#556172", "#D3DBD2", "#F2F5F1", "#FFFFFF"
BLUE, GREEN, RED, AMBER = "#2F6690", "#1F7A63", "#B42318", "#8A6A12"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "SFMono-Regular,Menlo,Consolas,monospace"

HEADLINES = {
    EventKind.INCIDENT_OPENED: ("Investigating", BLUE, "A pipeline run failed. The copilot is investigating."),
    EventKind.DIAGNOSIS_READY: ("Approval needed", AMBER, "The copilot found the cause and proposes a fix."),
    EventKind.RESOLVED: ("Resolved", GREEN, "The approved fix ran and the pipeline succeeded."),
    EventKind.ESCALATED: ("Needs a person", RED, "The copilot cannot fix this safely on its own."),
}


def _row(label: str, value: str) -> str:
    return (
        f'<tr><td style="padding:4px 12px 4px 0;color:{MUTED};font-size:13px;white-space:nowrap;vertical-align:top">'
        f'{escape(label)}</td><td style="padding:4px 0;color:{INK};font-size:14px">{value}</td></tr>'
    )


def _section(title: str, inner: str) -> str:
    return (
        f'<tr><td style="padding:18px 24px 0"><div style="font-size:12px;letter-spacing:.06em;text-transform:uppercase;'
        f'color:{MUTED};font-weight:600;margin-bottom:6px">{escape(title)}</div>{inner}</td></tr>'
    )


def _code(text: str) -> str:
    return f'<code style="font-family:{MONO};font-size:12.5px;background:{BG};padding:1px 5px;border-radius:4px">{escape(text)}</code>'


def _button(label: str, url: str, color: str) -> str:
    return (
        f'<a href="{escape(url, quote=True)}" style="display:inline-block;background:{color};color:#FFFFFF;'
        f'text-decoration:none;font-weight:600;font-size:15px;padding:12px 26px;border-radius:8px;margin:0 8px 8px 0">'
        f"{escape(label)}</a>"
    )


SIGNAL_TEXT = {  # learning/confidence_calibrator.py signals, in words a reviewer can check
    "explained_by_change": "The cause names what changed since the last good run",
    "confirmed_by_check": "A live check confirmed the cause",
    "exact_error_doc": "The error's own documentation was found",
    "knowledge_ok": "Runbooks and past incidents matched well",
}
AUTO_APPLY_AT, ROUTE_AT = 0.85, 0.70  # learning/auto_approval.AUTO_CONFIDENCE, the routing threshold


def confidence_band(score: float) -> tuple[str, str, str]:
    """(label, colour, what it means) for a calibrated confidence."""
    if score >= AUTO_APPLY_AT:
        return "High", GREEN, "high enough to auto-apply a known fix"
    if score >= ROUTE_AT:
        return "Medium", AMBER, "sure enough to propose a fix; a person decides"
    return "Low", RED, "too unsure to propose a fix; escalated to a person"


def _confidence(conf: dict | None, rc, auto: dict | None) -> str:
    """The fix's confidence: calibrated score with a bar, its band, the four evidence checks, the known-fix record."""
    model = (conf or {}).get("model", rc.confidence if rc else None)
    score = (conf or {}).get("calibrated") or model
    if score is None:
        return ""
    label, color, meaning = confidence_band(score)
    pct = max(2, min(100, round(score * 100)))
    bar = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:8px 0 2px">'
        f'<tr><td width="{pct}%" style="background:{color};height:8px;border-radius:4px 0 0 4px;font-size:0">&nbsp;</td>'
        f'<td style="background:{RULE};height:8px;border-radius:0 4px 4px 0;font-size:0">&nbsp;</td></tr></table>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="font-size:11px;color:{MUTED}">0</td>'
        f'<td align="right" style="font-size:11px;color:{MUTED}">propose ≥ 70 · auto-apply ≥ 85</td></tr></table>'
    )
    head = (
        f'<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="font-size:32px;font-weight:700;color:{color};padding-right:12px;line-height:1">{score:.0%}</td>'
        f'<td style="font-size:13px;color:{INK};line-height:1.4"><b>{label} confidence</b><br>'
        f'<span style="color:{MUTED}">{escape(meaning)}</span></td></tr></table>'
    )
    signals = (conf or {}).get("signals") or {}
    checks = "".join(
        f'<tr><td style="padding:2px 8px 2px 0;font-size:14px;color:{GREEN if ok else RED};font-weight:700">'
        f'{"✓" if ok else "✗"}</td><td style="padding:2px 0;font-size:13px;color:{INK}">'
        f"{escape(SIGNAL_TEXT.get(name, name.replace('_', ' ')))}</td></tr>"
        for name, ok in signals.items()
    )
    parts = [head, bar]
    if checks:
        parts.append(
            f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:8px">{checks}</table>'
        )
    how = f"model's own score {model:.0%}" if model is not None else ""
    if signals:
        how += f" · calibrated with {sum(signals.values())} of {len(signals)} evidence checks (40% model, 60% checks)"
    if how:
        parts.append(f'<div style="font-size:12px;color:{MUTED};margin-top:6px">{escape(how)}</div>')
    if auto and auto.get("memory_id"):
        ids = ", ".join(auto.get("from_incidents", [])[-3:])
        verdict = (
            "applied automatically" if auto.get("approved") else f"not applied automatically: {auto.get('reason')}"
        )
        parts.append(
            f'<div style="margin-top:10px;padding:8px 12px;background:{BG};border-radius:8px;font-size:13px;color:{INK}">'
            f"<b>Known fix</b>: worked {auto.get('successes', 0)}× before ({escape(ids)}), never failed · "
            f"{escape(verdict)}</div>"
        )
    return "".join(parts)


def render_html(
    event: IncidentEvent, approve_url: str | None = None, deny_url: str | None = None, approver: str | None = None
) -> str:
    inc, run, ev = event.incident, event.incident.run, event.evidence
    label, color, intro = HEADLINES.get(event.kind, ("Update", BLUE, ""))
    if is_auto_fixed(event):
        label, intro = "Auto-fixed", "A known fix that worked before ran without approval and the pipeline succeeded."
    rows = [
        _row("Incident", f"<b>{escape(inc.incident_id)}</b> · {escape(inc.status.value.replace('_', ' '))}"),
        _row(
            "Pipeline",
            f"{escape(platform_label(run.platform))} · {escape(run.job_name)} · source {escape(inc.source or '?')}"
            f" · run_date {escape(inc.run_date or '?')}",
        ),
        _row("Failed task", _code(run.failed_task or run.result_state)),
    ]
    if ev and ev.error:
        rows.append(_row("Error", _code(ev.error.strip().splitlines()[0][:240])))
    if run.run_page_url:
        rows.append(
            _row(
                "Failed run",
                f'<a href="{escape(run.run_page_url, quote=True)}" style="color:{BLUE}">open in {escape(platform_label(run.platform))}</a>',
            )
        )
    sections = [
        _section("What happened", f'<table role="presentation" cellpadding="0" cellspacing="0">{"".join(rows)}</table>')
    ]

    rc = event.root_cause
    if rc:
        refs = ", ".join(_code(r) for r in rc.evidence_refs[:3])
        sections.append(
            _section(
                "Root cause",
                f'<div style="font-size:15px;color:{INK};line-height:1.5">{escape(rc.cause)}</div>'
                f'<div style="font-size:13px;color:{MUTED};margin-top:4px">confidence {rc.confidence:.0%} · '
                f"class {escape(rc.failure_class.replace('_', ' '))}{' · based on ' + refs if refs else ''}</div>",
            )
        )
    res = event.resolution
    if res:
        steps = "".join(f'<li style="margin:3px 0">{escape(s)}</li>' for s in res.steps)
        fix = f'<div style="font-size:15px;color:{INK}">{escape(res.summary)}</div><ol style="margin:8px 0 0 18px;padding:0;color:{INK};font-size:14px">{steps}</ol>'
        if res.action:
            params = "".join(
                _row(k, _code(v if v != "" else "(reset to default)")) for k, v in res.action.parameters.items()
            )
            fix += (
                f'<div style="margin-top:12px;padding:12px 14px;border:1px solid {RULE};border-radius:8px;background:{BG}">'
                f'<div style="font-size:13px;color:{MUTED};margin-bottom:4px">Automated action if you approve: '
                f'<b style="color:{INK}">rerun {escape(run.job_name)}</b> with</div>'
                f'<table role="presentation" cellpadding="0" cellspacing="0">{params}</table>'
                f'<div style="font-size:13px;color:{MUTED};margin-top:4px">risk {escape(res.risk)} · '
                f"verify: {escape('; '.join(res.verify))}</div></div>"
            )
        elif res.action_rejected_reason:
            fix += f'<div style="font-size:13px;color:{MUTED};margin-top:8px">No automated action: {escape(res.action_rejected_reason)}</div>'
        sections.append(_section("Proposed fix", fix))
    if rc and res and res.action and (block := _confidence(event.confidence, rc, event.auto_approval)):
        sections.append(_section("Confidence in this fix", block))
    r = event.remediation
    if r and r.rerun_run_id:
        link = (
            f' · <a href="{escape(r.run_page_url or "", quote=True)}" style="color:{BLUE}">open rerun</a>'
            if r.run_page_url
            else ""
        )
        sections.append(
            _section(
                "Rerun",
                f'<div style="font-size:14px;color:{INK}">run {r.rerun_run_id}: '
                f"<b>{'succeeded' if r.succeeded else escape(r.result_state or 'running')}</b>{link}</div>",
            )
        )
    if ev and ev.tool_results:
        items = "".join(
            f'<li style="margin:3px 0"><b>{escape(t.tool)}</b>: {escape(t.summary[:260])}</li>' for t in ev.tool_results
        )
        sections.append(
            _section(
                "Evidence checked", f'<ul style="margin:0 0 0 18px;padding:0;color:{INK};font-size:13px">{items}</ul>'
            )
        )

    if approve_url and deny_url and res and res.action:
        sections.append(
            f'<tr><td style="padding:22px 24px 6px">{_button("Approve fix", approve_url, GREEN)}'
            f"{_button('Deny', deny_url, RED)}"
            f'<div style="font-size:12px;color:{MUTED};margin-top:6px;line-height:1.5">Each button opens a confirmation '
            f"page on the Mac running <code>copilot serve</code>; nothing changes until you confirm there. Links work "
            f"once and expire in 24 hours. Or run <code>copilot approve {escape(inc.incident_id)}</code>."
            + (
                f" Sent to you ({escape(approver)}) as an approver; your decision is recorded under your name."
                if approver
                else ""
            )
            + "</div></td></tr>"
        )

    return f"""<!doctype html><html><body style="margin:0;padding:0;background:{BG}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BG};padding:24px 12px;font-family:{FONT}">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:620px;background:{CARD};border:1px solid {RULE};border-radius:12px">
<tr><td style="padding:22px 24px 0">
<span style="display:inline-block;background:{color};color:#FFFFFF;font-size:12px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;padding:4px 10px;border-radius:999px">{escape(label)}</span>
<div style="font-size:20px;font-weight:700;color:{INK};margin-top:10px">{escape(inc.incident_id)} · {escape(run.job_name)}: {escape(run.failed_task or run.result_state)} failed</div>
<div style="font-size:14px;color:{MUTED};margin-top:4px">{escape(intro)}</div>
</td></tr>
{"".join(sections)}
<tr><td style="padding:20px 24px 22px;font-size:12px;color:{MUTED};border-top:1px solid {RULE}">Pipeline Incident Copilot · sent automatically · every action needs a person's approval</td></tr>
</table></td></tr></table></body></html>"""
