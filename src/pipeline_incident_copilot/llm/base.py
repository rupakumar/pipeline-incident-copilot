"""What every model backend returns, so callers (and tracing) see model + token usage."""

from dataclasses import dataclass
from typing import Protocol

LOCAL_MODEL_NAME = "prism-ml/Ternary-Bonsai-8B-mlx-2bit"


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int


class LLMBackend(Protocol):
    """A model tier behind the router (ClaudeLLM, OllamaLLM). The in-process MLX model only does `generate`."""

    def generate(self, prompt: str, system: str | None = None) -> LLMResponse: ...

    def structured(self, prompt: str, schema, system: str | None = None):  # -> (BaseModel, LLMResponse)
        ...

    def investigate(
        self,
        prompt: str,
        schema,
        tools: list[dict],
        execute,
        system: str | None = None,
        max_rounds: int = 4,
        on_round=None,
    ):  # -> (BaseModel, LLMResponse, calls)
        ...
