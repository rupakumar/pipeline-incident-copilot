"""ToolPolicy: the only tool calls the diagnose-incident subagents may make.

Prompts say "read-only"; this makes it true. Each subagent may run only its own `copilot` read commands (no shell
operators, so nothing can be chained or redirected) and read only pipeline code and knowledge files. Everything
else — writing, editing, other commands, `.env`, `data/` (store, tokens, Jenkins home) — is refused.
"""

import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath

COPILOT = ".venv/bin/copilot"
SHELL_OPERATORS = re.compile(r"[;&|`$<>\n\r\\]")
READABLE = ("databricks/notebooks/", "knowledge/", "infra/jenkins/jobs/")  # code and docs, never data/ or .env

# subagent → the copilot subcommands it may run (all read-only)
COMMANDS: dict[str, frozenset[str]] = {
    "incident-triage": frozenset({"evidence"}),
    "log-analyst": frozenset({"evidence"}),
    "knowledge-retriever": frozenset({"search"}),
    "root-cause-analyst": frozenset({"evidence", "check"}),
    "resolution-planner": frozenset({"evidence", "check-fix"}),
}
FILE_TOOLS = {"Read", "Grep", "Glob"}
# How a subagent returns its report to the skill (newer Claude Code). It only sends text to the orchestrator.
HANDBACK_TOOLS = {"SubagentHandback"}


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str = ""


def check_bash(agent: str, command: str) -> Verdict:
    allowed = COMMANDS.get(agent)
    if allowed is None:
        return Verdict(False, f"{agent or 'this agent'} may not run commands")
    if SHELL_OPERATORS.search(command):
        return Verdict(False, "shell operators (; & | ` $ < > \\ newlines) are not allowed: run one copilot command")
    try:
        words = shlex.split(command)
    except ValueError:
        return Verdict(False, "the command could not be parsed")
    if len(words) < 2 or not (words[0] == COPILOT or words[0].endswith(f"/{COPILOT}")):
        return Verdict(False, f"only `{COPILOT} <command>` may be run")
    if words[1] not in allowed:
        return Verdict(False, f"{agent} may only run: {', '.join(f'copilot {c}' for c in sorted(allowed))}")
    return Verdict(True)


def check_path(path: str | None, project_root: str) -> Verdict:
    if not path:
        return Verdict(False, f"give a path inside {', '.join(READABLE)}")
    p = PurePosixPath(path)
    root = PurePosixPath(project_root)
    if p.is_absolute():
        try:
            p = p.relative_to(root)
        except ValueError:
            return Verdict(False, "only files inside this project may be read")
    if ".." in p.parts:
        return Verdict(False, "paths with .. are not allowed")
    text = p.as_posix().rstrip("/") + "/"
    if not any(text.startswith(prefix) for prefix in READABLE):
        return Verdict(False, f"only {', '.join(READABLE)} may be read (not data/, .env or settings)")
    return Verdict(True)


def check_tool(agent: str, tool: str, tool_input: dict, project_root: str) -> Verdict:
    if tool == "Bash":
        return check_bash(agent, str(tool_input.get("command", "")))
    if tool in FILE_TOOLS:
        return check_path(tool_input.get("file_path") or tool_input.get("path"), project_root)
    if tool in HANDBACK_TOOLS:
        return Verdict(True)
    return Verdict(False, f"{tool} is not available to the diagnose-incident subagents")
