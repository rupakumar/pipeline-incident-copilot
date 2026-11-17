"""Claude Code hook entry point for the diagnose-incident subagents (see their frontmatter in .claude/agents/).

  pre  <agent>: PreToolUse  — exit 2 refuses a call that breaks ToolPolicy (the reason goes back to the agent)
  post <agent>: PostToolUse — append the call to the audit trail (data/runtime/claude_skill/audit/<session>.jsonl)

Reads the hook JSON from stdin. Kept light (no models, no network) so every tool call stays fast.
"""

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from pipeline_incident_copilot.guardrails.agent.tool_policy import check_tool
from pipeline_incident_copilot.guardrails.input.secret_redactor import redact

AUDIT_DIR = Path(os.getenv("COPILOT_AUDIT_DIR", "data/runtime/claude_skill/audit"))
INCIDENT_ID = re.compile(r"INC-\d{4,}")


def pre(agent: str, event: dict) -> int:
    verdict = check_tool(
        agent, event.get("tool_name", ""), event.get("tool_input") or {}, event.get("cwd") or os.getcwd()
    )
    if verdict.allowed:
        return 0
    print(f"Blocked by the copilot's tool policy: {verdict.reason}", file=sys.stderr)
    _append(agent, event, blocked=verdict.reason)
    return 2


def post(agent: str, event: dict) -> int:
    _append(agent, event)
    return 0


def _append(agent: str, event: dict, blocked: str | None = None) -> None:
    tool_input = event.get("tool_input") or {}
    target = tool_input.get("command") or tool_input.get("file_path") or tool_input.get("path") or ""
    response = event.get("tool_response")
    text = response if isinstance(response, str) else json.dumps(response, default=str) if response else ""
    record = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "session": event.get("session_id"),
        "agent": agent,
        "tool": event.get("tool_name"),
        "target": redact(str(target))[:500],
        "incidents": sorted(set(INCIDENT_ID.findall(str(target)))),
        "blocked": blocked,
        "result_chars": len(text),
        "result_head": redact(text[:300]),
    }
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_DIR / f"{event.get('session_id') or 'unknown'}.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")


def main() -> None:
    """Fails closed: any problem in the pre-check blocks the call (exit 1 would let it through)."""
    phase, agent = sys.argv[1], sys.argv[2]
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError("hook input is not a JSON object")
        code = pre(agent, event) if phase == "pre" else post(agent, event)
    except Exception as e:  # noqa: BLE001
        print(f"Blocked: the copilot's tool policy could not check this call ({e.__class__.__name__})", file=sys.stderr)
        code = 2 if phase == "pre" else 0
    sys.exit(code)


if __name__ == "__main__":
    main()
