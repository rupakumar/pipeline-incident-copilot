"""Unity Catalog checks: table schemas and the job identity's privileges."""

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import DatabricksError, NotFound
from pydantic import BaseModel, Field

from pipeline_incident_copilot.schemas.evidence import ToolResult


class DescribeTable:
    name = "describe_table"
    description = "Show a table's columns and types, owner and last update, or report that it does not exist."

    class Args(BaseModel):
        table: str = Field(description="catalog.schema.table")

    def __init__(self, w: WorkspaceClient):
        self.w = w

    def run(self, args: "DescribeTable.Args") -> ToolResult:
        try:
            t = self.w.tables.get(args.table)
        except NotFound:
            return ToolResult(
                tool=self.name,
                args=args.model_dump(),
                ok=True,
                summary=f"{args.table} does not exist",
                data={"exists": False},
            )
        except DatabricksError as e:
            return ToolResult(tool=self.name, args=args.model_dump(), ok=False, summary=f"cannot describe: {e}")
        columns = [{"name": c.name, "type": c.type_text} for c in t.columns or []]
        summary = f"{args.table}: " + ", ".join(f"{c['name']} {c['type']}" for c in columns)
        return ToolResult(
            tool=self.name,
            args=args.model_dump(),
            ok=True,
            summary=summary,
            data={"exists": True, "columns": columns, "owner": t.owner, "updated_at": t.updated_at},
        )


class CheckPermissions:
    name = "check_permissions"
    description = (
        "List the privileges the job identity effectively has on a catalog, schema or table, and whether a "
        "needed privilege (e.g. CREATE_TABLE, MODIFY, SELECT) is among them. For a table that does not exist "
        "yet, checks its schema."
    )

    class Args(BaseModel):
        target: str = Field(description="catalog, catalog.schema or catalog.schema.table")
        needed: str = Field(default="CREATE_TABLE", description="Privilege the failed step needed")

    def __init__(self, w: WorkspaceClient):
        self.w = w

    def _effective(self, securable_type: str, full_name: str, principal: str) -> set[str]:
        result = self.w.grants.get_effective(securable_type, full_name, principal=principal)
        privileges = set()
        for assignment in result.privilege_assignments or []:
            for p in assignment.privileges or []:
                privileges.add(p.privilege.value if hasattr(p.privilege, "value") else str(p.privilege))
        return privileges

    def run(self, args: "CheckPermissions.Args") -> ToolResult:
        principal = self.w.current_user.me().user_name
        parts = args.target.split(".")
        checks = [("table", args.target)] if len(parts) == 3 else []
        checks += [("schema", ".".join(parts[:2]))] if len(parts) >= 2 else []
        checks += [("catalog", parts[0])]
        for securable_type, full_name in checks:
            try:
                privileges = self._effective(securable_type, full_name, principal)
            except NotFound:
                continue  # e.g. the table does not exist yet: check its schema
            except DatabricksError as e:
                return ToolResult(
                    tool=self.name,
                    args=args.model_dump(),
                    ok=True,
                    summary=f"cannot read privileges on {securable_type} {full_name}: {e}",
                    data={"securable": full_name, "readable": False, "has_needed": False},
                )
            has = args.needed in privileges or "ALL_PRIVILEGES" in privileges
            listed = ", ".join(sorted(privileges)) or "none"
            return ToolResult(
                tool=self.name,
                args=args.model_dump(),
                ok=True,
                summary=f"{principal} on {securable_type} {full_name}: {listed} → {args.needed} "
                + ("granted" if has else "MISSING"),
                data={
                    "securable": full_name,
                    "securable_type": securable_type,
                    "privileges": sorted(privileges),
                    "has_needed": has,
                },
            )
        return ToolResult(tool=self.name, args=args.model_dump(), ok=False, summary=f"{args.target} not found")
