import subprocess
from types import SimpleNamespace as NS

from databricks.sdk.errors import NotFound

from pipeline_incident_copilot.integrations.git_client import GitClient
from pipeline_incident_copilot.tools.code_change_tools import GitDiffSinceLastSuccess
from pipeline_incident_copilot.tools.storage_tools import CheckPathExists


class FakeFiles:
    def __init__(self, files: set[str], folders: dict[str, list[str]]):
        self.files, self.folders = files, folders

    def get_metadata(self, path):
        if path not in self.files:
            raise NotFound("no file")

    def get_directory_metadata(self, path):
        if path not in self.folders:
            raise NotFound("no folder")

    def list_directory_contents(self, path):
        return [NS(name=n) for n in self.folders[path]]


def test_missing_file_reports_nearest_folder_and_its_contents():
    root = "/Volumes/workspace/pipeline_demo/raw_logs"
    files = FakeFiles(
        {f"{root}/spark/2017-06-09/Spark_2k.log"},
        {root: ["hdfs", "spark"], f"{root}/spark": ["2017-06-09", "2017-06-10"]},
    )
    tool = CheckPathExists(NS(files=files))
    missing = tool.run(tool.Args(path=f"dbfs:{root}/spark/2017-06-12/Spark_2k.log"))
    assert missing.data == {
        "exists": False,
        "nearest_existing_folder": f"{root}/spark",
        "nearest_folder_contents": ["2017-06-09", "2017-06-10"],
    }
    present = tool.run(tool.Args(path=f"{root}/spark/2017-06-09/Spark_2k.log"))
    assert present.data["exists"] is True and present.data["kind"] == "file"


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_git_diff_shows_pipeline_changes_between_deployed_commits(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t"), _git(tmp_path, "config", "user.name", "t")
    nb = tmp_path / "databricks" / "notebooks"
    nb.mkdir(parents=True)
    (nb / "gold_aggregate.py").write_text("hourly.write.mode('overwrite')\n")
    _git(tmp_path, "add", "."), _git(tmp_path, "commit", "-qm", "good")
    good = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    (nb / "gold_aggregate.py").write_text("rows = hourly.collect()\nhourly.write.mode('overwrite')\n")
    _git(tmp_path, "commit", "-qam", "debug collect")
    bad = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()

    tool = GitDiffSinceLastSuccess(GitClient(tmp_path))
    result = tool.run(tool.Args(from_commit=good, to_commit=bad))
    assert result.ok and result.data["changes"][0]["path"] == "databricks/notebooks/gold_aggregate.py"
    assert "+rows = hourly.collect()" in result.data["changes"][0]["diff"]
    assert tool.run(tool.Args(from_commit=good, to_commit=good)).summary.startswith("no pipeline code")
    assert not tool.run(tool.Args(from_commit="deadbeef", to_commit=bad)).ok
