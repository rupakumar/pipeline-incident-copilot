"""Run a Databricks Asset Bundle command with the workspace saved in the console (else .env).

Usage: python databricks/bundle.py validate | deploy | summary | deployment bind loghub_etl <job_id>

`deploy` refuses uncommitted changes under databricks/: every run records the git commit it was
deployed from (code_version), and that is only true if the deployed files are that commit.
Pass --allow-dirty to deploy anyway (the recorded code_version will then be misleading).
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from pipeline_incident_copilot.config import DATABRICKS_DIR, require
from pipeline_incident_copilot.integrations.databricks_client import saved_credentials


def main() -> None:
    if saved := saved_credentials():  # the CLI reads these from its environment
        os.environ["DATABRICKS_HOST"], os.environ["DATABRICKS_TOKEN"] = saved
    require("DATABRICKS_HOST")  # else loading config put .env into the environment the CLI inherits
    require("DATABRICKS_TOKEN")
    args = sys.argv[1:]
    if args[:1] == ["deploy"] and "--allow-dirty" not in args:
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "."], cwd=DATABRICKS_DIR, capture_output=True, text=True
        ).stdout.strip()
        if dirty:
            sys.exit(f"uncommitted changes under databricks/ — commit first (or --allow-dirty):\n{dirty}")
    args = [a for a in args if a != "--allow-dirty"]
    cli = shutil.which("databricks") or str(Path.home() / ".local" / "bin" / "databricks")
    sys.exit(subprocess.call([cli, "bundle", *args], cwd=DATABRICKS_DIR))


if __name__ == "__main__":
    main()
