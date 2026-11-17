"""Build the review deck (PowerPoint) from the saved evaluation reports.

Slides: 1 architecture (network map) · 2 evaluation + RAGAS-style scorecard · 3 Langfuse traces,
log plumbing and audit trail. Native shapes, charts and tables, so everything stays editable.
Fonts are Arial / Arial Black (installed everywhere; a missing font would be silently swapped).

Usage: python -m scripts.build_review_deck [--out docs/pipeline-incident-copilot-review.pptx]
"""

import argparse
import json
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

from pipeline_incident_copilot.config import EVALS_DIR, PROJECT_ROOT

REPORTS, BASELINES = EVALS_DIR / "reports", EVALS_DIR / "baselines"
OUT = PROJECT_ROOT / "docs" / "pipeline-incident-copilot-review.pptx"

FONT, DISPLAY = "Arial", "Arial Black"
NAVY, INK, MUTED, RULE = "0B1F3B", "0B1A30", "4E5D72", "CFD7E1"
BOARD, ROW, ZONE, ZONE2 = "FBFCFD", "F2F5F8", "EEF2F6", "E5EBF1"
BLUE, GREEN, RED, VIOLET, AMBER = "1846C9", "00843D", "D52B1E", "8E2DA8", "F2A900"
GOOD, MINOR, WHITE, GREY = "00843D", "C27A00", "FFFFFF", "8C96A5"


def rgb(hex_: str) -> RGBColor:
    return RGBColor.from_string(hex_)


# ------------------------------------------------------------------ drawing helpers
def box(slide, x, y, w, h, fill=None, line=None, width=1.0, shape=MSO_SHAPE.RECTANGLE):
    s = slide.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    if fill:
        s.fill.solid()
        s.fill.fore_color.rgb = rgb(fill)
    else:
        s.fill.background()
    if line:
        s.line.color.rgb = rgb(line)
        s.line.width = Pt(width)
    else:
        s.line.fill.background()
    s.shadow.inherit = False
    return s


def text(
    slide,
    x,
    y,
    w,
    h,
    runs,
    size=11,
    color=INK,
    bold=False,
    align=PP_ALIGN.LEFT,
    font=FONT,
    anchor=MSO_ANCHOR.TOP,
    fill=None,
    margin=0.02,
):
    """runs: a string, or a list of paragraphs, each a string or a list of (text, overrides) pieces."""
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    if fill:
        tb.fill.solid()
        tb.fill.fore_color.rgb = rgb(fill)
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = Inches(margin)
    tf.margin_top = tf.margin_bottom = Inches(0.01)
    paragraphs = [runs] if isinstance(runs, str) else runs
    for i, para in enumerate(paragraphs):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        pieces = [(para, {})] if isinstance(para, str) else para
        for piece, over in pieces:
            r = p.add_run()
            r.text = piece
            f = r.font
            f.name = over.get("font", font)
            f.size = Pt(over.get("size", size))
            f.bold = over.get("bold", bold)
            f.italic = over.get("italic", False)
            f.color.rgb = rgb(over.get("color", color))
    return tb


def line(slide, x1, y1, x2, y2, color, width=3.0, dash=None):
    c = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
    c.line.color.rgb = rgb(color)
    c.line.width = Pt(width)
    if dash:
        c.line.dash_style = dash
    return c


def polyline(slide, points, color, width=6.0, dash=None):
    """Open path through (x, y) points in inches."""
    emu = [(int(Inches(x)), int(Inches(y))) for x, y in points]
    fb = slide.shapes.build_freeform(emu[0][0], emu[0][1], scale=1.0)
    fb.add_line_segments(emu[1:], close=False)
    s = fb.convert_to_shape()
    s.fill.background()
    s.line.color.rgb = rgb(color)
    s.line.width = Pt(width)
    if dash:
        s.line.dash_style = dash
    s.shadow.inherit = False
    return s


def dot(slide, cx, cy, r, fill, ring, ring_width=2.5):
    return box(slide, cx - r, cy - r, 2 * r, 2 * r, fill=fill, line=ring, width=ring_width, shape=MSO_SHAPE.OVAL)


def pill(slide, x, y, w, h, label, fill, color=WHITE, size=9):
    b = box(slide, x, y, w, h, fill=fill, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
    b.adjustments[0] = 0.25
    tf = b.text_frame
    tf.margin_left = tf.margin_right = Inches(0.03)
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = label
    r.font.name, r.font.size, r.font.bold, r.font.color.rgb = FONT, Pt(size), True, rgb(color)
    return b


def header(slide, title, subtitle, number):
    box(slide, 0, 0, 13.333, 0.85, fill=NAVY)
    # mark: a line segment through an interchange ring — a generic map symbol, not any agency's logo
    box(slide, 0.3, 0.38, 0.66, 0.09, fill=BLUE)
    dot(slide, 0.63, 0.425, 0.15, WHITE, AMBER, ring_width=4)
    text(slide, 1.15, 0.14, 8.8, 0.6, title, size=24, color=WHITE, font=DISPLAY, anchor=MSO_ANCHOR.MIDDLE)
    text(
        slide, 9.6, 0.16, 3.45, 0.56, subtitle, size=9.5, color="C9D3E0", align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE
    )
    line(slide, 0.4, 7.0, 12.93, 7.0, INK, width=1.5)
    text(slide, 12.2, 7.05, 0.75, 0.3, f"{number} / 3", size=9, color=MUTED, align=PP_ALIGN.RIGHT)


def footer(slide, label, body):
    text(slide, 0.4, 7.05, 11.6, 0.35, [[(label + " ", {"bold": True, "color": INK}), (body, {})]], size=9, color=MUTED)


# ------------------------------------------------------------------ slide 1: network map
def slide_architecture(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    header(
        s,
        "Network map: from a failed run to a verified fix",
        ["LangGraph · SQLite checkpoints", "every change needs your approval"],
        1,
    )
    X = lambda u: 0.45 + u * 0.01025  # noqa: E731  map units → inches
    Y = lambda u: 1.0 + (u - 40) * 0.01025  # noqa: E731

    zones = [
        (20, 120, ZONE2, "Zone 1 · Data"),
        (140, 190, ZONE, "Zone 2 · Detect"),
        (330, 500, ZONE2, "Zone 3 · Diagnose (agents)"),
        (830, 100, ZONE, "Zone 4 · You"),
        (930, 170, ZONE2, "Zone 5 · Fix"),
        (1100, 80, ZONE, "Zone 6 · Learn"),
    ]
    for ux, uw, fill, label in zones:
        box(s, X(ux), Y(40), uw * 0.01025, 420 * 0.01025, fill=fill)
        text(
            s, X(ux), Y(46), uw * 0.01025, 0.25, label.upper(), size=7.5, color=MUTED, bold=True, align=PP_ALIGN.CENTER
        )
    zone_fill = lambda ux: next(f for zx, zw, f, _ in zones if zx <= ux < zx + zw)  # noqa: E731

    # lines (drawn first, labels go on top)
    polyline(s, [(X(1140), Y(250)), (X(1140), Y(120)), (X(620), Y(120)), (X(580), Y(160)), (X(580), Y(250))], GREEN, 9)
    text(
        s,
        X(640),
        Y(96),
        4.9,
        0.22,
        "MEMORY LINE · verified fixes become hints for the next incident",
        size=8.5,
        color=GREEN,
        bold=True,
        align=PP_ALIGN.CENTER,
    )
    polyline(
        s,
        [(X(1050), Y(250)), (X(1050), Y(192)), (X(730), Y(192)), (X(690), Y(232)), (X(690), Y(250))],
        VIOLET,
        5,
        MSO_LINE_DASH_STYLE.DASH,
    )
    text(
        s,
        X(740),
        Y(168),
        3.2,
        0.22,
        "RETRY LOOP · re-diagnose once with the new error",
        size=8.5,
        color=VIOLET,
        bold=True,
        align=PP_ALIGN.CENTER,
    )
    polyline(s, [(X(690), Y(250)), (X(690), Y(380)), (X(730), Y(420)), (X(1005), Y(420))], RED, 9)
    polyline(s, [(X(880), Y(250)), (X(880), Y(380)), (X(900), Y(420))], RED, 9)
    text(s, X(735), Y(430), 1.6, 0.2, "ESCALATION SPUR", size=8.5, color=RED, bold=True)
    line(s, X(60), Y(250), X(1140), Y(250), BLUE, width=11)
    text(s, X(55), Y(218), 1.4, 0.22, "INCIDENT LINE", size=8.5, color=BLUE, bold=True)
    dot(s, X(1018), Y(420), 0.11, BOARD, RED, 4)
    text(
        s,
        X(1035),
        Y(404),
        1.8,
        0.42,
        [[("A person decides", {"bold": True, "size": 11})], "unsure · no safe fix · rejected"],
        size=8,
        color=MUTED,
    )

    stations = [
        (80, "Databricks", "loghub_etl · 5 tasks", "audit tables", "stop"),
        (185, "Watcher", "Jobs API · 60 s", "incident saved first", "stop"),
        (285, "Evidence", "diff vs last good run", "5 read-only tools", "stop"),
        (385, "Incident", "class by rule", "error code → kind", "stop"),
        (480, "Log analysis", "facts by rule", "distilled query", "stop"),
        (580, "Knowledge", "corrective RAG", "Qdrant · rerank", "ring"),
        (690, "Root cause", "one call · qwen3:4b", "calibrated", "ring"),
        (785, "Resolution", "undo what changed", "guardrail", "stop"),
        (880, "Approval", "email buttons", "console · CLI", "human"),
        (970, "Rerun", "approved fix", "on Databricks", "stop"),
        (1050, "Verify", "all tasks", "passed?", "ring"),
        (1140, "Learn", "memory +", "track record", "learn"),
    ]
    for ux, name, d1, d2, kind in stations:
        cx, cy = X(ux), Y(250)
        if kind == "stop":
            dot(s, cx, cy, 0.075, BOARD, BLUE, 3)
        elif kind == "ring":
            dot(s, cx, cy, 0.12, BOARD, INK, 4)
        elif kind == "human":
            dot(s, cx, cy, 0.14, AMBER, INK, 4)
        else:
            dot(s, cx, cy, 0.12, GREEN, INK, 4)
        text(
            s,
            cx - 0.56,
            cy + 0.2,
            1.12,
            0.62,
            [[(name, {"bold": True, "size": 10.5, "color": INK})], d1, d2],
            size=7.5,
            color=MUTED,
            align=PP_ALIGN.CENTER,
            fill=zone_fill(ux),
        )

    # key
    ky = 5.5
    for i, (color, label, dash) in enumerate(
        [
            (BLUE, "Incident Line, every failure", None),
            (GREEN, "Memory Line, learning", None),
            (RED, "Escalation spur, to a person", None),
            (VIOLET, "Retry loop", MSO_LINE_DASH_STYLE.DASH),
        ]
    ):
        x = 0.5 + i * 2.65
        line(s, x, ky + 0.1, x + 0.42, ky + 0.1, color, width=5, dash=dash)
        text(s, x + 0.5, ky, 2.1, 0.22, label, size=9)
    dot(s, 11.18, ky + 0.1, 0.08, BOARD, INK, 3)
    text(s, 11.32, ky, 1.7, 0.22, "Interchange", size=9)
    # interchanges strip
    rails = [
        ("Ollama · qwen3:4b", "root cause + fix, fully local; Claude optional"),
        ("Qdrant", "4,957 error docs, code, runbooks, tickets, incident memory"),
        ("SQLite", "incidents, evidence, history, memories, LangGraph checkpoints"),
        ("Langfuse · Grafana", "traces + eval scores · pipeline dashboard"),
        ("Gmail", "investigating · approve / deny · resolved"),
    ]
    for i, (title, body) in enumerate(rails):
        x = 0.45 + i * 2.5
        line(s, x, 5.95, x + 2.35, 5.95, INK, width=3)
        text(
            s, x, 6.0, 2.35, 0.85, [[(title, {"bold": True, "size": 10.5, "color": INK})], body], size=8.5, color=MUTED
        )
    footer(
        s,
        "Rules of the network:",
        "agents never call each other, the graph does · nothing changes without approval · "
        "reruns may only reset or restore the last working value.",
    )


# ------------------------------------------------------------------ slide 2: line status (scorecard)
def slide_scorecard(prs, all21: dict, test_off: dict, test_mem: dict, retrieval: dict):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    header(
        s,
        "Line status: how well the copilot diagnoses",
        ["21 real Databricks failures · 9 held out", "fully local · qwen3:4b on Ollama"],
        2,
    )
    n = all21["incidents"]
    cause, action = round(all21["fact_ok"] * n), round(all21["action_ok"] * n)
    rows = [
        (
            BLUE,
            "Root cause",
            "names the exact parameter, date, table or path",
            "Good service",
            GOOD,
            f"{cause} / {n}",
            "",
        ),
        (
            AMBER,
            "Fix action",
            "right rerun, or correctly none",
            "Minor delays" if action < n else "Good service",
            MINOR if action < n else GOOD,
            f"{action} / {n}",
            "2 train incidents (mi-03, dq-03), both sent to a person",
        ),
        (
            GREEN,
            "Held-out test",
            "never seen in tickets or memory",
            "Good service",
            GOOD,
            f"{round(test_off['all_ok'] * test_off['incidents'])} / {test_off['incidents']}",
            "fully correct: class · cause · action",
        ),
        (
            VIOLET,
            "Context recall",
            "RAGAS-style · labelled docs that were cited",
            "Good service",
            GOOD,
            f"{all21['context_recall']:.2f} · {test_off['context_recall']:.2f} · {test_mem['context_recall']:.2f}",
            "all 21 · test · test with memory",
        ),
        (
            VIOLET,
            "Context precision",
            "RAGAS-style · cited docs that are relevant",
            "Good service",
            GOOD,
            " · ".join(f"{run['context_precision']:.2f}" for run in (all21, test_off, test_mem)),
            "all 21 · test · test with memory",
        ),
        (
            RED,
            "Faithfulness",
            "RAGAS-style · no fact in the cause is invented",
            "Minor delays",
            MINOR,
            f"{all21['faithfulness']:.2f} · {test_off['faithfulness']:.2f} · {test_mem['faithfulness']:.2f}",
            "memory once misled (dq-04)",
        ),
        (
            INK,
            "Journey time",
            "per incident, end to end",
            "Good service",
            GOOD,
            f"{test_off['mean_seconds']:.0f} s",
            "was 167 s · 9 incidents ≈ 2½ min (was ≈ 25)",
        ),
    ]
    x0, y0, rh = 0.45, 1.35, 0.68
    text(s, x0 + 0.2, 1.05, 3, 0.25, "LINE", size=8, color=MUTED, bold=True)
    text(s, x0 + 3.55, 1.05, 1.5, 0.25, "SERVICE", size=8, color=MUTED, bold=True)
    text(s, x0 + 5.15, 1.05, 2.5, 0.25, "READING", size=8, color=MUTED, bold=True)
    line(s, x0, 1.3, x0 + 7.55, 1.3, INK, width=1.5)
    for i, (tab, name, desc, status, status_color, value, why) in enumerate(rows):
        y = y0 + i * rh
        if i % 2 == 0:
            box(s, x0, y, 7.55, rh, fill=ROW)
        box(s, x0, y + 0.06, 0.09, rh - 0.12, fill=tab)
        text(
            s,
            x0 + 0.2,
            y + 0.08,
            3.3,
            rh - 0.1,
            [[(name, {"bold": True, "size": 13, "color": INK})], desc],
            size=8.5,
            color=MUTED,
        )
        pill(s, x0 + 3.55, y + 0.17, 1.45, 0.33, status.upper(), status_color, size=8.5)
        text(
            s,
            x0 + 5.15,
            y + 0.07,
            2.4,
            rh - 0.1,
            [[(value, {"bold": True, "size": 13, "color": INK})], why],
            size=8,
            color=MUTED,
        )
        line(s, x0, y + rh, x0 + 7.55, y + rh, RULE, width=0.75)

    # Charts and the table are drawn with plain shapes, not native chart/table objects: phone viewers
    # (e.g. files opened from WhatsApp) often cannot render PowerPoint charts and draw them wrongly.
    bar_panel(
        s,
        8.25,
        1.0,
        4.75,
        2.75,
        "RAGAS-style quality (0–1)",
        ["Context recall", "Context precision", "Faithfulness"],
        [
            ("All 21", GREY, [all21[k] for k in ("context_recall", "context_precision", "faithfulness")]),
            ("Test 9", BLUE, [test_off[k] for k in ("context_recall", "context_precision", "faithfulness")]),
            ("Test 9 + memory", GREEN, [test_mem[k] for k in ("context_recall", "context_precision", "faithfulness")]),
        ],
        maximum=1.0,
        fmt="{:.2f}",
    )
    bar_panel(
        s,
        8.25,
        3.85,
        4.75,
        1.55,
        "Confidence accuracy · Brier (lower is better)",
        ["All 21", "Test + memory"],
        [
            ("Model alone", MINOR, [all21["brier_model"], test_mem["brier_model"]]),
            ("Evidence-calibrated", GOOD, [all21["brier_calibrated"], test_mem["brier_calibrated"]]),
        ],
        maximum=0.12,
        fmt="{:.3f}",
    )

    # retrieval departures: a grid of text boxes on a navy panel
    box(s, 8.3, 5.47, 4.7, 1.43, fill=NAVY)
    text(
        s,
        8.42,
        5.5,
        4.5,
        0.25,
        [
            [
                ("Retrieval departures  ", {"bold": True, "size": 11, "color": WHITE}),
                ("21 failures · top 5", {"size": 8, "color": "C9D3E0"}),
            ]
        ],
    )
    cols = [
        ("Setup", 1.6, PP_ALIGN.LEFT),
        ("MRR", 0.85, PP_ALIGN.RIGHT),
        ("nDCG", 0.85, PP_ALIGN.RIGHT),
        ("Runbook@5", 1.05, PP_ALIGN.RIGHT),
    ]
    rows = [
        (name, retrieval[key])
        for name, key in [
            ("dense", "structure/dense"),
            ("keyword", "structure/bm25"),
            ("hybrid", "structure/hybrid"),
            ("▶ hybrid + rerank", "structure/hybrid+rerank"),
        ]
    ]
    y = 5.8
    x = 8.45
    for title, width, align in cols:
        text(s, x, y, width, 0.2, title.upper(), size=7, color="C9D3E0", bold=True, align=align)
        x += width
    line(s, 8.45, y + 0.21, 12.8, y + 0.21, "C9D3E0", width=0.75)
    for r, (name, m) in enumerate(rows):
        yy = y + 0.25 + r * 0.2
        live = r == len(rows) - 1
        x = 8.45
        for (_title, width, align), value in zip(
            cols, [name, f"{m['mrr']:.2f}", f"{m['ndcg']:.2f}", f"{m['runbook_hit']:.2f}"], strict=True
        ):
            text(s, x, yy, width, 0.2, value, size=8.5, color=AMBER if live else WHITE, bold=live, align=align)
            x += width
    footer(
        s,
        "How it is scored:",
        "objective checks vs known answers (no LLM judge) · RAGAS-style metrics from our labels · "
        "scores on Langfuse traces · regression gate blocks drops · dq-04 misled by memory → escalated.",
    )


def bar_panel(slide, x, y, w, h, title, categories, series, maximum, fmt):
    """Grouped horizontal bars drawn with rectangles (renders in every viewer), with value labels."""
    box(slide, x, y, w, h, fill=WHITE, line=RULE, width=0.75)
    text(slide, x + 0.12, y + 0.06, w - 0.24, 0.26, title, size=10.5, bold=True)
    legend_y = y + h - 0.27
    lx = x + 0.12
    for name, color, _ in series:
        box(slide, lx, legend_y + 0.06, 0.14, 0.12, fill=color)
        text(slide, lx + 0.18, legend_y, 1.35, 0.24, name, size=8, color=MUTED)
        lx += 0.32 + len(name) * 0.07
    label_w, value_w = 1.2, 0.48
    track_x, track_w = x + 0.12 + label_w, w - 0.24 - label_w - value_w
    top, bottom = y + 0.4, legend_y - 0.06
    group_h = (bottom - top) / len(categories)
    bar_h = min(0.2, (group_h - 0.1) / len(series))
    for i, category in enumerate(categories):
        gy = top + i * group_h
        text(
            slide, x + 0.12, gy, label_w - 0.08, group_h - 0.05, category, size=8.5, color=INK, anchor=MSO_ANCHOR.MIDDLE
        )
        for j, (_, color, values) in enumerate(series):
            by = gy + (group_h - len(series) * bar_h) / 2 + j * bar_h
            box(slide, track_x, by + 0.02, track_w, bar_h - 0.04, fill=ROW)
            box(
                slide,
                track_x,
                by + 0.02,
                max(0.02, track_w * min(values[i], maximum) / maximum),
                bar_h - 0.04,
                fill=color,
            )
            text(
                slide,
                track_x + track_w + 0.04,
                by - 0.01,
                value_w,
                bar_h,
                fmt.format(values[i]),
                size=7.5,
                bold=True,
                anchor=MSO_ANCHOR.MIDDLE,
            )


# ------------------------------------------------------------------ slide 3: control centre
def slide_observability(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    header(
        s,
        "Control centre: traces, plumbing, audit",
        ["verified against the live data, 5 Oct 2026", "Langfuse · Databricks · SQLite · git"],
        3,
    )

    def panel_title(x, tag, title, w):
        pill(s, x, 1.05, 0.95, 0.27, tag, NAVY, size=8)
        text(s, x + 1.05, 1.03, w - 1.05, 0.32, title, size=13, bold=True, anchor=MSO_ANCHOR.MIDDLE)
        line(s, x, 1.4, x + w, 1.4, INK, width=1.5)

    # --- Langfuse (CCTV)
    x, w = 0.45, 4.55
    panel_title(x, "CCTV", "Langfuse traces", w)
    for i, (value, label) in enumerate(
        [("100+", "diagnosis traces, 2 days"), ("67", "intake traces"), ("8", "eval scores per trace")]
    ):
        bx = x + i * 1.53
        box(s, bx, 1.5, 1.45, 0.62, line=INK, width=1.25)
        text(
            s,
            bx + 0.08,
            1.53,
            1.3,
            0.6,
            [[(value, {"bold": True, "size": 16, "color": INK})], label],
            size=7.5,
            color=MUTED,
        )
    box(s, x, 2.22, w, 3.5, fill=NAVY)
    tree = [
        (0, "chain", None, "diagnose-incident", "session INC-0010"),
        (1, "span", None, "node-knowledge", ""),
        (2, "retriever", BLUE, "search-dense · search-keyword", ""),
        (2, "retriever", BLUE, "search-memory", "→ memory:1 · 0.90"),
        (2, "span", None, "fuse-rankings → rerank-documents", ""),
        (1, "span", None, "node-root_cause", ""),
        (2, "generation", AMBER, "ollama/qwen3:4b", "1,458 in · 270 out"),
        (1, "span", None, "node-resolution", ""),
        (2, "guardrail", RED, "check-safe-action", "→ allowed"),
        (0, "chain", None, "resume-incident", "after approval"),
        (1, "tool", GREEN, "rerun-job → verify-rerun", ""),
    ]
    for i, (depth, kind, color, name, note) in enumerate(tree):
        y = 2.32 + i * 0.3
        bx = x + 0.12 + depth * 0.25
        if color:
            pill(s, bx, y + 0.03, 0.82, 0.2, kind, color, color=INK if color == AMBER else WHITE, size=6.5)
        else:
            b = box(s, bx, y + 0.03, 0.82, 0.2, line="C9D3E0", width=0.75, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
            b.text_frame.margin_left = b.text_frame.margin_right = Inches(0.02)
            b.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
            p = b.text_frame.paragraphs[0]
            p.alignment = PP_ALIGN.CENTER
            r = p.add_run()
            r.text = kind
            r.font.name, r.font.size, r.font.color.rgb = FONT, Pt(6.5), rgb("C9D3E0")
        text(
            s,
            bx + 0.9,
            y,
            w - (bx - x) - 1.0,
            0.27,
            [[(name, {"bold": True, "color": WHITE}), (f"   {note}" if note else "", {"color": "C9D3E0"})]],
            size=8.5,
            anchor=MSO_ANCHOR.MIDDLE,
        )
    box(s, x, 5.85, 0.07, 0.9, fill=RED)
    text(
        s,
        x + 0.17,
        5.84,
        3.0,
        0.92,
        [
            [("Secret scan", {"bold": True, "size": 12, "color": INK})],
            "273 recorded steps · 519 KB of inputs and outputs · tokens, keys and approval links",
        ],
        size=8,
        color=MUTED,
    )
    pill(s, x + 3.35, 6.1, 1.15, 0.33, "0 FOUND", GOOD, size=9)

    # --- log plumbing (signals)
    x, w = 5.25, 3.85
    panel_title(x, "SIGNALS", "Log plumbing, per incident", w)
    checks = [
        ("Failed task identified", "watcher vs the run log", "21/21", GOOD),
        ("Error + trace captured", "from the Jobs API", "23/23", GOOD),
        ("Error = Databricks export", "first line, word for word", "21/21", GOOD),
        ("Error code survives cleaning", "22 KB trace → short clue", "23/23", GOOD),
        ("Last good run found", "for the 'what changed' diff", "23/23", GOOD),
        ("Audit row in pipeline_runs", "1 record_run timeout → Jobs API fallback", "22/23", MINOR),
        ("Runs stamped with git code version", "deploy refuses uncommitted code", "27/27", GOOD),
        ("Quality failures in dq_results", "one row per broken rule", "5 = 5", GOOD),
    ]
    for i, (what, why, result, color) in enumerate(checks):
        y = 1.5 + i * 0.66
        text(s, x, y, 2.75, 0.6, [[(what, {"bold": True, "size": 10.5, "color": INK})], why], size=8, color=MUTED)
        dot(s, x + 2.95, y + 0.17, 0.07, color, color, 1)
        text(s, x + 3.08, y + 0.04, 0.8, 0.3, result, size=11, bold=True, align=PP_ALIGN.RIGHT)
        line(s, x, y + 0.62, x + w, y + 0.62, RULE, width=0.75)

    # --- audit (journey log)
    x, w = 9.35, 3.6
    panel_title(x, "JOURNEY", "Audit: INC-0020, start to fix", w)
    stops = [
        ("4 Oct · 18:18 UTC", "Opened", "run 529641498051010 failed in gold_aggregate", BOARD, BLUE),
        ("19:29", "Diagnosed", "gold_table changed → reset it; awaiting approval", BOARD, BLUE),
        ("20:04:09", "Approved by operator, via the email link", "single-use link consumed", AMBER, INK),
        ("20:04:11", "Rerun 260352217371041", "started, gold_table back to default", BOARD, BLUE),
        ("20:06:15", "Verified · resolved", "every task passed", GREEN, INK),
        ("20:14", "Learned · memory:1", "cited by the next similar incident", GREEN, INK),
    ]
    line(s, x + 0.12, 1.62, x + 0.12, 1.55 + 5 * 0.6 + 0.15, BLUE, width=5)
    for i, (when, what, detail, fill, ring) in enumerate(stops):
        y = 1.55 + i * 0.6
        dot(s, x + 0.12, y + 0.12, 0.085, fill, ring, 2.5)
        text(
            s,
            x + 0.32,
            y,
            w - 0.35,
            0.58,
            [
                [(when, {"size": 7.5, "color": MUTED, "bold": True})],
                [(what, {"bold": True, "size": 9.5, "color": INK})],
                detail,
            ],
            size=8,
            color=MUTED,
        )
    chips = ["who", "how", "when", "evidence", "git version", "162 checkpoints", "1,335 history entries"]
    cx, cy = x, 5.25
    for chip in chips:
        cw = 0.12 + len(chip) * 0.062
        if cx + cw > x + w:
            cx, cy = x, cy + 0.3
        b = box(s, cx, cy, cw, 0.24, line=INK, width=1, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
        b.adjustments[0] = 0.5
        tf = b.text_frame
        tf.margin_left = tf.margin_right = Inches(0.02)
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        r = p.add_run()
        r.text = chip
        r.font.name, r.font.size, r.font.bold, r.font.color.rgb = FONT, Pt(7.5), True, rgb(INK)
        cx += cw + 0.06
    box(s, x, 5.95, 0.06, 0.85, fill=AMBER)
    text(
        s,
        x + 0.14,
        5.93,
        w - 0.15,
        0.9,
        [
            [
                ("Fixed today: ", {"bold": True, "color": INK}),
                (
                    "guardrail check and memory search were not traced; Grafana tokens were not masked; "
                    "evals had written 13 re-diagnoses into INC-0020's real history (evals now use a copy).",
                    {},
                ),
            ]
        ],
        size=8,
        color=MUTED,
    )
    footer(
        s,
        "Verified, not assumed:",
        "every number here was read back from Langfuse, Databricks (Jobs API, audit tables) "
        "and the SQLite incident store on 5 Oct 2026.",
    )


def widen_template(prs, factor: float) -> None:
    """python-pptx's default template is 4:3; stretch the master and layouts to the 16:9 slides so viewers
    that position things from the master (some phone apps do) get the same geometry as PowerPoint."""
    for container in [prs.slide_master, *prs.slide_layouts]:
        for shape in container.shapes:
            shape.left, shape.width = int(shape.left * factor), int(shape.width * factor)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    all21 = json.loads((REPORTS / "diagnosis_all_memory-off.json").read_text())["summary"]
    test_off = json.loads((BASELINES / "diagnosis_test_memory-off.json").read_text())["summary"]
    test_mem = json.loads((BASELINES / "diagnosis_test_memory-from-train.json").read_text())["summary"]
    retrieval = {r["config"]: r for r in json.loads((REPORTS / "retrieval_results.json").read_text())}

    prs = Presentation()
    old_width = prs.slide_width
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    widen_template(prs, prs.slide_width / old_width)
    slide_architecture(prs)
    slide_scorecard(prs, all21, test_off, test_mem, retrieval)
    slide_observability(prs)
    for slide in prs.slides:  # page background
        bg = slide.background.fill
        bg.solid()
        bg.fore_color.rgb = rgb(BOARD)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(args.out)
    print(f"wrote {args.out} ({args.out.stat().st_size // 1024} KB, {len(prs.slides)} slides)")


if __name__ == "__main__":
    main()
