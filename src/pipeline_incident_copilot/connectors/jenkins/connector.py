"""JenkinsConnector: failed builds of the watched jobs, console-log evidence, rebuild with parameters.

Failed = FAILURE or UNSTABLE (ABORTED is a person's choice, not an incident). The failed step is the
pipeline stage (`[Pipeline] { (Stage)` in the console) or, for freestyle jobs, the build step that failed.
"""

import re
import time
from datetime import UTC, datetime, timedelta

from pipeline_incident_copilot.connectors.base import PollResult, summarize_health
from pipeline_incident_copilot.connectors.jenkins.client import JenkinsClient, JenkinsError
from pipeline_incident_copilot.rag.retrieval.query.log_distiller import distill
from pipeline_incident_copilot.schemas.connection import Connection, ConnectionTest
from pipeline_incident_copilot.schemas.evidence import (
    CodeChange,
    EvidenceBundle,
    RunComparison,
    RunSnapshot,
    ToolResult,
)
from pipeline_incident_copilot.schemas.incident import FailedRun, Incident
from pipeline_incident_copilot.schemas.remediation import RemediationResult

FAILED_RESULTS = {"FAILURE", "UNSTABLE"}
BUILD_FIELDS = "number,result,building,timestamp,duration,url,actions[parameters[name,value],lastBuiltRevision[SHA1]]"
CHANGES = "changeSets[items[commitId,msg,affectedPaths]]"
ERROR_LINE = re.compile(
    r"\b(error|fatal|exception|traceback|no such file|permission denied|failed|exit code|non-zero)\b", re.I
)
STAGE = re.compile(r"^\[Pipeline\] \{ \((.+)\)$")
FREESTYLE_STEP = re.compile(r"Build step '([^']+)' marked build as failure")


def _ts(ms: int | None) -> datetime | None:
    return datetime.fromtimestamp(ms / 1000, tz=UTC) if ms else None


def as_text(value) -> str:
    """Jenkins parameter values as the strings jobs see (booleans as true/false)."""
    if value is None:
        return ""
    return str(value).lower() if isinstance(value, bool) else str(value)


def parameters_of(build: dict) -> dict[str, str]:
    params: dict[str, str] = {}
    for action in build.get("actions") or []:
        for p in (action or {}).get("parameters") or []:
            value = p.get("value")
            params[p["name"]] = as_text(value)
    return params


def commit_of(build: dict) -> str | None:
    for action in build.get("actions") or []:
        if (action or {}).get("lastBuiltRevision"):
            return action["lastBuiltRevision"].get("SHA1")
    return None


def is_failed(build: dict) -> bool:
    return build.get("result") in FAILED_RESULTS


def failed_step(console: str) -> str | None:
    stage = None
    for line in console.splitlines():
        if m := STAGE.match(line.strip()):
            stage = m.group(1)
    if stage:
        return stage
    m = FREESTYLE_STEP.search(console)
    return f"build step '{m.group(1)}'" if m else None


def error_from_console(console: str, max_lines: int = 8) -> tuple[str | None, str]:
    """(the lines that say what went wrong, the last 150 lines as the trace)."""
    lines = [line for line in console.splitlines() if line.strip()]
    hits = [
        line for line in lines if ERROR_LINE.search(line) and not line.startswith(("+ ", "Finished:", "Build step '"))
    ]
    error = "\n".join(hits[-max_lines:]) or None
    return error, "\n".join(lines[-150:])


class JenkinsConnector:
    platform = "jenkins"

    def __init__(self, connection_id: str, client: JenkinsClient, pipelines: list[str]):
        self.connection_id = connection_id
        self.client = client
        self.pipelines = pipelines

    @classmethod
    def from_connection(cls, connection: Connection, secrets: dict[str, str]) -> "JenkinsConnector":
        s = connection.settings
        client = JenkinsClient(s.get("url", ""), s.get("username", ""), secrets.get("api_token", ""))
        return cls(connection.connection_id, client, connection.pipelines)

    # --- FailureSource ---------------------------------------------------------------------------
    def test(self) -> ConnectionTest:
        try:
            user = self.client.whoami()
            names = self.client.job_names()
            missing = []
            for job in self.pipelines:
                try:
                    self.client.json(self.client.job_path(job), tree="name")
                except JenkinsError:
                    missing.append(job)
        except JenkinsError as e:
            return ConnectionTest(ok=False, message=str(e))
        found = len(self.pipelines) - len(missing)
        return ConnectionTest(
            ok=not missing and user != "anonymous",
            message=f"signed in as {user}; {found} of {len(self.pipelines)} job(s) found"
            + (f"; not found: {', '.join(missing)}" if missing else "")
            + ("; the token was not accepted (anonymous)" if user == "anonymous" else ""),
            available=names[:20],
            missing=missing,
        )

    def _builds(self, job: str, lookback: int) -> list[dict]:
        data = self.client.json(self.client.job_path(job), tree=f"builds[{BUILD_FIELDS}]{{0,{lookback}}}")
        return [b for b in data.get("builds") or [] if not b.get("building")]  # newest first

    def to_failed_run(self, job: str, build: dict) -> FailedRun:
        started = _ts(build.get("timestamp"))
        return FailedRun(
            connection_id=self.connection_id,
            platform=self.platform,
            job_id=job,
            job_name=job,
            run_id=build["number"],
            run_page_url=build.get("url"),
            started_at=started,
            ended_at=started + timedelta(milliseconds=build.get("duration") or 0) if started else None,
            result_state=build.get("result") or "FAILURE",
            parameters=parameters_of(build),
        )

    def poll(self, since: datetime, lookback: int) -> PollResult:
        result = PollResult()
        for job in self.pipelines:
            builds = self._builds(job, lookback)
            result.health.append(
                summarize_health(
                    self.connection_id,
                    job,
                    (
                        (str(b["number"]), b.get("result"), _ts(b.get("timestamp")), b.get("url"), is_failed(b))
                        for b in builds
                    ),
                )
            )
            result.failed_runs += [
                self.to_failed_run(job, b)
                for b in builds
                if is_failed(b) and (_ts(b.get("timestamp")) or since) >= since
            ]
        return result

    def complete(self, run: FailedRun) -> FailedRun:
        try:
            step = failed_step(self.client.console(str(run.job_id), int(run.run_id)))
        except JenkinsError:
            step = None
        return run.model_copy(update={"failed_task": step}) if step else run

    # --- EvidenceSource --------------------------------------------------------------------------
    def _snapshot(self, build: dict, status: str, step: str | None, error: str | None) -> RunSnapshot:
        params = parameters_of(build)
        return RunSnapshot(
            job_run_id=str(build["number"]),
            run_date=params.get("RUN_DATE") or None,
            source=params.get("SOURCE") or None,
            status=status,
            failed_task=step,
            error_summary=(error or "").splitlines()[0][:300] if error else None,
            bronze_rows=None,
            silver_rows=None,
            gold_rows=None,
            input_path=params.get("INPUT_FILE") or params.get("INPUT_PATH") or None,
            code_version=commit_of(build),
            parameters=params,
            started_at=_ts(build.get("timestamp")),
            duration_seconds=(build.get("duration") or 0) / 1000 or None,
        )

    def collect_evidence(self, incident: Incident) -> EvidenceBundle:
        job, number = str(incident.run.job_id), int(incident.run.run_id)
        problems: list[str] = []
        path = f"{self.client.job_path(job)}/{number}"
        try:
            console = self.client.console(job, number)
        except JenkinsError as e:
            console = ""
            problems.append(f"console log not read: {e}")
        error, trace = error_from_console(console)
        step = failed_step(console) or incident.run.failed_task
        if console and error is None:
            problems.append("no error line found in the console log; see the trace")

        results: list[ToolResult] = []
        comparison, code_changes = None, []
        try:
            failed = self.client.json(path, tree=f"{BUILD_FIELDS},{CHANGES}")
            try:
                good = self.client.json(f"{self.client.job_path(job)}/lastSuccessfulBuild", tree=BUILD_FIELDS)
            except JenkinsError:
                good = None
            failed_snap = self._snapshot(failed, failed.get("result") or "FAILURE", step, error)
            good_snap = self._snapshot(good, "SUCCESS", None, None) if good else None
            changed = {}
            if good_snap:
                for key in sorted(set(failed_snap.parameters) | set(good_snap.parameters)):
                    a, b = good_snap.parameters.get(key, ""), failed_snap.parameters.get(key, "")
                    if a != b:
                        changed[key] = (a, b)
            code_changes = [
                CodeChange(path=p, status="M", diff=f"{item.get('commitId', '')[:10]} {item.get('msg', '')}".strip())
                for cs in failed.get("changeSets") or []
                for item in cs.get("items") or []
                for p in item.get("affectedPaths") or []
            ]
            code_changed = bool(code_changes) or (
                bool(good_snap and good_snap.code_version and failed_snap.code_version)
                and good_snap.code_version != failed_snap.code_version
            )
            comparison = RunComparison(
                failed=failed_snap, last_success=good_snap, changed_parameters=changed, code_changed=code_changed
            )
            if good_snap:
                changes = "; ".join(f"{k}: {a or '(default)'} → {b or '(default)'}" for k, (a, b) in changed.items())
                summary = (
                    f"last success: build #{good_snap.job_run_id} ({good_snap.started_at:%Y-%m-%d %H:%M} UTC). "
                    f"Changed: {changes or 'no parameters'}; code {'changed' if code_changed else 'unchanged'}"
                )
            else:
                summary = f"no earlier successful build of {job} found"
            results.append(
                ToolResult(
                    tool="compare_with_last_success",
                    args={"job": job, "build": number},
                    ok=True,
                    summary=summary,
                    data={"comparison": comparison.model_dump(mode="json")},
                )
            )
        except JenkinsError as e:
            problems.append(f"build history not read: {e}")
        if not code_changes:
            problems.append("no source changes recorded for this build (job without SCM, or nothing changed)")

        return EvidenceBundle(
            incident_id=incident.incident_id,
            failed_task=step,
            error=error,
            error_trace_chars=len(trace),
            distilled_log=distill(error or "", trace),
            parameters=incident.run.parameters,
            comparison=comparison,
            code_changes=code_changes,
            tool_results=results,
            problems=problems,
        )

    # --- RerunTarget -----------------------------------------------------------------------------
    def job_defaults(self, job_id) -> dict[str, str]:
        data = self.client.json(
            self.client.job_path(str(job_id)), tree="property[parameterDefinitions[name,defaultParameterValue[value]]]"
        )
        defaults: dict[str, str] = {}
        for prop in data.get("property") or []:
            for d in (prop or {}).get("parameterDefinitions") or []:
                value = (d.get("defaultParameterValue") or {}).get("value")
                defaults[d["name"]] = as_text(value)
        return defaults

    def start_rerun(self, incident: Incident, parameters: dict[str, str]) -> RemediationResult:
        job = str(incident.run.job_id)
        build = self.client.wait_for_queue(self.client.build_with_parameters(job, parameters))
        return RemediationResult(
            connection_id=self.connection_id,
            job_id=job,
            rerun_run_id=build["number"],
            run_page_url=build.get("url"),
            parameters=parameters,
            started=True,
        )

    def wait_for_rerun(
        self, result: RemediationResult, timeout: timedelta = timedelta(minutes=20), poll: float = 5
    ) -> RemediationResult:
        job, number = str(result.job_id), int(result.rerun_run_id)
        deadline = time.monotonic() + timeout.total_seconds()
        while True:
            build = self.client.json(f"{self.client.job_path(job)}/{number}", tree="result,building")
            if not build.get("building") and build.get("result"):
                break
            if time.monotonic() > deadline:
                raise TimeoutError(f"build #{number} of {job} still running after {timeout}")
            time.sleep(poll)
        if build["result"] == "SUCCESS":
            return result.model_copy(update={"succeeded": True, "result_state": "SUCCESS"})
        console = self.client.console(job, number)
        error, _ = error_from_console(console)
        return result.model_copy(
            update={
                "succeeded": False,
                "result_state": build["result"],
                "failed_task": failed_step(console),
                "error": (error or "")[:2000] or None,
            }
        )
