"""Mask secrets before any text leaves the machine (logs and prompts can contain them)."""

import re

_PATTERNS = [
    (re.compile(r"dapi[0-9a-f]{20,}(-\d+)?"), "dapi***"),  # Databricks PAT
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"), "sk-ant-***"),  # Anthropic
    (re.compile(r"(sk|pk)-lf-[A-Za-z0-9\-]{8,}"), r"\1-lf-***"),  # Langfuse
    (re.compile(r"glsa_[A-Za-z0-9_]{10,}"), "glsa_***"),  # Grafana service-account token
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"), "Bearer ***"),
    (re.compile(r"(?i)(password|passwd|secret|token)(\s*[=:]\s*)\S+"), r"\1\2***"),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
