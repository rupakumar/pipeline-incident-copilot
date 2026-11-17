"""Flag instruction-like text inside data (logs, errors, documents) before an agent reads it.

Logs come from the failed system and may carry text an attacker controls (a file name, a column value). The
agents are told to treat it as data; this marks suspicious lines so the report can say so, and the evidence
output wraps such fields as untrusted. Detection only: nothing is removed, the evidence stays complete.
"""

import re

PATTERNS = [
    r"ignore (all |any |your |the )?(previous|prior|above) (instructions|rules)",
    r"(note|message|instructions?) (to|for) (the )?(ai|assistant|agent|model|llm)",
    r"\byou are (now )?(an?|the) (ai|assistant|agent)\b",
    r"(pre-?approved|skip (the )?approval|without (asking|approval))",
    r"\b(run|execute)\b[^\n]{0,40}\bcopilot (apply|approve|reject|request-approval)\b",
    r"\bsystem prompt\b",
    r"(curl|wget)\s+https?://",
]
_COMPILED = [re.compile(p, re.I) for p in PATTERNS]


def suspicious_lines(text: str | None, limit: int = 5) -> list[str]:
    """Lines that look like instructions to an AI, verbatim (at most `limit`)."""
    hits = []
    for line in (text or "").splitlines():
        if any(p.search(line) for p in _COMPILED):
            hits.append(line.strip()[:300])
            if len(hits) >= limit:
                break
    return hits
