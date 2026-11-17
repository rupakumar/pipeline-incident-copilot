"""Langfuse client for the whole app.

- credentials come from .env (LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL)
- tracing switches off cleanly when keys are missing or LANGFUSE_TRACING_ENABLED=false
- every exported span is passed through `redact()` (mask_otel_spans hook, runs at export)
"""

import os
from functools import lru_cache

from langfuse import Langfuse
from langfuse.types import MaskOtelSpansParams, MaskOtelSpansResult, OtelSpanPatch

from pipeline_incident_copilot import config  # noqa: F401  (loads .env before the client reads env vars)
from pipeline_incident_copilot.guardrails.input.secret_redactor import redact

DEFAULT_ENVIRONMENT = "development"
SERVICE_NAME = "pipeline-incident-copilot"

os.environ.setdefault("OTEL_SERVICE_NAME", SERVICE_NAME)  # shown as resource service.name


def _mask_otel_spans(*, params: MaskOtelSpansParams) -> MaskOtelSpansResult | None:
    patches = {}
    for identifier, span in params.spans.items():
        changed = {
            key: masked
            for key, value in span.attributes.items()
            if isinstance(value, str) and (masked := redact(value)) != value
        }
        if changed:
            patches[identifier] = OtelSpanPatch(set_attributes={**changed, "masking.applied": True})
    return MaskOtelSpansResult(span_patches=patches) if patches else None


def _keys_present() -> bool:
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


@lru_cache(maxsize=1)
def get_langfuse() -> Langfuse:
    enabled = _keys_present() and os.getenv("LANGFUSE_TRACING_ENABLED", "true").lower() != "false"
    return Langfuse(
        tracing_enabled=enabled,
        environment=os.getenv("LANGFUSE_TRACING_ENVIRONMENT", DEFAULT_ENVIRONMENT),
        mask_otel_spans=_mask_otel_spans,
    )


def tracing_enabled() -> bool:
    return _keys_present() and os.getenv("LANGFUSE_TRACING_ENABLED", "true").lower() != "false"
