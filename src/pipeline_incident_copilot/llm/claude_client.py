"""Claude via the official Anthropic SDK.

- default model claude-opus-5 (adaptive thinking is on by default on Opus 5)
- server-side refusal fallbacks enabled (`fallbacks="default"`)
- structured outputs via `messages.parse` + Pydantic
"""

import os

import anthropic
from pydantic import BaseModel

from pipeline_incident_copilot.llm.base import LLMResponse

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


class ClaudeRefused(RuntimeError):
    pass


class ClaudeLLM:
    def __init__(self, model: str = MODEL, effort: str = "medium"):
        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort

    def _check(self, response) -> None:
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise ClaudeRefused(f"Claude declined (category={getattr(details, 'category', None)})")

    @staticmethod
    def _response(response, text: str) -> LLMResponse:
        # response.model is the model that actually served the request (may be a fallback)
        return LLMResponse(
            text=text,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def generate(self, prompt: str, system: str | None = None, max_tokens: int = 16000) -> LLMResponse:
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            output_config={"effort": self.effort},
            **({"system": system} if system else {}),
            messages=[{"role": "user", "content": prompt}],
        )
        self._check(response)
        return self._response(response, "".join(b.text for b in response.content if b.type == "text").strip())

    def investigate(
        self,
        prompt: str,
        schema: type[BaseModel],
        tools: list[dict],
        execute,
        system: str | None = None,
        max_rounds: int = 4,
        max_tokens: int = 16000,
        on_round=None,
    ) -> tuple[BaseModel, LLMResponse, list[dict]]:
        """Agentic loop: Claude may call `tools` (executed by `execute(name, input) -> (text, is_error)`)
        up to `max_rounds` times, then must answer as `schema`. Returns (answer, usage totals, calls)."""
        messages = [{"role": "user", "content": prompt}]
        calls, input_tokens, output_tokens = [], 0, 0
        for round_no in range(max_rounds + 1):
            last = round_no == max_rounds
            response = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=max_tokens,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                output_config={"effort": self.effort},
                output_format=schema,
                tools=tools,
                tool_choice={"type": "none"} if last else {"type": "auto"},  # last round: answer, no more checks
                **({"system": system} if system else {}),
                messages=messages,
            )
            self._check(response)
            input_tokens += response.usage.input_tokens
            output_tokens += response.usage.output_tokens
            if on_round:
                requested = [{"tool": b.name, "input": b.input} for b in response.content if b.type == "tool_use"]
                on_round(round_no, response.model, response.usage.input_tokens, response.usage.output_tokens, requested)
            if response.stop_reason == "max_tokens":
                raise RuntimeError("Claude hit max_tokens before answering")
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                parsed = response.parsed_output
                if parsed is None:
                    raise RuntimeError(f"no structured answer (stop_reason={response.stop_reason})")
                usage = LLMResponse(parsed.model_dump_json(), response.model, input_tokens, output_tokens)
                return parsed, usage, calls
            messages.append({"role": "assistant", "content": response.content})  # keeps thinking blocks intact
            results = []
            for block in tool_uses:
                text, is_error = execute(block.name, block.input)
                calls.append({"tool": block.name, "input": block.input, "result": text, "is_error": is_error})
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": is_error})
            messages.append({"role": "user", "content": results})
        raise RuntimeError("tool loop ended without an answer")

    def structured(
        self, prompt: str, schema: type[BaseModel], system: str | None = None, max_tokens: int = 16000
    ) -> tuple[BaseModel, LLMResponse]:
        response = self.client.beta.messages.parse(
            model=self.model,
            max_tokens=max_tokens,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            output_config={"effort": self.effort},
            output_format=schema,
            **({"system": system} if system else {}),
            messages=[{"role": "user", "content": prompt}],
        )
        self._check(response)
        parsed = response.parsed_output
        return parsed, self._response(response, parsed.model_dump_json())
