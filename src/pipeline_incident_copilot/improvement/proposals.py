"""Change proposers. Each proposes ONE change, only to files the loop may touch:

- TunableProposer: one knob of config/tunables.yaml one step up or down, inside its bounds, not tried recently
- PromptProposer: one extra rule for one agent prompt, written by the local model from tonight's wrong diagnoses;
  every line of the original that says what the model must never/only do is kept verbatim

Safety code (guardrails, auto-apply, approvals) is never a target: see ALLOWED_FILES.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from pipeline_incident_copilot.agents.base import DEFAULT_PROMPTS_DIR
from pipeline_incident_copilot.config import PROJECT_ROOT
from pipeline_incident_copilot.tuning.tunables import SEARCH_SPACE, TUNABLES_FILE, Tunables, read_tunables

PROMPT_FILES = ("root_cause.md", "resolution.md", "analyst.md")  # incident.md is a rarely used fallback
ALLOWED_FILES = {
    TUNABLES_FILE.relative_to(PROJECT_ROOT).as_posix(),
    *(f"{DEFAULT_PROMPTS_DIR.relative_to(PROJECT_ROOT).as_posix()}/{name}" for name in PROMPT_FILES),
}
PROTECTED_LINE = re.compile(r"\b(never|only|must|do not|don't|data|instruction)", re.I)


@dataclass
class Proposal:
    kind: str  # tunable / prompt
    target: str  # knob name or prompt file
    change: str  # one line, e.g. "knowledge_cite: 4 → 5"
    rationale: str
    files: dict[str, str] = field(default_factory=dict)  # repo-relative path → new content (ALLOWED_FILES only)

    def __post_init__(self):
        outside = set(self.files) - ALLOWED_FILES
        if outside:
            raise ValueError(f"the loop may not change {sorted(outside)}")


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


class TunableProposer:
    def __init__(self, tried):
        self.tried = tried  # change text → bool (decided in the last 30 days)

    def candidates(self, current: Tunables) -> list[Proposal]:
        out = []
        for name, knob in SEARCH_SPACE.items():
            if not knob.measured:
                continue
            value = getattr(current, name)
            for direction in (+1, -1):
                new = round(value + direction * knob.step, 4)
                if not knob.low <= new <= knob.high:
                    continue
                if isinstance(value, int):
                    new = int(new)
                change = f"{name}: {_fmt(value)} → {_fmt(new)}"
                if self.tried(change):
                    continue
                updated = current.model_copy(update={name: new})
                out.append(Proposal("tunable", name, change, f"try {name} one step {'up' if direction > 0 else 'down'}",
                                    {"config/tunables.yaml": _render(updated)}))  # fmt: skip
        return out

    def propose(self, current: Tunables | None = None) -> Proposal | None:
        found = self.candidates(current or read_tunables(TUNABLES_FILE))
        return found[0] if found else None


def _render(tunables: Tunables) -> str:
    import tempfile

    from pipeline_incident_copilot.tuning.tunables import write_tunables

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "tunables.yaml"
        write_tunables(tunables, path)
        return path.read_text()


class PromptProposer:
    """Asks the local model for ONE extra rule for the root-cause prompt, from the diagnoses it got wrong."""

    def __init__(self, text_model, tried, prompt_file: str = "root_cause.md"):
        self.text_model = text_model  # (task, prompt, system, max_tokens) -> str  (llm.model_router.text)
        self.tried = tried
        self.prompt_file = prompt_file

    def propose(self, failed_rows: list[dict]) -> Proposal | None:
        if not failed_rows:
            return None
        original = (DEFAULT_PROMPTS_DIR / self.prompt_file).read_text()
        cases = "\n".join(
            f"- scenario {r.get('scenario')}: expected class {r.get('class_expected', '?')}, got cause "
            f"{json.dumps(r.get('cause', ''))[:200]}; checks class/fact/action = "
            f"{r.get('class_ok')}/{r.get('fact_ok')}/{r.get('action_ok')}"
            for r in failed_rows[:5]
        )
        rule = (
            self.text_model(
                "improve_prompt",
                f"These diagnoses were wrong:\n{cases}\n\nCurrent instructions:\n{original}\n\n"
                "Write ONE new instruction (one sentence, under 40 words) that would have prevented these mistakes. "
                "It must not loosen any safety rule. Reply with the sentence only.",
                "You improve instructions for an incident root-cause analyst. Be concrete and general.",
                120,
            )
            .strip()
            .strip('"')
        )
        if not rule or len(rule) > 300 or "\n" in rule:
            return None
        change = f"{self.prompt_file}: + {rule[:80]}"
        if self.tried(change):
            return None
        candidate = original.rstrip() + f"\n- {rule}\n"
        if not keeps_protected_lines(original, candidate):
            return None
        rel = f"{DEFAULT_PROMPTS_DIR.relative_to(PROJECT_ROOT).as_posix()}/{self.prompt_file}"
        return Proposal(
            "prompt", self.prompt_file, change, f"from {len(failed_rows)} wrong diagnoses", {rel: candidate}
        )


def keeps_protected_lines(original: str, candidate: str) -> bool:
    """Every line that says what must never / only happen survives unchanged, and the prompt stays a similar size."""
    kept = set(candidate.splitlines())
    protected = [line for line in original.splitlines() if PROTECTED_LINE.search(line)]
    return all(line in kept for line in protected) and 0.8 <= len(candidate) / max(len(original), 1) <= 1.5
