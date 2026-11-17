"""Diagnosis metrics — objective, no LLM judge.

Correctness (against failure_scenarios.yaml):
- class_ok   root cause class == the scenario's class
- fact_ok    the cause names the scenario's distinctive value (date, column, table, format, size, path)
- action_ok  reset the wrongly changed parameter when a rerun fixes it; none when a person must act first

RAGAS-style (the same ideas as RAGAS, computed from our labels instead of an LLM judge):
- context_recall     share of the labelled relevant documents that were cited (capped at the citation count)
- context_precision  share of citations that are labelled relevant
- faithfulness       share of the concrete facts in the cause (quoted values, dates, table names, numbers)
                     that appear in the evidence or the cited documents, i.e. not invented

Calibration: Brier score and expected calibration error (ECE) of a confidence against "all correct".
"""

import re

from evals.dataset_loader import scenario_params

FIXABLE = {"input_path", "timestamp_format", "gold_table", "debug_sample_rows"}  # a reset fixes these
RESET_VALUES = {"", "0"}
NATIVE_FORMATS = {"yy/MM/dd HH:mm:ss", "yyMMdd HHmmss"}


def key_facts(scenario: dict) -> list[list[str]]:
    """Facts the cause must mention: one list of acceptable tokens per changed parameter."""
    params = scenario_params(scenario)
    changed = {k: v for k, v in params.items() if k not in {"source", "run_date", "silver_write_mode"}}
    if not changed:  # date-driven: the missing / corrupted day is the fact
        return [[params["run_date"]]]
    facts = []
    for key, value in changed.items():
        if key == "enrich_columns":
            facts.append([c.strip() for c in value.split(",")])
        elif key == "input_path":
            facts.append([value] + [marker for marker in ("raw_logs_v2", ".gz") if marker in value])
        elif key == "gold_table":
            facts.append([value, value.rsplit(".", 1)[0], "samples"])
        elif key == "debug_sample_rows":
            facts.append([value, f"{int(value):,}", f"{int(value) // 1_000_000}m", "debug_sample", "collect"])
        else:
            facts.append([value])
    return facts


def fact_ok(scenario: dict, cause: str) -> bool:
    text = cause.lower()
    return all(any(token.lower() in text for token in options) for options in key_facts(scenario))


def action_ok(scenario: dict, resolution: dict | None) -> bool:
    if resolution is None:
        return False
    action = (resolution.get("action") or {}).get("parameters")
    params = scenario_params(scenario)
    fixable = [k for k in params if k in FIXABLE]
    if scenario["failure_class"] == "schema_change":
        return action is None or action.get("silver_write_mode") == "overwrite" or action.get("enrich_columns") == ""
    if not fixable:  # missing or corrupted delivery: a person must act first
        return action is None
    if action is None:
        return False
    value = action.get(fixable[0])
    return value in RESET_VALUES or (fixable[0] == "timestamp_format" and value in NATIVE_FORMATS)


def context_scores(cited: list[str], relevant: set[str], cite_limit: int) -> tuple[float, float]:
    """(recall, precision) of the cited parent ids against the labelled relevant set."""
    hits = len(set(cited) & relevant)
    recall = hits / min(len(relevant), cite_limit) if relevant else 0.0
    precision = hits / len(cited) if cited else 0.0
    return round(min(recall, 1.0), 3), round(precision, 3)


FACT_PATTERNS = [
    r"'([^']{3,})'",  # quoted values
    r"\b(\d{4}-\d{2}-\d{2})\b",  # dates
    r"\b([a-z_]+\.[a-z_]+(?:\.[a-z_]+)?)\b",  # catalog.schema(.table)
    r"\b(\d{1,3}(?:,\d{3})+|\d{4,})\b",  # large numbers
    r"\b([a-z]+_[a-z_]+)\b",  # identifiers like gold_table, raw_logs_v2
]


def facts_in(cause: str) -> set[str]:
    out = set()
    for pattern in FACT_PATTERNS:
        out |= {m.lower() for m in re.findall(pattern, cause, re.I)}
    return out


def faithfulness(cause: str, context: str) -> float:
    """Share of the cause's concrete facts found in the evidence + citations (1.0 when it states none)."""
    facts = facts_in(cause)
    if not facts:
        return 1.0
    text = context.lower().replace(",", "")
    supported = [f for f in facts if f.replace(",", "") in text]
    return round(len(supported) / len(facts), 3)


def brier(confidences: list[float], correct: list[bool]) -> float:
    return round(sum((c - float(ok)) ** 2 for c, ok in zip(confidences, correct, strict=True)) / len(correct), 4)


def ece(confidences: list[float], correct: list[bool], bins: int = 5) -> float:
    """Expected calibration error: |accuracy - mean confidence| per confidence bin, weighted by bin size."""
    total, n = 0.0, len(correct)
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, c in enumerate(confidences) if lo <= c < hi or (b == bins - 1 and c == 1.0)]
        if idx:
            accuracy = sum(correct[i] for i in idx) / len(idx)
            mean_conf = sum(confidences[i] for i in idx) / len(idx)
            total += len(idx) / n * abs(accuracy - mean_conf)
    return round(total, 4)
