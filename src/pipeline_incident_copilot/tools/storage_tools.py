"""Storage checks on Unity Catalog volumes."""

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import DatabricksError, NotFound
from pydantic import BaseModel, Field

from pipeline_incident_copilot.schemas.evidence import ToolResult

MAX_LISTED = 20


class CheckPathExists:
    name = "check_path_exists"
    description = (
        "Check whether a file or folder exists in a Unity Catalog volume (/Volumes/...). "
        "If it does not, report the nearest folder that does exist and what it contains."
    )

    class Args(BaseModel):
        path: str = Field(description="Full /Volumes/<catalog>/<schema>/<volume>/... path")

    def __init__(self, w: WorkspaceClient):
        self.w = w

    def _exists(self, path: str) -> str | None:
        """'file', 'folder' or None."""
        try:
            self.w.files.get_metadata(path)
            return "file"
        except (NotFound, DatabricksError):
            pass
        try:
            self.w.files.get_directory_metadata(path)
            return "folder"
        except (NotFound, DatabricksError):
            return None

    def _listing(self, folder: str) -> list[str]:
        try:
            entries = self.w.files.list_directory_contents(folder)
            return sorted(e.name for e in entries)[:MAX_LISTED]
        except DatabricksError:
            return []

    def run(self, args: "CheckPathExists.Args") -> ToolResult:
        path = args.path.removeprefix("dbfs:").rstrip("/")
        kind = self._exists(path)
        if kind:
            return ToolResult(
                tool=self.name,
                args=args.model_dump(),
                ok=True,
                summary=f"{path} exists ({kind})",
                data={"exists": True, "kind": kind},
            )
        parts = path.split("/")
        nearest, listing = None, []
        for depth in range(len(parts) - 1, 1, -1):  # walk up to /Volumes
            candidate = "/".join(parts[:depth])
            if self._exists(candidate) == "folder":
                nearest, listing = candidate, self._listing(candidate)
                break
        where = f"; nearest existing folder {nearest} contains: {', '.join(listing) or '(empty)'}" if nearest else ""
        return ToolResult(
            tool=self.name,
            args=args.model_dump(),
            ok=True,
            summary=f"{path} does NOT exist{where}",
            data={"exists": False, "nearest_existing_folder": nearest, "nearest_folder_contents": listing},
        )
