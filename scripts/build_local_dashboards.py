# ruff: noqa: E501 — SQL and Grafana JSON literals read better unwrapped
"""Generate the local Grafana dashboards (infra/grafana/local/dashboards/*.json) from Python.

The JSON files are committed (Grafana provisions them); edit this script and re-run it instead of editing the JSON:
    python -m scripts.build_local_dashboards && python -m scripts.local_grafana --render-only
Placeholders (__APPROVAL_CONSOLE_URL__, ...) are filled per machine by scripts/local_grafana.py.

Dashboards: Incident Command (needs a person now, approval queue, escalations, lifecycle, confidence, learned fixes), Ticket detail,
Connections (every connected platform and the health of each watched pipeline).
"""

import json

from pipeline_incident_copilot.config import PROJECT_ROOT

OUT = PROJECT_ROOT / "infra" / "grafana" / "local" / "dashboards"
DS = {"type": "frser-sqlite-datasource", "uid": "copilot-sqlite"}
CONSOLE = "__APPROVAL_CONSOLE_URL__"
LANGFUSE = "__LANGFUSE_PROJECT_URL__"
HEALTH = "__PIPELINE_HEALTH_URL__"
DETAIL_UID = "copilot-ticket-detail"
DESK_UID = "copilot-incident-desk"
CONNECTIONS_UID = "copilot-connections"
PLATFORM_NAMES = {
    "databricks": "Databricks",
    "jenkins": "Jenkins",
    "snowflake": "Snowflake",
    "aws_glue": "AWS Glue",
    "azure_data_factory": "Azure Data Factory",
}
PLATFORM_SQL = "CASE {col} " + " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in PLATFORM_NAMES.items()) + " ELSE {col} END"
# who approved or denied: "auto · known fix" for the auto-apply policy, else the person (and where they clicked)
DECIDED_BY = """CASE WHEN EXISTS (SELECT 1 FROM incident_events a WHERE a.incident_id = i.incident_id AND a.kind = 'auto_approved')
       THEN 'auto · known fix'
       ELSE coalesce((SELECT CASE WHEN a.kind = 'approval:reject' THEN 'denied · ' ELSE '' END
                             || substr(a.detail, 4, instr(a.detail || ':', ':') - 4)
                      FROM incident_events a WHERE a.incident_id = i.incident_id AND a.kind LIKE 'approval:%'
                      ORDER BY a.id DESC LIMIT 1), '-') END"""
# the desk's filters: "All" is sent as __all
FILTER = "('${platform}' = '__all' OR i.platform = '${platform}') AND ('${app}' = '__all' OR i.pipeline = '${app}')"

BLUE, AMBER, RED, GREEN, GREY, VIOLET = "#1846C9", "#C27A00", "#D52B1E", "#00843D", "#8C96A5", "#8E2DA8"
TEAL = "#00838F"
STATUS = {
    "open": ("open", BLUE),
    "investigating": ("investigating", BLUE),
    "awaiting_approval": ("awaiting approval", AMBER),
    "remediating": ("rerun running", BLUE),
    "resolved": ("resolved", GREEN),
    "escalated": ("escalated", RED),
    "rejected": ("rejected", GREY),
    "closed": ("closed", GREY),
}
STATUS_MAP = [
    {
        "type": "value",
        "options": {k: {"text": t, "color": c, "index": i} for i, (k, (t, c)) in enumerate(STATUS.items())},
    }
]

LATEST = "latest AS (SELECT incident_id, max(id) AS id FROM diagnoses GROUP BY incident_id)"
WAITING = "i.status = 'awaiting_approval' AND json_extract(d.diagnosis_json, '$.resolution.action') IS NOT NULL"


def target(sql: str) -> dict:
    return {
        "refId": "A",
        "datasource": DS,
        "queryType": "table",
        "rawQueryText": sql,
        "queryText": sql,
        "timeColumns": [],
    }


def override(name: str, *props) -> dict:
    return {"matcher": {"id": "byName", "options": name}, "properties": [{"id": k, "value": v} for k, v in props]}


def button(label: str, color: str, decision: str, ticket_ref: str) -> dict:
    return override(
        label,
        ("mappings", [{"type": "value", "options": {label: {"text": label, "color": color, "index": 0}}}]),
        ("custom.cellOptions", {"type": "color-background", "mode": "basic"}),
        ("custom.align", "center"),
        ("custom.width", 96),
        (
            "links",
            [
                {
                    "title": f"{label}: opens the confirm page",
                    "url": f"{CONSOLE}/review/{ticket_ref}/{decision}",
                    "targetBlank": True,
                }
            ],
        ),
    )


def table(pid, title, sql, grid, overrides=(), description="", sort=None):
    p = {
        "id": pid,
        "type": "table",
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": grid,
        "targets": [target(sql)],
        "fieldConfig": {
            "defaults": {
                "custom": {"align": "left", "cellOptions": {"type": "auto", "wrapText": True}, "inspect": True},
                "color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute", "steps": [{"color": "transparent", "value": None}]},
            },
            "overrides": list(overrides),
        },
        "options": {"showHeader": True, "cellHeight": "md", "footer": {"show": False}},
    }
    if sort:
        p["options"]["sortBy"] = [{"displayName": sort, "desc": True}]
    return p


def dashboard(uid, title, panels, links, templating=None, description=""):
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": ["pipeline-incident-copilot"],
        "timezone": "utc",
        "editable": False,
        "graphTooltip": 0,
        "refresh": "10s",
        "time": {"from": "now-30d", "to": "now"},
        "timepicker": {"hidden": True, "refresh_intervals": ["5s", "10s", "30s", "1m"]},
        "links": links,
        "templating": {"list": templating or []},
        "panels": panels,
        "schemaVersion": 39,
        "version": 1,
    }


# ------------------------------------------------------------------ Incident Command (the desk)
# One question per row, most urgent at the top left (Grafana dashboard best practices): what needs a person now,
# the approval queue, escalations, where tickets are, whether the confidence can be trusted, what was learned.
CALIBRATED = "coalesce(json_extract(d.diagnosis_json, '$.confidence.calibrated'), json_extract(d.diagnosis_json, '$.root_cause.confidence'))"
BAND = f"""CASE WHEN {CALIBRATED} >= 0.85 THEN 'auto-fix eligible' WHEN {CALIBRATED} >= 0.7 THEN 'needs approval'
       WHEN {CALIBRATED} IS NULL THEN '-' ELSE 'escalate' END"""
SIGNAL_NAMES = ("explained_by_change", "confirmed_by_check", "exact_error_doc", "knowledge_ok")
# the four evidence checks as ✓ / ✗ in a fixed order; · when the diagnosis predates them being recorded
CHECKS = " || ".join(
    f"CASE json_extract(d.diagnosis_json, '$.confidence.signals.{s}') WHEN 1 THEN '✓' WHEN 0 THEN '✗' ELSE '·' END"
    for s in SIGNAL_NAMES
)
KNOWN_FIX = """CASE WHEN json_extract(d.diagnosis_json, '$.auto_approval.memory_id') IS NULL THEN 'none on record'
       ELSE 'worked ' || json_extract(d.diagnosis_json, '$.auto_approval.successes') || '×, failed '
            || json_extract(d.diagnosis_json, '$.auto_approval.failures') || '×' END"""
CLASS_NAMES = {
    "permissions": ("permissions", VIOLET),
    "data_quality": ("data quality", AMBER),
    "resource_exhaustion": ("out of memory", RED),
    "missing_input": ("missing input", BLUE),
    "schema_change": ("schema change", TEAL),
}
CLASS_MAP = [
    {
        "type": "value",
        "options": {k: {"text": t, "color": c, "index": i} for i, (k, (t, c)) in enumerate(CLASS_NAMES.items())},
    }
]
CONFIDENCE_STEPS = {
    "mode": "absolute",
    "steps": [{"color": RED, "value": None}, {"color": AMBER, "value": 0.7}, {"color": GREEN, "value": 0.85}],
}
TICKET_LINK = (
    "links",
    [{"title": "Ticket details", "url": f"/d/{DETAIL_UID}/ticket-detail?var-incident=${{__value.raw}}"}],
)
LAST_30D = "i.opened_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-30 days')"
LAST_7D = "i.opened_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 days')"


def stat(pid, title, sql, grid, color, description="", unit=None, decimals=0, no_value="0"):
    defaults = {"color": {"mode": "fixed", "fixedColor": color}, "decimals": decimals, "noValue": no_value}
    if unit:
        defaults["unit"] = unit
    return {
        "id": pid,
        "type": "stat",
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": grid,
        "targets": [target(sql)],
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "value",
            "graphMode": "none",
            "textMode": "value",
            "justifyMode": "center",
            "orientation": "auto",
            "wideLayout": True,
        },
        "fieldConfig": {"defaults": defaults, "overrides": []},
    }


def bars(pid, title, sql, grid, color, description="", unit=None, decimals=0, max_value=None):
    defaults = {"color": {"mode": "fixed", "fixedColor": color}, "min": 0, "decimals": decimals}
    if unit:
        defaults["unit"] = unit
    if max_value is not None:
        defaults["max"] = max_value
    return {
        "id": pid,
        "type": "bargauge",
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": grid,
        "targets": [target(sql)],
        "transformations": [
            {
                "id": "rowsToFields",
                "options": {
                    "mappings": [
                        {"fieldName": "name", "handlerKey": "field.name"},
                        {"fieldName": "value", "handlerKey": "field.value"},
                    ]
                },
            }
        ],
        "options": {
            "orientation": "horizontal",
            "displayMode": "basic",
            "showUnfilled": True,
            "valueMode": "text",
            "namePlacement": "top",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
        "fieldConfig": {"defaults": defaults, "overrides": []},
    }


def row(pid, title, y, collapsed=False, panels=None):
    r = {"id": pid, "type": "row", "title": title, "collapsed": collapsed, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}}
    r["panels"] = panels or []
    return r


def median_seconds(start_kind_sql: str, end_kinds: str) -> str:
    """Median seconds from the incident opening to the first event of `end_kinds`, tickets of the last 30 days."""
    return f"""WITH t AS (
  SELECT (julianday(e.at) - julianday(i.opened_at)) * 86400 AS s
  FROM incidents i JOIN (SELECT incident_id, min(at) AS at FROM incident_events WHERE kind IN ({end_kinds}) GROUP BY incident_id) e
    USING (incident_id)
  WHERE {start_kind_sql} AND {FILTER})
SELECT s AS "value" FROM t ORDER BY s LIMIT 1 OFFSET (SELECT count(*) / 2 FROM t)"""


# --- row 1: needs a person now -----------------------------------------------------------------------------
now_row = row(100, "Needs a person now", 0)
awaiting = stat(
    101,
    "Awaiting approval",
    f"SELECT count(*) AS value FROM incidents i WHERE status = 'awaiting_approval' AND {FILTER}",
    {"x": 0, "y": 1, "w": 4, "h": 4},
    AMBER,
    "Fixes waiting for a person: approve or deny them in the queue below.",
)
escalated_now = stat(
    102,
    "Escalated, open",
    f"SELECT count(*) AS value FROM incidents i WHERE status = 'escalated' AND {FILTER}",
    {"x": 4, "y": 1, "w": 4, "h": 4},
    RED,
    "The copilot stopped and handed these to a person (no safe fix, low confidence, or the fix failed). Close them once handled.",
)
fixing = stat(
    103,
    "In progress",
    f"SELECT count(*) AS value FROM incidents i WHERE status IN ('open', 'investigating', 'remediating') AND {FILTER}",
    {"x": 8, "y": 1, "w": 4, "h": 4},
    BLUE,
    "Being diagnosed, or an approved rerun is running.",
)
auto_fixed = stat(
    104,
    "Auto-fixed · 7 days",
    f"""SELECT count(DISTINCT e.incident_id) AS value FROM incident_events e JOIN incidents i USING (incident_id)
WHERE e.kind = 'auto_approved' AND e.at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 days') AND {FILTER}""",
    {"x": 12, "y": 1, "w": 4, "h": 4},
    VIOLET,
    "Known fixes the copilot applied on its own (same failure, same fix, worked at least twice, confidence ≥ 0.85, switched on for the connection).",
)
time_to_diagnosis = stat(
    105,
    "Median time to diagnosis · 7 days",
    median_seconds(LAST_7D, "'status:awaiting_approval', 'status:escalated'"),
    {"x": 16, "y": 1, "w": 4, "h": 4},
    GREY,
    "Failure detected → fix proposed or escalated. Median of tickets opened in the last 7 days (the slow tail does not hide the typical case).",
    unit="s",
    decimals=0,
    no_value="-",
)
time_to_fix = stat(
    106,
    "Median time to fix · 7 days",
    median_seconds(LAST_7D, "'status:resolved'"),
    {"x": 20, "y": 1, "w": 4, "h": 4},
    GREEN,
    "Failure detected → rerun verified, including the wait for approval. Median of tickets opened in the last 7 days.",
    unit="s",
    decimals=0,
    no_value="-",
)

# --- row 2: approval queue ----------------------------------------------------------------------------------
queue_row = row(110, "Approval queue", 5)
queue_sql = f"""WITH {LATEST}
SELECT
  i.incident_id AS "Ticket",
  coalesce(i.pipeline, '?') AS "Pipeline",
  coalesce(json_extract(d.diagnosis_json, '$.root_cause.failure_class'), '-') AS "Class",
  json_extract(d.diagnosis_json, '$.root_cause.cause') AS "Cause",
  'rerun ' || json_extract(d.diagnosis_json, '$.resolution.action.parameters') AS "Proposed fix",
  {CALIBRATED} AS "Confidence",
  {BAND} AS "Band",
  {CHECKS} AS "Evidence",
  {KNOWN_FIX} AS "Known fix",
  round((julianday('now') - julianday(i.updated_at)) * 1440) AS "Waiting (min)",
  'Approve' AS "Approve",
  'Deny' AS "Deny"
FROM incidents i LEFT JOIN latest l USING (incident_id) LEFT JOIN diagnoses d ON d.id = l.id
WHERE {WAITING} AND {FILTER}
ORDER BY i.updated_at"""
queue = table(
    111,
    "Waiting for your decision",
    queue_sql,
    {"x": 0, "y": 6, "w": 24, "h": 8},
    description="Only tickets with a guardrail-checked fix waiting for a person, oldest first. Confidence is evidence-calibrated "
    "(0.4 × model + 0.6 × evidence checks): red < 0.70 escalates, amber needs approval, green ≥ 0.85 may auto-fix a known fix. "
    "Evidence, in order: the cause names what changed · a live check confirms it · the error's own document was found · knowledge "
    "search was strong (· = not recorded for older tickets). Approve / Deny open the console's confirm page.",
    overrides=[
        override("Ticket", ("custom.width", 90), TICKET_LINK),
        override("Pipeline", ("custom.width", 150)),
        override(
            "Class", ("custom.width", 120), ("mappings", CLASS_MAP), ("custom.cellOptions", {"type": "color-text"})
        ),
        override("Proposed fix", ("custom.width", 230)),
        override(
            "Confidence",
            ("custom.width", 150),
            ("min", 0),
            ("max", 1),
            ("decimals", 2),
            ("thresholds", CONFIDENCE_STEPS),
            ("custom.cellOptions", {"type": "gauge", "mode": "basic", "valueDisplayMode": "text"}),
        ),
        override("Band", ("custom.width", 130)),
        override("Evidence", ("custom.width", 80), ("custom.align", "center")),
        override("Known fix", ("custom.width", 140)),
        override("Waiting (min)", ("custom.width", 100)),
        button("Approve", GREEN, "approve", "${__data.fields.Ticket}"),
        button("Deny", RED, "reject", "${__data.fields.Ticket}"),
    ],
)
queue["fieldConfig"]["defaults"]["noValue"] = "Nothing is waiting for you."

# --- row 3: escalated ---------------------------------------------------------------------------------------
escalated_row = row(120, "Escalated: a person must act", 14)
escalated_sql = f"""WITH {LATEST}
SELECT
  i.incident_id AS "Ticket",
  coalesce(i.pipeline, '?') AS "Pipeline",
  coalesce(json_extract(d.diagnosis_json, '$.root_cause.failure_class'), '-') AS "Class",
  coalesce((SELECT e.detail FROM incident_events e WHERE e.incident_id = i.incident_id AND e.kind = 'status:escalated'
            ORDER BY e.id DESC LIMIT 1), '-') AS "Why the copilot stopped",
  coalesce(json_extract(d.diagnosis_json, '$.resolution.steps[0]'), 'see the ticket') AS "Suggested next step",
  round((julianday('now') - julianday(i.updated_at)) * 24, 1) AS "Open for (h)",
  'Close' AS "Close"
FROM incidents i LEFT JOIN latest l USING (incident_id) LEFT JOIN diagnoses d ON d.id = l.id
WHERE i.status = 'escalated' AND {FILTER}
ORDER BY i.updated_at"""
escalations = table(
    121,
    "Handed to a person",
    escalated_sql,
    {"x": 0, "y": 15, "w": 24, "h": 7},
    description="Tickets the copilot could not fix safely on its own. Act outside the copilot (the suggested step is a start), "
    "then Close: it opens a confirm page, nothing runs, and your note goes into the history.",
    overrides=[
        override("Ticket", ("custom.width", 90), TICKET_LINK),
        override("Pipeline", ("custom.width", 150)),
        override(
            "Class", ("custom.width", 120), ("mappings", CLASS_MAP), ("custom.cellOptions", {"type": "color-text"})
        ),
        override("Open for (h)", ("custom.width", 100)),
        button("Close", GREY, "close", "${__data.fields.Ticket}"),
    ],
)
escalations["fieldConfig"]["defaults"]["noValue"] = "No escalated tickets."

# --- row 4: stages ------------------------------------------------------------------------------------------
stages_row = row(130, "Where tickets are, and how they ended · last 30 days", 22)
stages_sql = f"""SELECT
  count(*) AS "Detected",
  sum(EXISTS (SELECT 1 FROM diagnoses x WHERE x.incident_id = i.incident_id)) AS "Diagnosed",
  sum(status = 'awaiting_approval') AS "Awaiting approval",
  sum(status IN ('open', 'investigating', 'remediating')) AS "In progress",
  sum(status = 'resolved' AND NOT EXISTS (SELECT 1 FROM incident_events a WHERE a.incident_id = i.incident_id AND a.kind = 'auto_approved')) AS "Fixed, approved by a person",
  sum(status = 'resolved' AND EXISTS (SELECT 1 FROM incident_events a WHERE a.incident_id = i.incident_id AND a.kind = 'auto_approved')) AS "Auto-fixed",
  sum(status = 'escalated') AS "Escalated",
  sum(status IN ('rejected', 'closed')) AS "Closed, nothing run"
FROM incidents i WHERE {LAST_30D} AND {FILTER}"""
stages = {
    "id": 131,
    "type": "stat",
    "title": "Lifecycle",
    "description": "Left to right: detected → diagnosed → waiting → fixing → how it ended. Ends are split so you can see how much "
    "the copilot fixes on its own.",
    "datasource": DS,
    "gridPos": {"x": 0, "y": 23, "w": 24, "h": 4},
    "targets": [target(stages_sql)],
    "options": {
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        "colorMode": "value",
        "graphMode": "none",
        "textMode": "value_and_name",
        "justifyMode": "center",
        "orientation": "vertical",
        "wideLayout": True,
    },
    "fieldConfig": {
        "defaults": {"color": {"mode": "fixed", "fixedColor": GREY}, "decimals": 0, "noValue": "0"},
        "overrides": [
            override("Awaiting approval", ("color", {"mode": "fixed", "fixedColor": AMBER})),
            override("In progress", ("color", {"mode": "fixed", "fixedColor": BLUE})),
            override("Fixed, approved by a person", ("color", {"mode": "fixed", "fixedColor": GREEN})),
            override("Auto-fixed", ("color", {"mode": "fixed", "fixedColor": VIOLET})),
            override("Escalated", ("color", {"mode": "fixed", "fixedColor": RED})),
        ],
    },
}

# --- row 5: can we trust the confidence? -------------------------------------------------------------------
trust_row = row(140, "Can we trust the confidence?", 27)
DIAG_BAND = f"""WITH {LATEST}, b AS (
  SELECT i.incident_id, {CALIBRATED} AS c
  FROM incidents i JOIN latest l USING (incident_id) JOIN diagnoses d ON d.id = l.id WHERE {FILTER})"""
worked_sql = f"""{DIAG_BAND}, o AS (
  SELECT incident_id, max(kind IN ('approval:approve', 'auto_approved')) AS tried, max(kind = 'status:resolved') AS ok
  FROM incident_events GROUP BY incident_id)
SELECT CASE WHEN c >= 0.85 THEN '≥ 0.85' WHEN c >= 0.7 THEN '0.70 – 0.85' ELSE '< 0.70' END
         || ' · ' || sum(o.ok AND o.tried) || ' of ' || sum(o.tried) || ' fixes' AS name,
       1.0 * sum(o.ok AND o.tried) / sum(o.tried) AS value
FROM b JOIN o USING (incident_id) WHERE c IS NOT NULL
GROUP BY c >= 0.85, c >= 0.7 HAVING sum(o.tried) > 0 ORDER BY min(c) DESC"""
worked = bars(
    141,
    "Fixes that worked, by confidence",
    worked_sql,
    {"x": 0, "y": 28, "w": 8, "h": 7},
    GREEN,
    "Of the fixes that were run (approved or auto-applied), the share whose rerun was verified, per confidence band. "
    "A trustworthy score has higher bands working more often; small counts mean a trend to watch, not proof.",
    unit="percentunit",
    max_value=1,
)
bands_sql = f"""{DIAG_BAND}
SELECT CASE WHEN c >= 0.85 THEN '≥ 0.85 · auto-fix eligible' WHEN c >= 0.7 THEN '0.70 – 0.85 · needs approval'
            ELSE '< 0.70 · escalated' END AS name, count(*) AS value
FROM b WHERE c IS NOT NULL GROUP BY c >= 0.85, c >= 0.7 ORDER BY min(c) DESC"""
bands = bars(
    142,
    "Diagnoses by confidence band",
    bands_sql,
    {"x": 8, "y": 28, "w": 8, "h": 7},
    BLUE,
    "Every ticket's latest diagnosis by evidence-calibrated confidence. Calibrated = 0.4 × the model's number + 0.6 × the share of "
    "the four evidence checks that passed.",
)
auto_sql = f"""SELECT CASE WHEN e.kind = 'auto_approved' THEN 'auto-fixed'
            ELSE 'held: ' || replace(substr(e.detail, 1, CASE WHEN instr(e.detail, ' (') > 0 THEN instr(e.detail, ' (') - 1 ELSE length(e.detail) END),
                                     'needs a person: ', '') END AS "Decision",
       count(*) AS "Tickets"
FROM incident_events e JOIN incidents i USING (incident_id)
WHERE e.kind IN ('auto_approved', 'auto_approval:no') AND e.at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-30 days') AND {FILTER}
GROUP BY 1 ORDER BY 2 DESC"""
auto_decisions = table(
    143,
    "Why a known fix did or did not auto-apply · 30 days",
    auto_sql,
    {"x": 16, "y": 28, "w": 8, "h": 7},
    description="Every auto-apply decision: applied, or the rule that held it back for a person.",
    overrides=[override("Tickets", ("custom.width", 80))],
)
auto_decisions["fieldConfig"]["defaults"]["noValue"] = "No auto-apply decisions yet."

# --- row 6: what the copilot has learned -------------------------------------------------------------------
learned_row = row(150, "What the copilot has learned", 35)
learned_count = stat(
    151, "Fixes learned", "SELECT count(*) AS value FROM memories", {"x": 0, "y": 36, "w": 4, "h": 4}, VIOLET,
    "One memory per failure pattern, written after a fix was approved and verified by a rerun.",
)  # fmt: skip
eligible = stat(
    152,
    "Ready to auto-fix",
    "SELECT count(*) AS value FROM memories m WHERE failure_count = 0 AND fix_successes >= 2",
    {"x": 0, "y": 40, "w": 4, "h": 4},
    GREEN,
    "Learned fixes that worked at least twice and never failed (they still need confidence ≥ 0.85 and auto-apply switched on for the connection).",
)
reused = stat(
    153,
    "Verified reuses",
    "SELECT coalesce(sum(fix_successes), 0) AS value FROM memories",
    {"x": 0, "y": 44, "w": 4, "h": 4},
    BLUE,
    "Times a learned fix was applied again and its rerun verified.",
)
memories_sql = """SELECT
  m.id AS "#",
  m.failure_class AS "Class",
  coalesce(m.pipeline_key, '-') AS "Pipeline",
  m.failed_task AS "Failed task",
  coalesce(nullif(substr(m.pattern, length(rtrim(m.pattern, replace(m.pattern, '|', ''))) + 1), '-'), '-') AS "What changed",
  'rerun ' || coalesce(m.fix_json, '?') AS "Fix that worked",
  m.fix_successes AS "Worked",
  m.failure_count AS "Failed",
  CASE WHEN m.failure_count > 0 THEN 'distrusted'
       WHEN m.fix_successes >= 2 AND EXISTS (SELECT 1 FROM connections c WHERE m.pipeline_key LIKE c.connection_id || ':%' AND c.auto_apply = 1)
         THEN 'auto-fix on'
       WHEN m.fix_successes >= 2 THEN 'eligible, switched off'
       ELSE 'needs ' || (2 - m.fix_successes) || ' more' END AS "Auto-fix",
  (SELECT group_concat(value, ', ') FROM json_each(m.incident_ids)) AS "Learned from",
  replace(substr(m.updated_at, 1, 16), 'T', ' ') AS "Last used (UTC)"
FROM memories m ORDER BY m.updated_at DESC"""
memories = table(
    154,
    "Learned fixes",
    memories_sql,
    {"x": 4, "y": 36, "w": 20, "h": 12},
    description="Everything the copilot has learned: the failure pattern, the fix that worked, its track record and whether it may "
    "apply itself. A fix that fails later is distrusted and no longer offered.",
    overrides=[
        override("#", ("custom.width", 50)),
        override(
            "Class", ("custom.width", 120), ("mappings", CLASS_MAP), ("custom.cellOptions", {"type": "color-text"})
        ),
        override("Pipeline", ("custom.width", 230)),
        override("Failed task", ("custom.width", 150)),
        override("Worked", ("custom.width", 70)),
        override("Failed", ("custom.width", 70)),
        override(
            "Auto-fix",
            ("custom.width", 150),
            (
                "mappings",
                [
                    {
                        "type": "value",
                        "options": {
                            "auto-fix on": {"text": "auto-fix on", "color": GREEN, "index": 0},
                            "eligible, switched off": {"text": "eligible, switched off", "color": AMBER, "index": 1},
                            "distrusted": {"text": "distrusted", "color": RED, "index": 2},
                        },
                    }
                ],
            ),
            ("custom.cellOptions", {"type": "color-text"}),
        ),
        override("Last used (UTC)", ("custom.width", 140)),
    ],
)

# --- row 7: trends (collapsed) -----------------------------------------------------------------------------
per_day_sql = (
    f"""WITH {LATEST}
SELECT substr(i.opened_at, 1, 10) AS "Day",
"""
    + ",\n".join(
        f"  sum(json_extract(d.diagnosis_json, '$.root_cause.failure_class') = '{k}') AS \"{t}\""
        for k, (t, _) in CLASS_NAMES.items()
    )
    + f""",
  sum(json_extract(d.diagnosis_json, '$.root_cause.failure_class') IS NULL) AS "not diagnosed"
FROM incidents i LEFT JOIN latest l USING (incident_id) LEFT JOIN diagnoses d ON d.id = l.id
WHERE {FILTER} GROUP BY 1 ORDER BY 1"""
)
per_day = {
    "id": 161,
    "type": "barchart",
    "title": "Tickets per day, by failure class",
    "description": "Opened tickets per day, stacked by the diagnosed failure class.",
    "datasource": DS,
    "gridPos": {"x": 0, "y": 49, "w": 14, "h": 8},
    "targets": [target(per_day_sql)],
    "options": {
        "stacking": "normal",
        "xField": "Day",
        "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
        "barWidth": 0.6,
        "showValue": "never",
        "orientation": "vertical",
    },  # fmt: skip
    "fieldConfig": {
        "defaults": {"custom": {"fillOpacity": 85, "lineWidth": 0}, "decimals": 0},
        "overrides": [override(t, ("color", {"mode": "fixed", "fixedColor": c})) for _, (t, c) in CLASS_NAMES.items()]
        + [override("not diagnosed", ("color", {"mode": "fixed", "fixedColor": GREY}))],
    },
}
activity_sql = f"""SELECT replace(substr(e.at, 1, 19), 'T', ' ') AS "Time (UTC)", e.incident_id AS "Ticket", e.kind AS "Step",
  e.detail AS "Detail"
FROM incident_events e JOIN incidents i USING (incident_id) WHERE {FILTER} ORDER BY e.id DESC LIMIT 40"""
activity = table(
    162,
    "Latest activity (audit trail)",
    activity_sql,
    {"x": 14, "y": 49, "w": 10, "h": 8},
    description="Every step the copilot and people took, newest first.",
    overrides=[
        override("Time (UTC)", ("custom.width", 150)),
        override("Ticket", ("custom.width", 90), TICKET_LINK),
        override("Step", ("custom.width", 170)),
        override("Detail", ("custom.cellOptions", {"type": "auto", "wrapText": False})),
    ],
)
trends_row = row(160, "Trends and audit trail", 48, collapsed=True, panels=[per_day, activity])


def query_var(name, label, sql):
    return {
        "name": name,
        "label": label,
        "type": "query",
        "datasource": DS,
        "query": sql,  # the SQLite plugin wants variable queries as plain SQL
        "definition": sql,
        "refresh": 1,
        "sort": 1,
        "current": {"text": "All", "value": "$__all"},
        "options": [],
        "includeAll": True,
        "allValue": "__all",
        "multi": False,
    }


platform_var = query_var(
    "platform", "Platform", "SELECT platform FROM incidents UNION SELECT platform FROM connections"
)
app_var = query_var(
    "app",
    "Application",
    "SELECT pipeline FROM incidents WHERE pipeline IS NOT NULL AND ('${platform}' = '__all' OR platform = '${platform}') "
    "UNION SELECT p.value FROM connections c, json_each(c.pipelines_json) p WHERE ('${platform}' = '__all' OR c.platform = '${platform}')",
)
desk = dashboard(
    DESK_UID,
    "Incident Command",
    [
        now_row,
        awaiting,
        escalated_now,
        fixing,
        auto_fixed,
        time_to_diagnosis,
        time_to_fix,
        queue_row,
        queue,
        escalated_row,
        escalations,
        stages_row,
        stages,
        trust_row,
        worked,
        bands,
        auto_decisions,
        learned_row,
        learned_count,
        eligible,
        reused,
        memories,
        trends_row,
    ],  # fmt: skip
    templating=[platform_var, app_var],
    links=[
        {"title": "Connections", "type": "link", "url": f"/d/{CONNECTIONS_UID}/connections", "icon": "dashboard"},
        {
            "title": "Self-improvement",
            "type": "link",
            "url": "/d/copilot-self-improvement/self-improvement",
            "icon": "dashboard",
        },
        {
            "title": "+ Connect a pipeline",
            "type": "link",
            "url": f"{CONSOLE}/connections",
            "targetBlank": True,
            "icon": "external link",
        },
        {"title": "Approval console", "type": "link", "url": CONSOLE, "targetBlank": True, "icon": "external link"},
        {
            "title": "Pipeline health (Grafana Cloud)",
            "type": "link",
            "url": HEALTH,
            "targetBlank": True,
            "icon": "dashboard",
        },
        {
            "title": "Langfuse traces",
            "type": "link",
            "url": f"{LANGFUSE}/sessions",
            "targetBlank": True,
            "icon": "external link",
        },
    ],  # fmt: skip
    description="What needs a person now, the approval queue, escalations, where tickets are, whether the confidence can be "
    "trusted, and what the copilot has learned. Refreshes every 10 s.",
)

# ------------------------------------------------------------------ Ticket detail
incident_var = {
    "name": "incident",
    "label": "Ticket",
    "type": "query",
    "datasource": DS,
    "query": "SELECT incident_id FROM incidents ORDER BY seq DESC",
    "definition": "SELECT incident_id FROM incidents ORDER BY seq DESC",
    "refresh": 1,
    "sort": 0,
    "current": {},
    "options": [],
    "includeAll": False,
    "multi": False,
}
ONE = "d AS (SELECT diagnosis_json AS diagnosis_json FROM diagnoses WHERE incident_id = '$incident' ORDER BY id DESC LIMIT 1)"
summary_sql = f"""WITH {ONE}
SELECT i.status AS "Status",
  coalesce((SELECT json_extract(diagnosis_json, '$.root_cause.failure_class') FROM d), '-') AS "Class",
  (SELECT {CALIBRATED} FROM d) AS "Confidence",
  {DECIDED_BY} AS "Decided by",
  {PLATFORM_SQL.format(col="i.platform")} || ' · ' || coalesce(i.pipeline, '?') AS "Application",
  coalesce(i.failed_task, '-') AS "Failed task",
  replace(substr(i.opened_at, 1, 16), 'T', ' ') AS "Opened (UTC)"
FROM incidents i WHERE i.incident_id = '$incident'"""
summary = {
    "id": 1,
    "type": "stat",
    "title": "Ticket $incident",
    "datasource": DS,
    "gridPos": {"x": 0, "y": 0, "w": 24, "h": 4},
    "targets": [target(summary_sql)],
    "options": {
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/", "values": False},
        "colorMode": "background",
        "graphMode": "none",
        "textMode": "value_and_name",
        "text": {"titleSize": 13, "valueSize": 20},
        "justifyMode": "center",
        "orientation": "vertical",
        "wideLayout": True,
    },
    "fieldConfig": {
        "defaults": {"color": {"mode": "fixed", "fixedColor": "#4E5D72"}},
        "overrides": [
            override(
                "Status",
                ("mappings", STATUS_MAP),
                ("color", {"mode": "thresholds"}),
                ("thresholds", {"mode": "absolute", "steps": [{"color": GREY, "value": None}]}),
            ),
            override("Class", ("mappings", CLASS_MAP)),
            override(
                "Confidence", ("decimals", 2), ("color", {"mode": "thresholds"}), ("thresholds", CONFIDENCE_STEPS)
            ),
        ],
    },
}
decision_sql = f"""WITH {LATEST}
SELECT CASE WHEN {WAITING} THEN 'Approve' ELSE '' END AS "Approve",
  CASE WHEN {WAITING} THEN 'Deny' ELSE '' END AS "Deny",
  CASE WHEN i.status = 'escalated' THEN 'Close' ELSE '' END AS "Close",
  CASE WHEN {WAITING} THEN 'rerun_job ' || json_extract(d.diagnosis_json, '$.resolution.action.parameters')
       WHEN i.status = 'escalated' THEN 'a person must act, then close the ticket'
       ELSE 'no decision needed (' || replace(i.status, '_', ' ') || ')' END AS "Decision",
  'Open on ' || {PLATFORM_SQL.format(col="i.platform")} AS "Failed run",
  'Open the trace' AS "Langfuse",
  json_extract(i.incident_json, '$.run.run_page_url') AS "run_url"
FROM incidents i LEFT JOIN latest l USING (incident_id) LEFT JOIN diagnoses d ON d.id = l.id
WHERE i.incident_id = '$incident'"""
decision = table(
    2,
    "Decision",
    decision_sql,
    {"x": 0, "y": 4, "w": 24, "h": 4},
    description="Approve / Deny a waiting fix, or Close an escalated ticket: each opens the console's confirm page.",
    overrides=[
        button("Approve", GREEN, "approve", "${incident}"),
        button("Deny", RED, "reject", "${incident}"),
        button("Close", GREY, "close", "${incident}"),
        override(
            "Failed run",
            ("custom.width", 180),
            ("links", [{"title": "Open the failed run", "url": "${__data.fields.run_url}", "targetBlank": True}]),
        ),
        override(
            "Langfuse",
            ("custom.width", 150),
            (
                "links",
                [{"title": "Langfuse session", "url": f"{LANGFUSE}/sessions/${{incident}}", "targetBlank": True}],
            ),
        ),
        override("run_url", ("custom.hidden", True)),
    ],
)
SIGNAL_TEXT = {
    "explained_by_change": "Names what changed since the last good run",
    "confirmed_by_check": "Confirmed by a live check",
    "exact_error_doc": "The error’s own document was found",
    "knowledge_ok": "Knowledge search was strong",
}
why_sql = (
    f"WITH {ONE}\n"
    + "\nUNION ALL ".join(
        [
            "SELECT 1 AS n, 'Model confidence' AS \"Part\", printf('%.2f', json_extract(diagnosis_json, '$.confidence.model')) AS \"Value\", "
            "'the model’s own number (counts 40%)' AS \"Meaning\" FROM d"
        ]
        + [
            f"SELECT {i + 2}, '{text}', CASE json_extract(diagnosis_json, '$.confidence.signals.{key}') WHEN 1 THEN '✓ passed' WHEN 0 THEN '✗ failed' "
            f"ELSE 'not recorded' END, 'evidence check (the four together count 60%)' FROM d"
            for i, (key, text) in enumerate(SIGNAL_TEXT.items())
        ]
        + [
            f"SELECT 6, 'Calibrated confidence', printf('%.2f', {CALIBRATED}), "
            f"CASE WHEN {CALIBRATED} >= 0.85 THEN 'auto-fix eligible (with a known fix)' WHEN {CALIBRATED} >= 0.7 THEN 'a person approves' "
            f"ELSE 'escalated to a person' END FROM d"
        ]
    )
    + "\nORDER BY n"
)
why = table(
    3,
    "Why this confidence",
    why_sql,
    {"x": 0, "y": 8, "w": 10, "h": 8},
    description="Calibrated = 0.4 × model + 0.6 × share of the four evidence checks passed. Older tickets show the score without its checks.",
    overrides=[
        override("n", ("custom.hidden", True)),
        override("Part", ("custom.width", 260)),
        override("Value", ("custom.width", 110)),
    ],
)
diagnosis_sql = """WITH d AS (SELECT diagnosis_json AS j FROM diagnoses WHERE incident_id = '$incident' ORDER BY id DESC LIMIT 1)
SELECT 'Root cause' AS "Item", json_extract(j, '$.root_cause.cause') AS "Detail" FROM d
UNION ALL SELECT 'Rests on', (SELECT group_concat(value, ', ') FROM json_each(j, '$.root_cause.evidence_refs')) FROM d
UNION ALL SELECT 'Proposed fix', json_extract(j, '$.resolution.summary') FROM d
UNION ALL SELECT 'Steps', (SELECT group_concat(value, '  →  ') FROM json_each(j, '$.resolution.steps')) FROM d
UNION ALL SELECT 'Automated action', coalesce('rerun_job ' || json_extract(j, '$.resolution.action.parameters'),
  'none: ' || coalesce(json_extract(j, '$.resolution.action_rejected_reason'), 'a person must act first')) FROM d
UNION ALL SELECT 'Known fix', coalesce(json_extract(j, '$.auto_approval.reason'), '-') FROM d
UNION ALL SELECT 'Knowledge cited', (SELECT group_concat(json_extract(value, '$.parent_id'), ', ') FROM json_each(j, '$.knowledge.citations')) FROM d
UNION ALL SELECT 'Rerun', CASE WHEN json_extract(j, '$.remediation.rerun_run_id') IS NULL THEN '-'
  ELSE json_extract(j, '$.remediation.rerun_run_id') || ' · ' ||
       CASE WHEN json_extract(j, '$.remediation.succeeded') THEN 'succeeded' ELSE coalesce(json_extract(j, '$.remediation.result_state'), 'running') END END FROM d"""
diagnosis = table(
    4,
    "Diagnosis",
    diagnosis_sql,
    {"x": 10, "y": 8, "w": 14, "h": 8},
    description="The latest diagnosis of the agents.",
    overrides=[override("Item", ("custom.width", 140))],
)
stages_detail_sql = """WITH s AS (
  SELECT at, CASE kind WHEN 'opened' THEN 'detected' ELSE replace(replace(kind, 'status:', ''), '_', ' ') END AS stage, id
  FROM incident_events WHERE incident_id = '$incident' AND (kind = 'opened' OR kind LIKE 'status:%' OR kind = 'auto_approved'))
SELECT replace(substr(at, 1, 19), 'T', ' ') AS "Time (UTC)", stage AS "Stage",
  CASE WHEN lead(at) OVER (ORDER BY id) IS NULL THEN 'now'
       ELSE printf('%.0f s', (julianday(lead(at) OVER (ORDER BY id)) - julianday(at)) * 86400) END AS "Time in stage"
FROM s ORDER BY id"""
stage_times = table(
    5,
    "Stages",
    stages_detail_sql,
    {"x": 0, "y": 16, "w": 10, "h": 8},
    description="Each stage the ticket went through and how long it stayed there.",
    overrides=[override("Time (UTC)", ("custom.width", 160)), override("Stage", ("custom.width", 170))],
)
evidence_sql = """SELECT 'error' AS "Check", substr(json_extract(bundle_json, '$.error'), 1, 400) AS "Result" FROM evidence WHERE incident_id = '$incident'
UNION ALL SELECT json_extract(t.value, '$.tool'), json_extract(t.value, '$.summary')
  FROM evidence, json_each(evidence.bundle_json, '$.tool_results') t WHERE evidence.incident_id = '$incident'
UNION ALL SELECT 'not collected', p.value FROM evidence, json_each(evidence.bundle_json, '$.problems') p WHERE evidence.incident_id = '$incident'"""
evidence = table(
    6,
    "Evidence",
    evidence_sql,
    {"x": 10, "y": 16, "w": 14, "h": 8},
    description="Collected when the failure was found: the error, the run comparison, the git diff, live checks.",
    overrides=[override("Check", ("custom.width", 200))],
)
history_sql = """SELECT replace(substr(at, 1, 19), 'T', ' ') AS "Time (UTC)", kind AS "Step", detail AS "Detail"
FROM incident_events WHERE incident_id = '$incident' ORDER BY id DESC"""
history = table(
    7,
    "History",
    history_sql,
    {"x": 0, "y": 25, "w": 24, "h": 11},
    description="Every step for this ticket, newest first.",
    overrides=[override("Time (UTC)", ("custom.width", 160)), override("Step", ("custom.width", 220))],
)
detail = dashboard(
    DETAIL_UID,
    "Ticket detail",
    [
        summary,
        decision,
        why,
        diagnosis,
        stage_times,
        evidence,
        row(8, "Full history", 24, collapsed=True, panels=[history]),
    ],
    links=[
        {"title": "Incident Command", "type": "link", "url": f"/d/{DESK_UID}/incident-desk", "icon": "dashboard"},
        {"title": "Connections", "type": "link", "url": f"/d/{CONNECTIONS_UID}/connections", "icon": "dashboard"},
        {"title": "Approval console", "type": "link", "url": CONSOLE, "targetBlank": True, "icon": "external link"},
    ],
    templating=[incident_var],
    description="One ticket: status, decision, why the confidence is what it is, diagnosis, stages, evidence and history.",
)


# ------------------------------------------------------------------ Connections
OPEN = "status NOT IN ('resolved', 'rejected', 'closed')"
HEALTH_MAP = [
    {
        "type": "value",
        "options": {
            "SUCCESS": {"text": "succeeded", "color": GREEN, "index": 0},
            "SUCCEEDED": {"text": "succeeded", "color": GREEN, "index": 1},
            "FAILED": {"text": "failed", "color": RED, "index": 2},
            "FAILURE": {"text": "failed", "color": RED, "index": 3},
            "UNSTABLE": {"text": "unstable", "color": AMBER, "index": 4},
            "not polled yet": {"text": "not polled yet", "color": GREY, "index": 5},
        },
    }
]
CHECK_MAP = [
    {
        "type": "value",
        "options": {
            "connected": {"text": "connected", "color": GREEN, "index": 0},
            "check failed": {"text": "check failed", "color": RED, "index": 1},
            "off": {"text": "off", "color": GREY, "index": 2},
            "not checked": {"text": "not checked", "color": BLUE, "index": 3},
        },
    }
]
conn_counts_sql = f"""SELECT
  (SELECT count(DISTINCT platform) FROM connections WHERE enabled) AS "Platforms",
  (SELECT coalesce(sum(json_array_length(pipelines_json)), 0) FROM connections WHERE enabled) AS "Applications watched",
  (SELECT count(*) FROM pipeline_health h JOIN connections c USING (connection_id)
     WHERE c.enabled AND h.last_state IN ('SUCCESS', 'SUCCEEDED')) AS "Last run ok",
  (SELECT count(*) FROM pipeline_health h JOIN connections c USING (connection_id)
     WHERE c.enabled AND h.last_state IN ('FAILED', 'FAILURE', 'UNSTABLE')) AS "Last run failed",
  (SELECT count(*) FROM connections WHERE enabled AND last_check_ok = 0) AS "Connection errors",
  (SELECT count(*) FROM incidents WHERE {OPEN}) AS "Open tickets",
  (SELECT round((julianday('now') - julianday(max(value))) * 1440, 1) FROM watcher_state WHERE key LIKE 'last_poll:%')
     AS "Watcher: min since last poll"
"""
conn_counts = {
    "id": 1,
    "type": "stat",
    "title": "Connected pipelines",
    "datasource": DS,
    "gridPos": {"x": 0, "y": 0, "w": 18, "h": 4},
    "targets": [target(conn_counts_sql)],
    "options": {
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        "colorMode": "background",
        "graphMode": "none",
        "textMode": "value_and_name",
        "justifyMode": "center",
        "orientation": "vertical",
        "wideLayout": True,
    },
    "fieldConfig": {
        "defaults": {"color": {"mode": "fixed", "fixedColor": BLUE}, "decimals": 0, "noValue": "0"},
        "overrides": [
            override("Last run ok", ("color", {"mode": "fixed", "fixedColor": GREEN})),
            override("Last run failed", ("color", {"mode": "fixed", "fixedColor": RED})),
            override(
                "Connection errors",
                ("color", {"mode": "thresholds"}),
                (
                    "thresholds",
                    {"mode": "absolute", "steps": [{"color": GREY, "value": None}, {"color": RED, "value": 1}]},
                ),
            ),
            override("Open tickets", ("color", {"mode": "fixed", "fixedColor": AMBER})),
            override(
                "Watcher: min since last poll",
                ("decimals", 1),
                ("noValue", "never"),
                ("color", {"mode": "thresholds"}),
                (
                    "thresholds",
                    {"mode": "absolute", "steps": [{"color": GREEN, "value": None}, {"color": RED, "value": 10}]},
                ),
            ),
        ],
    },
}
connect_text = {
    "id": 2,
    "type": "text",
    "title": "",
    "gridPos": {"x": 18, "y": 0, "w": 6, "h": 4},
    "options": {
        "mode": "markdown",
        "content": f"### [＋ Connect a pipeline]({CONSOLE}/connections)\n"
        "Databricks · Jenkins available now; Snowflake, AWS Glue, Azure Data Factory next. "
        "Secrets stay in the macOS Keychain.",
    },
}
apps_sql = f"""SELECT
  {PLATFORM_SQL.format(col="c.platform")} AS "Platform",
  c.name AS "Connection",
  p.value AS "Application",
  coalesce(h.last_state, 'not polled yet') AS "Last run",
  CASE WHEN h.last_run_id IS NULL THEN '' ELSE '#' || h.last_run_id END AS "Run",
  replace(substr(h.last_run_at, 1, 16), 'T', ' ') AS "Last run at (UTC)",
  coalesce(h.runs_24h, 0) AS "Runs 24 h",
  coalesce(h.failures_24h, 0) AS "Failed 24 h",
  (SELECT count(*) FROM incidents i WHERE i.connection_id = c.connection_id AND i.pipeline = p.value AND i.{OPEN}) AS "Open tickets",
  (SELECT count(*) FROM incidents i WHERE i.connection_id = c.connection_id AND i.pipeline = p.value) AS "All tickets",
  c.platform AS "platform_key",
  h.last_run_url AS "run_url"
FROM connections c, json_each(c.pipelines_json) p
LEFT JOIN pipeline_health h ON h.connection_id = c.connection_id AND h.pipeline = p.value
WHERE c.enabled
ORDER BY h.last_state IN ('FAILED', 'FAILURE', 'UNSTABLE') DESC, "Open tickets" DESC, c.name, p.value"""
desk_link = (
    f"/d/{DESK_UID}/incident-desk?var-platform=${{__data.fields.platform_key}}&var-app=${{__data.fields.Application}}"
)
apps = table(
    3,
    "Applications",
    apps_sql,
    {"x": 0, "y": 4, "w": 24, "h": 8},
    description="Every watched pipeline on every connection, failing ones first, as of the watcher's last poll (copilot watch). "
    "Click an application for its tickets, a run number for the run on its platform.",
    overrides=[
        override("Platform", ("custom.width", 130)),
        override("Connection", ("custom.width", 260)),
        override("Application", ("links", [{"title": "Tickets of this application", "url": desk_link}])),
        override(
            "Last run",
            ("mappings", HEALTH_MAP),
            ("custom.cellOptions", {"type": "color-background", "mode": "basic"}),
            ("custom.width", 130),
        ),
        override(
            "Run",
            ("custom.width", 150),
            ("links", [{"title": "Open the run", "url": "${__data.fields.run_url}", "targetBlank": True}]),
        ),
        override("Last run at (UTC)", ("custom.width", 140)),
        override("Runs 24 h", ("custom.width", 90)),
        override(
            "Failed 24 h",
            ("custom.width", 100),
            ("custom.cellOptions", {"type": "color-text"}),
            (
                "thresholds",
                {"mode": "absolute", "steps": [{"color": "text", "value": None}, {"color": RED, "value": 1}]},
            ),
        ),
        override(
            "Open tickets",
            ("custom.width", 110),
            ("custom.cellOptions", {"type": "color-text"}),
            (
                "thresholds",
                {"mode": "absolute", "steps": [{"color": "text", "value": None}, {"color": AMBER, "value": 1}]},
            ),
        ),
        override("All tickets", ("custom.width", 100)),
        override("platform_key", ("custom.hidden", True)),
        override("run_url", ("custom.hidden", True)),
    ],
)
conns_sql = f"""SELECT c.name AS "Connection",
  {PLATFORM_SQL.format(col="c.platform")} AS "Platform",
  CASE WHEN NOT c.enabled THEN 'off' WHEN c.last_check_ok IS NULL THEN 'not checked'
       WHEN c.last_check_ok THEN 'connected' ELSE 'check failed' END AS "Status",
  replace(substr(c.last_check_at, 1, 16), 'T', ' ') AS "Last check (UTC)",
  coalesce(c.last_check_message, '') AS "Result",
  json_array_length(c.pipelines_json) AS "Applications",
  CASE WHEN c.auto_apply THEN 'on · after ' || c.auto_apply_min_successes || ' successes · '
       || CASE WHEN c.auto_apply_cooldown_hours > 0 THEN c.auto_apply_cooldown_hours || ' h cooldown' ELSE 'no cooldown' END
       ELSE 'off' END AS "Auto-apply",
  CASE WHEN json_array_length(c.approvers_json) > 0
       THEN (SELECT group_concat(a.value, ', ') FROM json_each(c.approvers_json) a) ELSE 'default recipients' END
    AS "Approvers",
  'Manage' AS "Manage",
  c.connection_id AS "connection_id"
FROM connections c ORDER BY c.created_at"""
conns = table(
    4,
    "Connections",
    conns_sql,
    {"x": 0, "y": 12, "w": 15, "h": 7},
    description="One row per platform account. Manage opens the console's form (edit, check now, pause, delete).",
    overrides=[
        override("Connection", ("custom.width", 270)),
        override("Platform", ("custom.width", 110)),
        override(
            "Status",
            ("mappings", CHECK_MAP),
            ("custom.cellOptions", {"type": "color-background", "mode": "basic"}),
            ("custom.width", 120),
        ),
        override("Last check (UTC)", ("custom.width", 130)),
        override("Applications", ("custom.width", 100)),
        override("Auto-apply", ("custom.width", 170)),
        override("Result", ("custom.cellOptions", {"type": "auto", "wrapText": False})),
        override(
            "Manage",
            ("custom.width", 90),
            (
                "links",
                [
                    {
                        "title": "Edit in the console",
                        "url": f"{CONSOLE}/connections/${{__data.fields.connection_id}}/edit",
                        "targetBlank": True,
                    }
                ],
            ),
        ),
        override("connection_id", ("custom.hidden", True)),
    ],
)
open_by_app_sql = f"""SELECT {PLATFORM_SQL.format(col="platform")} || ' · ' || coalesce(pipeline, '?') AS application, count(*) AS tickets
FROM incidents WHERE {OPEN} GROUP BY application ORDER BY tickets DESC"""
open_by_app = {
    "id": 5,
    "type": "bargauge",
    "title": "Open tickets by application",
    "datasource": DS,
    "gridPos": {"x": 15, "y": 12, "w": 9, "h": 7},
    "targets": [target(open_by_app_sql)],
    "transformations": [
        {
            "id": "rowsToFields",
            "options": {
                "mappings": [
                    {"fieldName": "application", "handlerKey": "field.name"},
                    {"fieldName": "tickets", "handlerKey": "field.value"},
                ]
            },
        }
    ],
    "options": {
        "orientation": "horizontal",
        "displayMode": "basic",
        "showUnfilled": True,
        "valueMode": "text",
        "namePlacement": "top",
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
    },
    "fieldConfig": {
        "defaults": {
            "color": {"mode": "fixed", "fixedColor": AMBER},
            "min": 0,
            "decimals": 0,
            "noValue": "no open tickets",
        },
        "overrides": [],
    },
}
connections_dash = dashboard(
    CONNECTIONS_UID,
    "Connections",
    [conn_counts, connect_text, apps, conns, open_by_app],
    links=[
        {"title": "Incident desk", "type": "link", "url": f"/d/{DESK_UID}/incident-desk", "icon": "dashboard"},
        {
            "title": "+ Connect a pipeline",
            "type": "link",
            "url": f"{CONSOLE}/connections",
            "targetBlank": True,
            "icon": "external link",
        },
    ],
    description="Every connected pipeline platform and application the copilot watches, with health and open tickets.",
)

# ------------------------------------------------------------------ Self-improvement (Task 2)
SELF_UID = "copilot-self-improvement"
VERDICT_MAP = [
    {
        "type": "value",
        "options": {
            "accepted": {"text": "accepted · branch ready", "color": GREEN, "index": 0},
            "rejected": {"text": "rejected", "color": GREY, "index": 1},
            "skipped": {"text": "skipped", "color": BLUE, "index": 2},
        },
    }
]
self_counts_sql = """SELECT
  (SELECT count(*) FROM improvement_decisions WHERE verdict = 'accepted') AS "Accepted (branches)",
  (SELECT count(*) FROM improvement_decisions WHERE verdict = 'rejected') AS "Rejected",
  (SELECT count(*) FROM eval_runs) AS "Eval runs",
  (SELECT replace(substr(max(at), 1, 16), 'T', ' ') FROM eval_runs) AS "Last nightly (UTC)"
"""
self_counts = {
    "id": 1,
    "type": "stat",
    "title": "Self-improvement loop",
    "datasource": DS,
    "gridPos": {"x": 0, "y": 0, "w": 24, "h": 4},
    "targets": [target(self_counts_sql)],
    "options": {
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/", "values": False},
        "colorMode": "background",
        "graphMode": "none",
        "textMode": "value_and_name",
        "justifyMode": "center",
        "orientation": "vertical",
        "wideLayout": True,
    },
    "fieldConfig": {
        "defaults": {"color": {"mode": "fixed", "fixedColor": BLUE}, "noValue": "-"},
        "overrides": [
            override("Accepted (branches)", ("color", {"mode": "fixed", "fixedColor": GREEN})),
            override("Rejected", ("color", {"mode": "fixed", "fixedColor": GREY})),
            override("Last nightly (UTC)", ("color", {"mode": "fixed", "fixedColor": "#4E5D72"})),
        ],
    },
}
decisions_sql = """SELECT replace(substr(at, 1, 16), 'T', ' ') AS "When (UTC)", kind AS "Kind", change AS "Change",
  verdict AS "Verdict", reason AS "Why", coalesce(branch, '') AS "Branch to review",
  json_extract(deltas_json, '$.quality') AS "Quality Δ (tuning)"
FROM improvement_decisions ORDER BY id DESC LIMIT 50"""
decisions = table(
    2,
    "Decisions (one change per night; accepted = a branch a person merges)",
    decisions_sql,
    {"x": 0, "y": 4, "w": 24, "h": 9},
    description="Every change the improver tried, the validator's verdict and reason. Nothing is merged automatically.",
    overrides=[
        override("When (UTC)", ("custom.width", 140)),
        override("Kind", ("custom.width", 80)),
        override("Change", ("custom.width", 300)),
        override(
            "Verdict",
            ("mappings", VERDICT_MAP),
            ("custom.cellOptions", {"type": "color-background", "mode": "basic"}),
            ("custom.width", 170),
        ),
        override("Why", ("custom.cellOptions", {"type": "auto", "wrapText": False})),
        override("Branch to review", ("custom.width", 280)),
        override("Quality Δ (tuning)", ("custom.width", 140), ("decimals", 3)),
    ],
)
scorecard_sql = """WITH latest AS (SELECT * FROM eval_runs WHERE label = 'baseline' AND split = 'test' ORDER BY id DESC LIMIT 1),
prev AS (SELECT * FROM eval_runs WHERE label = 'baseline' AND split = 'test' ORDER BY id DESC LIMIT 1 OFFSET 1)
SELECT m.key AS "Metric", m.value AS "Tonight",
  (SELECT json_extract(prev.summary_json, '$.' || m.key) FROM prev) AS "Previous night"
FROM latest, json_each(latest.summary_json) m
WHERE m.key IN ('all_ok', 'class_ok', 'fact_ok', 'action_ok', 'context_recall', 'context_precision', 'faithfulness',
                'brier_calibrated', 'escalated', 'mean_seconds', 'incidents')"""
scorecard = table(
    3,
    "Scorecard: holdout incidents, current config",
    scorecard_sql,
    {"x": 0, "y": 13, "w": 10, "h": 10},
    description="The diagnosis eval on the held-out incidents (never used to choose changes), tonight vs the night before.",
    overrides=[override("Metric", ("custom.width", 160))],
)
runs_sql = """SELECT replace(substr(at, 1, 16), 'T', ' ') AS "When (UTC)", label AS "Run", split AS "Split", config AS "Config",
  json_extract(summary_json, '$.all_ok') AS "All correct", json_extract(summary_json, '$.context_recall') AS "Context recall",
  json_extract(summary_json, '$.incidents') AS "Incidents"
FROM eval_runs ORDER BY id DESC LIMIT 40"""
runs = table(
    4,
    "Eval runs",
    runs_sql,
    {"x": 10, "y": 13, "w": 14, "h": 10},
    description="Each eval the loop ran (separate process per configuration).",
    overrides=[
        override("When (UTC)", ("custom.width", 140)),
        override("Run", ("custom.width", 90)),
        override("Split", ("custom.width", 70)),
    ],
)
self_dash = dashboard(
    SELF_UID,
    "Self-improvement",
    [self_counts, decisions, scorecard, runs],
    links=[
        {"title": "Incident desk", "type": "link", "url": f"/d/{DESK_UID}/incident-desk", "icon": "dashboard"},
        {"title": "Connections", "type": "link", "url": f"/d/{CONNECTIONS_UID}/connections", "icon": "dashboard"},
    ],
    description="The nightly loop: scorecard, one change at a time, validator verdicts, branches waiting for a person.",
)

OUT.mkdir(parents=True, exist_ok=True)
for name, d in (
    ("incident_desk.json", desk),
    ("ticket_detail.json", detail),
    ("connections.json", connections_dash),
    ("self_improvement.json", self_dash),
):
    (OUT / name).write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
    print("wrote", OUT / name)
