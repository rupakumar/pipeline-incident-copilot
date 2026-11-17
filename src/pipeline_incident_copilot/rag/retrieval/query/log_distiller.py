"""Rule-based log cleaning (no LLM): the baseline every LLM query method must beat.

Keeps the exception line and user-code frames; drops JVM frames, library frames, ANSI colour
codes, and the JVM stack Databricks sometimes embeds inside the error message.
"""

import re

from pipeline_incident_copilot.observability.langfuse_tracer import get_langfuse

ANSI = re.compile(r"\x1b\[[0-9;]*m|\[[0-9;]*m")
LIB_FRAME = re.compile(r"site-packages|/databricks/python|py4j|pyspark/")


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text or "")


def distill(error: str, trace: str, max_chars: int = 800) -> str:
    """Keep the exception line + user-code frames; drop JVM frames, library frames, colour codes."""
    with get_langfuse().start_as_current_observation(
        as_type="span",
        name="distill-log",
        input={"error": strip_ansi(error)[:500], "error_chars": len(error or ""), "trace_chars": len(trace or "")},
    ) as observation:
        out = _distill(error, trace, max_chars)
        observation.update(output=out, metadata={"output_chars": len(out)})
    return out


def _distill(error: str, trace: str, max_chars: int) -> str:
    lines, keep, in_lib = strip_ansi(trace).splitlines(), [], False
    for line in lines:
        s = line.strip()
        if not s or s.startswith("at ") or s.startswith("...") or set(s) <= {"-"}:
            continue
        if s.startswith("File "):
            in_lib = bool(LIB_FRAME.search(s))
            continue
        if in_lib:
            continue
        if s.startswith("---->") or s.startswith("--->") or re.match(r"^\w*(Error|Exception)\b", s):
            s = re.sub(r"\s+Traceback \(most recent call last\)$", "", s)
            keep.append(re.sub(r"^-+>\s*\d*\s*", "", s))
    # Databricks sometimes embeds the whole JVM stack in the error message itself
    error = strip_ansi(error).split("JVM stacktrace:")[0]
    out = "\n".join(line for line in error.splitlines() if not line.strip().startswith("at ")).strip()
    for line in keep:
        if line not in out and len(out) + len(line) < max_chars:
            out += "\n" + line
    return out
