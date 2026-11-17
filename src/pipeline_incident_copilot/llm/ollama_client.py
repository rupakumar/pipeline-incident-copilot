"""Local model served by Ollama (http://localhost:11434): small, fast, free; same interface as ClaudeLLM.

- default model qwen3:4b (OLLAMA_MODEL to change; 1.7b is too weak for root cause), no thinking, temperature 0
- structured output via Ollama's JSON-schema `format`, validated with Pydantic (one retry)
- tool loop via Ollama tool calling; the final answer is a separate schema-constrained call
- the model stays loaded only briefly (keep_alive) and `unload()` frees it right away
"""

import json
import os

import requests
from pydantic import BaseModel, ValidationError

from pipeline_incident_copilot.guardrails.resource.memory_guard import require_free_memory, swap_used_gb
from pipeline_incident_copilot.llm.base import LLMResponse

MODEL = os.getenv("OLLAMA_MODEL", "qwen3:4b")
HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
NUM_CTX = 4096  # prompts are ~2.5-3.3k tokens + ~300 out; a smaller window = less memory
KEEP_ALIVE = "2m"
TIMEOUT = 300


def available() -> bool:
    try:
        return requests.get(f"{HOST}/api/version", timeout=3).ok
    except requests.RequestException:
        return False


class OllamaLLM:
    def __init__(self, model: str = MODEL, host: str = HOST):
        self.model = model
        self.host = host.rstrip("/")
        self._checked = False

    def _chat(
        self, messages: list[dict], tools: list[dict] | None = None, schema: dict | None = None, max_tokens: int = 800
    ) -> dict:
        if not self._checked:  # memory rule: refuse to load a model when the Mac is short on memory
            free = require_free_memory()
            print(f"[safety] {free}% memory free, swap {swap_used_gb():.2f} GB; loading {self.model} in Ollama")
            self._checked = True
        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": False,
            "keep_alive": KEEP_ALIVE,
            "options": {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": max_tokens},
        }
        if tools:
            body["tools"] = tools
        if schema:
            body["format"] = schema
        resp = requests.post(f"{self.host}/api/chat", json=body, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("prompt_eval_count", 0) >= NUM_CTX - max_tokens:
            print(f"[ollama] prompt of {data['prompt_eval_count']} tokens is near the {NUM_CTX} window: may be cut")
        return data

    def _response(self, data: dict, text: str) -> LLMResponse:
        return LLMResponse(
            text=text,
            model=f"ollama/{data.get('model', self.model)}",
            input_tokens=data.get("prompt_eval_count", 0),
            output_tokens=data.get("eval_count", 0),
        )

    @staticmethod
    def _messages(system: str | None, prompt: str) -> list[dict]:
        return ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]

    def generate(self, prompt: str, system: str | None = None, max_tokens: int = 1024) -> LLMResponse:
        data = self._chat(self._messages(system, prompt))
        return self._response(data, data["message"]["content"].strip())

    def _answer(self, messages: list[dict], schema: type[BaseModel], max_tokens: int = 800) -> tuple[BaseModel, dict]:
        """Schema-constrained answer, validated; one retry with the validation errors."""
        data = self._chat(messages, schema=schema.model_json_schema(), max_tokens=max_tokens)
        try:
            return schema.model_validate_json(data["message"]["content"]), data
        except ValidationError as e:
            retry = [
                *messages,
                {"role": "assistant", "content": data["message"]["content"]},
                {"role": "user", "content": f"That JSON was invalid: {e.errors(include_url=False)}. Answer again."},
            ]
            data = self._chat(retry, schema=schema.model_json_schema(), max_tokens=max_tokens)
            return schema.model_validate_json(data["message"]["content"]), data

    def structured(
        self, prompt: str, schema: type[BaseModel], system: str | None = None, max_tokens: int = 800
    ) -> tuple[BaseModel, LLMResponse]:
        parsed, data = self._answer(self._messages(system, prompt), schema, max_tokens)
        return parsed, self._response(data, parsed.model_dump_json())

    def investigate(
        self,
        prompt: str,
        schema: type[BaseModel],
        tools: list[dict],
        execute,
        system: str | None = None,
        max_rounds: int = 4,
        on_round=None,
    ) -> tuple[BaseModel, LLMResponse, list[dict]]:
        """Tool loop (same contract as ClaudeLLM.investigate); `tools` use Anthropic's
        {name, description, input_schema} shape and are converted for Ollama."""
        ollama_tools = [
            {
                "type": "function",
                "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]},
            }
            for t in tools
        ]
        messages = self._messages(system, prompt)
        calls, tokens_in, tokens_out = [], 0, 0
        for round_no in range(max_rounds):
            data = self._chat(messages, tools=ollama_tools, max_tokens=300)
            msg = data["message"]
            tokens_in += data.get("prompt_eval_count", 0)
            tokens_out += data.get("eval_count", 0)
            requested = msg.get("tool_calls") or []
            if on_round:
                on_round(
                    round_no,
                    f"ollama/{self.model}",
                    data.get("prompt_eval_count", 0),
                    data.get("eval_count", 0),
                    [{"tool": c["function"]["name"], "input": c["function"]["arguments"]} for c in requested],
                )
            if not requested:
                break
            messages.append({"role": "assistant", "content": msg.get("content", ""), "tool_calls": requested})
            for call in requested:
                name, args = call["function"]["name"], call["function"].get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                text, is_error = execute(name, args)
                calls.append({"tool": name, "input": args, "result": text, "is_error": is_error})
                messages.append({"role": "tool", "tool_name": name, "content": text})
        messages.append({"role": "user", "content": "Now give your final answer as JSON matching the required schema."})
        parsed, data = self._answer(messages, schema, max_tokens=400)
        tokens_in += data.get("prompt_eval_count", 0)
        tokens_out += data.get("eval_count", 0)
        if on_round:
            on_round(
                max_rounds, f"ollama/{self.model}", data.get("prompt_eval_count", 0), data.get("eval_count", 0), []
            )
        return parsed, LLMResponse(parsed.model_dump_json(), f"ollama/{self.model}", tokens_in, tokens_out), calls

    def unload(self) -> None:
        """Free the model's memory in the Ollama server now (instead of after keep_alive)."""
        try:
            requests.post(f"{self.host}/api/generate", json={"model": self.model, "keep_alive": 0}, timeout=30)
        except requests.RequestException:
            pass
