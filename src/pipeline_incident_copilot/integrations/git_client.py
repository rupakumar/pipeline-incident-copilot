"""Read-only git access to this repository: what changed between two deployed commits."""

import subprocess
from pathlib import Path

from pipeline_incident_copilot.config import PROJECT_ROOT

MAX_DIFF_CHARS = 4000  # per file; enough to show the change, small enough for an LLM prompt


class GitClient:
    def __init__(self, repo: Path = PROJECT_ROOT):
        self.repo = repo

    def _git(self, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")
        return result.stdout

    def has_commit(self, commit: str) -> bool:
        try:
            self._git("cat-file", "-e", f"{commit}^{{commit}}")
            return True
        except RuntimeError:
            return False

    def changed_files(self, from_commit: str, to_commit: str, paths: list[str]) -> list[tuple[str, str]]:
        """[(status, path)] for files that differ between the two commits under `paths`."""
        out = self._git("diff", "--name-status", f"{from_commit}..{to_commit}", "--", *paths)
        return [(line.split("\t")[0][0], line.split("\t")[-1]) for line in out.splitlines() if line.strip()]

    def diff(self, from_commit: str, to_commit: str, path: str) -> str:
        text = self._git("diff", "--unified=3", f"{from_commit}..{to_commit}", "--", path)
        return text if len(text) <= MAX_DIFF_CHARS else text[:MAX_DIFF_CHARS] + "\n… (diff truncated)"

    def log(self, from_commit: str, to_commit: str, paths: list[str]) -> list[str]:
        """One line per commit between the two, newest first."""
        out = self._git("log", "--oneline", f"{from_commit}..{to_commit}", "--", *paths)
        return [line for line in out.splitlines() if line.strip()]
