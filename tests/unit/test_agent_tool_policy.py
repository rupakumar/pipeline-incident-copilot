"""The diagnose-incident subagents are read-only by enforcement (hook + ToolPolicy), not by prompt."""

import json
import subprocess
import sys

import pytest

from pipeline_incident_copilot.guardrails.agent.tool_policy import check_bash, check_path, check_tool

ROOT = "/repo"


@pytest.mark.parametrize(
    ("agent", "command"),
    [
        ("log-analyst", ".venv/bin/copilot evidence INC-0029"),
        ("knowledge-retriever", '.venv/bin/copilot search "DATA_QUALITY_FAILED MIN_ROWS 5000"'),
        ("root-cause-analyst", ".venv/bin/copilot check check_path_exists path=/Volumes/w/p/raw/x.log"),
        ("resolution-planner", ".venv/bin/copilot check-fix INC-0029 --reset MIN_ROWS=1000"),
        ("log-analyst", "/Users/x/proj/.venv/bin/copilot evidence INC-0029"),
    ],
)
def test_each_agent_may_run_its_own_read_commands(agent, command):
    assert check_bash(agent, command).allowed


@pytest.mark.parametrize(
    ("agent", "command", "reason"),
    [
        ("resolution-planner", ".venv/bin/copilot request-approval INC-1 --reset a= --report r.json", "may only run"),
        ("resolution-planner", ".venv/bin/copilot approve INC-1", "may only run"),
        ("log-analyst", ".venv/bin/copilot search x", "may only run"),
        ("log-analyst", ".venv/bin/copilot evidence INC-1; rm -rf data", "shell operators"),
        ("log-analyst", ".venv/bin/copilot evidence INC-1 > /tmp/x", "shell operators"),
        ("log-analyst", ".venv/bin/copilot evidence $(cat .env)", "shell operators"),
        ("log-analyst", "cat .env", "only `.venv/bin/copilot"),
        ("log-analyst", "python -c 'import os'", "only `.venv/bin/copilot"),
        ("someone-else", ".venv/bin/copilot evidence INC-1", "may not run commands"),
    ],
)
def test_everything_else_is_refused(agent, command, reason):
    verdict = check_bash(agent, command)
    assert not verdict.allowed and reason in verdict.reason


def test_only_code_and_knowledge_files_may_be_read():
    assert check_path("databricks/notebooks/bronze_ingest.py", ROOT).allowed
    assert check_path(f"{ROOT}/knowledge/runbooks/missing_input.md", ROOT).allowed
    assert check_path("infra/jenkins/jobs/loghub-daily-export.xml", ROOT).allowed
    for path in (".env", f"{ROOT}/data/runtime/copilot.db", "data/runtime/jenkins/home/copilot/api_token",
                 "knowledge/../.env", "/etc/passwd", ".claude/settings.json", None):  # fmt: skip
        assert not check_path(path, ROOT).allowed, path
    assert not check_tool("log-analyst", "Write", {"file_path": "knowledge/x.md"}, ROOT).allowed
    assert not check_tool("log-analyst", "Edit", {"file_path": "databricks/notebooks/x.py"}, ROOT).allowed
    assert not check_tool("log-analyst", "Grep", {"pattern": "token"}, ROOT).allowed  # no path = whole repo


def test_subagents_may_hand_back_their_report_but_no_other_unknown_tool():
    assert check_tool("log-analyst", "SubagentHandback", {"result": "{}"}, ROOT).allowed
    for tool in ("Write", "Edit", "WebFetch", "Agent", "NotebookEdit", "SomeFutureTool"):
        assert not check_tool("log-analyst", tool, {}, ROOT).allowed, tool


def run_hook(tmp_path, phase, agent, event):
    return subprocess.run(
        [sys.executable, "-m", "pipeline_incident_copilot.guardrails.agent.hook", phase, agent],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env={"COPILOT_AUDIT_DIR": str(tmp_path / "audit"), "PATH": "/usr/bin:/bin"},
    )


def test_hook_blocks_with_exit_2_and_audits_every_call(tmp_path):
    base = {"session_id": "s1", "cwd": ROOT, "tool_name": "Bash"}
    bad = run_hook(tmp_path, "pre", "log-analyst", {**base, "tool_input": {"command": "cat .env"}})
    assert bad.returncode == 2 and "Blocked by the copilot's tool policy" in bad.stderr
    good = {**base, "tool_input": {"command": ".venv/bin/copilot evidence INC-0029"}}
    assert run_hook(tmp_path, "pre", "log-analyst", good).returncode == 0
    run_hook(tmp_path, "post", "log-analyst", {**good, "tool_response": {"stdout": "token=abc123secret"}})
    lines = [json.loads(x) for x in (tmp_path / "audit" / "s1.jsonl").read_text().splitlines()]
    assert [line["blocked"] is not None for line in lines] == [True, False]
    assert lines[1]["incidents"] == ["INC-0029"] and "abc123secret" not in lines[1]["result_head"]
    assert run_hook(tmp_path, "pre", "log-analyst", "not json").returncode == 2  # unreadable input fails closed
