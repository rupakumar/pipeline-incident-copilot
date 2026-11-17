"""Local MLX model, run in-process behind the memory guard (see safety.py)."""

import gc
import threading

import mlx.core as mx
from mlx_lm import load, stream_generate

from pipeline_incident_copilot.guardrails.resource.memory_guard import check_and_limit, release
from pipeline_incident_copilot.llm.base import LOCAL_MODEL_NAME, LLMResponse


class LocalLLM:
    def __init__(self, model_name: str = LOCAL_MODEL_NAME):
        self.model_name = model_name
        self._model = None
        self._tokenizer = None
        self._lock = threading.Lock()  # one request at a time — never parallel generation

    def _ensure_loaded(self) -> None:
        if self._model is None:
            check_and_limit()
            self._model, self._tokenizer = load(self.model_name)

    def generate(self, prompt: str, system: str | None = None, max_tokens: int = 256) -> LLMResponse:
        with self._lock:
            self._ensure_loaded()
            messages = ([{"role": "system", "content": system}] if system else []) + [
                {"role": "user", "content": prompt}
            ]
            text = self._tokenizer.apply_chat_template(
                messages, add_generation_prompt=True, tokenize=False, enable_thinking=False
            )
            # default sampler is greedy (temperature 0) → deterministic, cacheable outputs
            out, last = "", None
            for last in stream_generate(self._model, self._tokenizer, text, max_tokens=max_tokens):
                out += last.text
            return LLMResponse(
                text=out.strip(),
                model=self.model_name,
                input_tokens=last.prompt_tokens if last else 0,
                output_tokens=last.generation_tokens if last else 0,
            )

    def unload(self) -> None:
        with self._lock:
            self._model = self._tokenizer = None
            gc.collect()
            release()

    @property
    def peak_memory_gb(self) -> float:
        return mx.get_peak_memory() / 1024**3
