"""Safely benchmark a local MLX model on the kinds of short tasks it would do for us.

Usage: python -m scripts.bench_local_llm [model]
"""

import gc
import sys
import time

import mlx.core as mx
from mlx_lm import load, stream_generate

from pipeline_incident_copilot.guardrails.resource.memory_guard import GB, check_and_limit, free_memory_pct, release

MODEL = sys.argv[1] if len(sys.argv) > 1 else "prism-ml/Ternary-Bonsai-8B-mlx-2bit"
MAX_TOKENS = 150

TASKS = {
    "query_rewrite": (
        "Rewrite this Databricks job failure into one concise search query for an error "
        "knowledge base. Output only the query.\n\n"
        "[DELTA_METADATA_MISMATCH] A metadata mismatch was detected when writing to the Delta "
        "table. at com.databricks.sql.transaction.tahoe.DeltaErrors$.metadataMismatch"
    ),
    "grade_doc": (
        "Is this document relevant to the error? Answer only 'yes' or 'no'.\n\n"
        "Error: Path does not exist: dbfs:/Volumes/workspace/pipeline_demo/raw_logs/spark/incoming\n"
        "Document: Error class: PATH_NOT_FOUND. SQLSTATE 42K03. Path does not exist: <path>."
    ),
    "classify": (
        "Classify this pipeline failure as one of: missing_input, schema_change, permissions, "
        "resource_exhaustion, data_quality. Output only the label.\n\n"
        "The driver has been killed unexpectedly and is restarting. This is likely because it "
        "ran out of memory or exhausted other resources."
    ),
}


def main() -> None:
    check_and_limit()
    t = time.time()
    model, tokenizer = load(MODEL)
    print(f"loaded {MODEL} in {time.time() - t:.1f}s, active memory {mx.get_active_memory() / GB:.2f} GB")

    for name, prompt in TASKS.items():
        messages = [{"role": "user", "content": prompt}]
        text = tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=False, enable_thinking=False
        )
        out, last = "", None
        for last in stream_generate(model, tokenizer, text, max_tokens=MAX_TOKENS):
            out += last.text
        print(
            f"\n[{name}] {last.generation_tokens} tok @ {last.generation_tps:.1f} tok/s "
            f"(prompt {last.prompt_tps:.0f} tok/s), peak {last.peak_memory:.2f} GB\n  → {out.strip()[:300]}"
        )

    del model, tokenizer
    gc.collect()
    release()
    print(f"\nunloaded; peak MLX memory {mx.get_peak_memory() / GB:.2f} GB; system free now {free_memory_pct()}%")


if __name__ == "__main__":
    main()
