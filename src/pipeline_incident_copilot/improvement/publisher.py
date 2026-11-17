"""BranchPublisher: put an accepted change on its own branch for a person to review and merge.

Uses a temporary git worktree, so the working tree in use is never touched, and nothing is merged or pushed.
Only ALLOWED_FILES can be written (checked again here).
"""

import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from pipeline_incident_copilot.config import PROJECT_ROOT, RUNTIME_DIR
from pipeline_incident_copilot.improvement.proposals import ALLOWED_FILES, Proposal

WORKTREES = RUNTIME_DIR / "worktrees"
ATTRIBUTION = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"


def _git(*args: str, cwd: Path = PROJECT_ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class BranchPublisher:
    def __init__(self, repo_root: Path = PROJECT_ROOT, base: str = "HEAD"):
        self.repo_root = repo_root
        self.base = base

    def publish(self, proposal: Proposal, verdict_reason: str) -> str:
        outside = set(proposal.files) - ALLOWED_FILES
        if outside:
            raise ValueError(f"refusing to publish changes to {sorted(outside)}")
        slug = re.sub(r"[^a-z0-9]+", "-", proposal.target.lower()).strip("-")[:40]
        branch = f"improve/{datetime.now(UTC):%Y%m%d-%H%M}-{slug}"
        WORKTREES.mkdir(parents=True, exist_ok=True)
        tree = WORKTREES / branch.replace("/", "-")
        _git("worktree", "add", "-q", "-b", branch, str(tree), self.base, cwd=self.repo_root)
        try:
            for rel, content in proposal.files.items():
                (tree / rel).write_text(content)
                _git("add", rel, cwd=tree)
            message = (
                f"Self-improvement: {proposal.change}\n\n{proposal.rationale}.\nValidated: {verdict_reason}.\n"
                f"Proposed by the nightly loop; review and merge by a person.\n\n{ATTRIBUTION}\n"
            )
            _git("-c", "user.name=Pipeline Incident Copilot", "-c", "user.email=copilot@localhost",
                 "commit", "-q", "-m", message, cwd=tree)  # fmt: skip
        finally:
            _git("worktree", "remove", "--force", str(tree), cwd=self.repo_root)
        return branch
