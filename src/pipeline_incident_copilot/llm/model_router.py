"""Model router: each task goes to the 'local' (MLX in-process), 'ollama' (local server) or 'claude' tier.

Override any route with an env var, e.g. LLM_ROUTE_QUERY_REWRITE=claude.
Outputs are cached on disk (deterministic local decoding) so re-running evals is cheap
and doesn't reload the model.

Tracing: every model call is a Langfuse `generation` named after the action it performs
(stable names — never the model), carrying model, token usage and whether it was a cache hit.
"""

import json
import os
import re
import time
from functools import lru_cache

from pydantic import BaseModel, ValidationError

from pipeline_incident_copilot.config import RUNTIME_DIR
from pipeline_incident_copilot.llm import claude_client as claude
from pipeline_incident_copilot.llm import ollama_client as ollama
from pipeline_incident_copilot.llm.base import LOCAL_MODEL_NAME, LLMResponse
from pipeline_incident_copilot.llm.response_cache import ResponseCache
from pipeline_incident_copilot.observability.langfuse_tracer import get_langfuse

ROUTES = {
    "query_rewrite": "local",
    "multi_query": "local",
    "hyde": "ollama",  # used by the knowledge agent's retry: keep the graph on one local model
    "grade": "local",
    "classify": "ollama",  # fallback only: known errors are classified by rule
    "write_ticket": "local",
    # Heavy reasoning runs locally on Ollama by default (user choice, 2026-10-05);
    # LLM_ROUTE_ROOT_CAUSE=claude etc. switches a step to Claude.
    "root_cause": "ollama",
    "resolution": "ollama",
    "judge": "ollama",
    "improve_prompt": "ollama",  # self-improvement loop: one prompt rule per night (validated before a person merges)
}
# Langfuse observation names: verb-first, stable (dashboards and evaluators key on them)
OBSERVATION_NAMES = {
    "query_rewrite": "rewrite-query",
    "multi_query": "generate-query-variants",
    "hyde": "draft-hypothetical-document",
    "grade": "grade-relevance",
    "classify": "classify-incident",
    "write_ticket": "write-support-ticket",
    "root_cause": "analyze-root-cause",
    "resolution": "propose-resolution",
    "judge": "judge-diagnosis",
    "improve_prompt": "propose-prompt-change",
}
CACHE_PATH = RUNTIME_DIR / "llm_cache.json"


def route(task: str) -> str:
    return os.getenv(f"LLM_ROUTE_{task.upper()}", ROUTES.get(task, "claude"))


def _observation_name(task: str) -> str:
    return OBSERVATION_NAMES.get(task, task.replace("_", "-"))


def _model_name(backend: str) -> str:
    return {"local": LOCAL_MODEL_NAME, "ollama": f"ollama/{ollama.MODEL}"}.get(backend, claude.MODEL)


def _messages(system: str | None, prompt: str) -> list[dict]:
    return ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]


def _usage(response: LLMResponse) -> dict:
    return {"input": response.input_tokens, "output": response.output_tokens}


@lru_cache(maxsize=1)
def _local():
    from pipeline_incident_copilot.llm.local_mlx_client import LocalLLM  # imported lazily: loads MLX

    return LocalLLM()


@lru_cache(maxsize=1)
def _claude():
    return claude.ClaudeLLM()


@lru_cache(maxsize=1)
def _ollama():
    return ollama.OllamaLLM()


def _remote(backend: str):
    """The client for a non-MLX tier (both implement generate / structured / investigate)."""
    return _ollama() if backend == "ollama" else _claude()


def _fallback_backend() -> str:
    """Where the MLX path goes when it cannot produce valid JSON."""
    return "ollama" if ollama.available() else "claude"


def unload_local() -> None:
    """Free the in-process MLX model's memory as soon as a batch of LLM work is done."""
    if _local.cache_info().currsize:
        _local().unload()
        _local.cache_clear()


def unload_all() -> None:
    """Free every local model: MLX in this process and the Ollama server's model."""
    unload_local()
    if _ollama.cache_info().currsize:
        _ollama().unload()


_cache = ResponseCache(CACHE_PATH)
stats: list[dict] = []  # per-call latency/backends, for eval reporting
token_usage: list[dict] = []  # every Ollama/Claude call: task, model, input/output tokens (cost + speed reporting)


def _record(task, backend, start, cached):
    stats.append({"task": task, "backend": backend, "seconds": time.time() - start, "cached": cached})


def text(task: str, prompt: str, system: str | None = None, max_tokens: int = 256) -> str:
    backend, start = route(task), time.time()
    key = _cache.key("text", backend, task, system, prompt, max_tokens)
    langfuse = get_langfuse()
    if (hit := _cache.get(key)) is not None:
        # not a model call: a span, so generation counts/costs only reflect real invocations
        with langfuse.start_as_current_observation(
            as_type="span",
            name=_observation_name(task),
            input=_messages(system, prompt),
            output=hit,
            metadata={"task": task, "route": backend, "cache_hit": True},
        ):
            _record(task, backend, start, True)
            return hit
    with langfuse.start_as_current_observation(
        as_type="generation",
        name=_observation_name(task),
        input=_messages(system, prompt),
        model=_model_name(backend),
        model_parameters={"max_tokens": max_tokens, "temperature": 0},
        metadata={"task": task, "route": backend},
    ) as generation:
        response = (
            _local().generate(prompt, system, max_tokens)
            if backend == "local"
            else _remote(backend).generate(prompt, system)
        )
        generation.update(
            output=response.text, model=response.model, usage_details=_usage(response), metadata={"cache_hit": False}
        )
    _cache.put(key, response.text)
    _record(task, backend, start, False)
    return response.text


def _extract_json(raw: str) -> str:
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", raw, re.S)
    return m.group(0) if m else raw


def _template(schema: type[BaseModel]) -> str:
    """Fill-in-the-blanks JSON. Small models tend to echo a formal JSON Schema back verbatim;
    a template with placeholders gets filled in instead."""
    fields = {
        name: f"<{f.description or name}{' — or null' if not f.is_required() else ''}>"
        for name, f in schema.model_fields.items()
    }
    return json.dumps(fields, indent=1, ensure_ascii=False)


def structured(
    task: str, prompt: str, schema: type[BaseModel], system: str | None = None, max_tokens: int = 400
) -> BaseModel:
    """Local: prompt for JSON → validate → retry once → fall back to Claude (if configured).

    Traced as a `chain` (the task) holding one `generation` per model attempt and a
    `guardrail` for each schema validation.
    """
    backend, start = route(task), time.time()
    key = _cache.key("structured", backend, task, system, prompt, schema.__name__)
    langfuse = get_langfuse()
    name = _observation_name(task)
    with langfuse.start_as_current_observation(
        as_type="chain",
        name=name,
        input=_messages(system, prompt),
        metadata={"task": task, "route": backend, "schema": schema.__name__},
    ) as chain:
        if (hit := _cache.get(key)) is not None:
            chain.update(output=hit, metadata={"cache_hit": True})
            _record(task, backend, start, True)
            return schema.model_validate(hit)

        if backend == "local":
            result = _structured_local(task, prompt, schema, system, max_tokens, name)
            if result is None:
                fallback = _fallback_backend()
                result = _structured_remote(
                    fallback, prompt, schema, system, name, fallback=True, max_tokens=max_tokens
                )
                backend = f"{fallback}(fallback)"
        else:
            result = _structured_remote(backend, prompt, schema, system, name, fallback=False, max_tokens=max_tokens)
        chain.update(output=result.model_dump(), metadata={"cache_hit": False, "served_by": backend})

    _cache.put(key, result.model_dump())
    _record(task, backend, start, False)
    return result


def _structured_local(task, prompt, schema, system, max_tokens, name) -> BaseModel | None:
    """Two attempts on the local model; None means fall back to Claude."""
    langfuse = get_langfuse()
    ask = f"{prompt}\n\nRespond with ONLY this JSON object, replacing each <...> with your value:\n{_template(schema)}"
    last_error = None
    for attempt in (1, 2):
        with langfuse.start_as_current_observation(
            as_type="generation",
            name=f"{name}-llm",
            input=_messages(system, ask),
            model=LOCAL_MODEL_NAME,
            model_parameters={"max_tokens": max_tokens, "temperature": 0},
            metadata={"attempt": attempt},
        ) as generation:
            response = _local().generate(ask, system, max_tokens)
            generation.update(output=response.text, model=response.model, usage_details=_usage(response))
        with langfuse.start_as_current_observation(
            as_type="guardrail",
            name="validate-structured-output",
            input=response.text,
            metadata={"schema": schema.__name__},
        ) as check:
            try:
                result = schema.model_validate_json(_extract_json(response.text))
                check.update(output={"valid": True})
                return result
            except ValidationError as e:
                last_error = e
                check.update(output={"valid": False, "errors": e.errors(include_url=False)}, level="WARNING")
                ask = f"{ask}\n\nYour previous answer was invalid ({e.error_count()} errors). Output valid JSON only."
    if not (ollama.available() or claude.available()):
        raise ValueError(f"local model gave invalid JSON for {task}: {last_error}")
    return None


def _structured_remote(backend, prompt, schema, system, name, fallback: bool, max_tokens: int = 800) -> BaseModel:
    with get_langfuse().start_as_current_observation(
        as_type="generation",
        name=f"{name}-llm",
        input=_messages(system, prompt),
        model=_model_name(backend),
        metadata={"fallback_from_local": fallback, "route": backend},
    ) as generation:
        result, response = _remote(backend).structured(prompt, schema, system, max_tokens=max_tokens)
        generation.update(output=result.model_dump(), model=response.model, usage_details=_usage(response))
    token_usage.append(
        {"task": name, "model": response.model, "input": response.input_tokens, "output": response.output_tokens}
    )
    return result


def investigate(task: str, prompt: str, schema: type[BaseModel], tools, system: str | None = None, max_rounds: int = 4):
    """A model with read-only tools (an agent loop), answering as `schema`. Never cached: tools read live state.

    Runs on the task's route (ollama or claude). `tools` is a ToolRegistry. Traced as an `agent`
    observation; each model round is a `generation`,
    each check a `tool` (from ToolRegistry.run). Returns (answer, tool calls made).
    """
    start = time.time()
    backend = route(task)
    langfuse = get_langfuse()
    name = _observation_name(task)
    definitions = [
        {"name": t["name"], "description": t["description"], "input_schema": t["args"]} for t in tools.describe()
    ]
    results = []

    def execute(tool_name: str, tool_input: dict) -> tuple[str, bool]:
        try:
            result = tools.run(tool_name, **tool_input)
        except (KeyError, ValueError, TypeError) as e:  # unknown tool or invalid arguments
            return f"Error: {e}", True
        results.append(result)
        return json.dumps({"ok": result.ok, "summary": result.summary, "data": result.data}, default=str)[
            :6000
        ], not result.ok

    def on_round(round_no, model, tokens_in, tokens_out, requested):
        with langfuse.start_as_current_observation(
            as_type="generation", name=f"{name}-llm", model=model, metadata={"round": round_no}
        ) as generation:
            generation.update(
                output=requested or "final answer", usage_details={"input": tokens_in, "output": tokens_out}
            )

    with langfuse.start_as_current_observation(
        as_type="agent", name=name, input=_messages(system, prompt), metadata={"task": task, "route": backend}
    ) as agent:
        answer, usage, calls = _remote(backend).investigate(
            prompt, schema, definitions, execute, system=system, max_rounds=max_rounds, on_round=on_round
        )
        agent.update(
            output=answer.model_dump(),
            metadata={
                "tool_calls": len(calls),
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            },
        )
    _record(task, backend, start, False)
    token_usage.append(
        {
            "task": name,
            "model": usage.model,
            "input": usage.input_tokens,
            "output": usage.output_tokens,
            "seconds": round(time.time() - start, 1),
        }
    )
    return answer, results
