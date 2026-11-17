"""Code and job-definition changes between two deployed versions (git)."""

from pydantic import BaseModel, Field

from pipeline_incident_copilot.integrations.git_client import GitClient
from pipeline_incident_copilot.schemas.evidence import CodeChange, ToolResult

PIPELINE_PATHS = ["databricks/notebooks", "databricks/resources", "databricks/databricks.yml"]


class GitDiffSinceLastSuccess:
    name = "git_diff_since_last_success"
    description = (
        "Show pipeline code and job-definition changes (notebooks, job YAML) between the commit the last "
        "successful run was deployed from and the commit of the failed run."
    )

    class Args(BaseModel):
        from_commit: str = Field(description="code_version of the last successful run")
        to_commit: str = Field(description="code_version of the failed run")
        paths: list[str] = Field(default_factory=lambda: list(PIPELINE_PATHS))

    def __init__(self, git: GitClient):
        self.git = git

    def changes(self, args: "GitDiffSinceLastSuccess.Args") -> list[CodeChange]:
        return [
            CodeChange(path=path, status=status, diff=self.git.diff(args.from_commit, args.to_commit, path))
            for status, path in self.git.changed_files(args.from_commit, args.to_commit, args.paths)
        ]

    def run(self, args: "GitDiffSinceLastSuccess.Args") -> ToolResult:
        missing = [c for c in (args.from_commit, args.to_commit) if not self.git.has_commit(c)]
        if missing:
            return ToolResult(
                tool=self.name,
                args=args.model_dump(),
                ok=False,
                summary=f"commit(s) not in this repository: {', '.join(missing)}",
            )
        changes = self.changes(args)
        commits = self.git.log(args.from_commit, args.to_commit, args.paths)
        span = f"{args.from_commit[:7]}..{args.to_commit[:7]}"
        summary = (
            f"no pipeline code or job-definition changes in {span}"
            if not changes
            else f"{len(changes)} file(s) changed in {span}: " + ", ".join(f"{c.status} {c.path}" for c in changes)
        )
        return ToolResult(
            tool=self.name,
            args=args.model_dump(),
            ok=True,
            summary=summary,
            data={"changes": [c.model_dump() for c in changes], "commits": commits},
        )
