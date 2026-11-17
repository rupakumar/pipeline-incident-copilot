"""The platforms the copilot can connect to, and what the connect form asks for each.

The form in the console is built from these field lists, so a new platform needs no UI code. `build` turns a
saved connection + its secrets into a live connector. Tiers: live (has a connector), paid (the form is ready, the
platform needs a paid account to test against) and coming (listed in the gallery only).
"""

from collections.abc import Callable
from dataclasses import dataclass

from pipeline_incident_copilot.schemas.connection import Connection


@dataclass(frozen=True)
class FormField:
    name: str
    label: str
    kind: str = "text"  # text | url | secret (stored in the Keychain, never shown again)
    required: bool = True
    placeholder: str = ""
    help: str = ""
    section: str = "connect"  # connect | dashboards (what Grafana needs to chart this platform)


@dataclass(frozen=True)
class PlatformSpec:
    platform: str
    label: str
    description: str
    fields: tuple[FormField, ...]
    pipelines_label: str
    pipelines_help: str
    build: Callable[[Connection, dict[str, str]], object] | None = None  # None: not available yet
    coming: str = ""  # when it is not available: why (needs a paid account) or "coming"
    color: str = "#3D4B5C"  # brand colour of the gallery tile
    logo: str = ""  # key in interface/brand_logos.LOGOS; empty = monogram
    category: str = ""  # shown under the name in the gallery

    @property
    def available(self) -> bool:
        return self.build is not None

    @property
    def tier(self) -> str:
        return "live" if self.available else "paid" if self.fields else "coming"

    @property
    def secret_fields(self) -> list[str]:
        return [f.name for f in self.fields if f.kind == "secret"]


def _build_databricks(connection: Connection, secrets: dict[str, str]):
    from pipeline_incident_copilot.connectors.databricks.connector import DatabricksConnector

    return DatabricksConnector.from_connection(connection, secrets)


def _build_jenkins(connection: Connection, secrets: dict[str, str]):
    from pipeline_incident_copilot.connectors.jenkins.connector import JenkinsConnector

    return JenkinsConnector.from_connection(connection, secrets)


PLATFORMS: dict[str, PlatformSpec] = {
    "databricks": PlatformSpec(
        platform="databricks",
        label="Databricks",
        description="Lakeflow Jobs: failed runs, task errors and traces, rerun with parameters.",
        fields=(
            FormField("host", "Workspace URL", "url", placeholder="https://dbc-1234abcd-5678.cloud.databricks.com"),
            FormField("token", "Personal access token", "secret", help="User settings → Developer → Access tokens."),
            FormField(
                "sql_warehouse_http_path",
                "SQL warehouse HTTP path",
                required=False,
                placeholder="/sql/1.0/warehouses/1a2b3c4d5e6f7a8b",
                help="SQL warehouses → your warehouse → Connection details. Grafana queries the run audit tables "
                "through it. Blank: the workspace's first warehouse.",
                section="dashboards",
            ),
            FormField(
                "grafana_token",
                "Read-only token for Grafana",
                "secret",
                required=False,
                help="A separate access token only Grafana uses, so it can be revoked without stopping the copilot.",
                section="dashboards",
            ),
        ),
        pipelines_label="Jobs to watch",
        pipelines_help="Job names, one per line.",
        build=_build_databricks,
        color="#FF3621",
        logo="databricks",
        category="Lakehouse jobs",
    ),
    "jenkins": PlatformSpec(
        platform="jenkins",
        label="Jenkins",
        description="Builds of freestyle or pipeline jobs: console log, parameters, rebuild with parameters.",
        fields=(
            FormField("url", "Jenkins URL", "url", placeholder="http://localhost:8080"),
            FormField("username", "User name", placeholder="admin"),
            FormField("api_token", "API token", "secret", help="Your user → Security → API Token → Add new token."),
        ),
        pipelines_label="Jobs to watch",
        pipelines_help="Job names (folder/job for jobs in folders), one per line.",
        build=_build_jenkins,
        color="#D24939",
        logo="jenkins",
        category="CI/CD builds",
    ),
    "snowflake": PlatformSpec(
        platform="snowflake",
        label="Snowflake",
        description="Tasks: TASK_HISTORY failures, query errors, EXECUTE TASK to rerun.",
        fields=(
            FormField("account", "Account identifier", placeholder="orgname-accountname"),
            FormField("user", "User"),
            FormField("warehouse", "Warehouse"),
            FormField("private_key", "Key-pair private key", "secret"),
        ),
        pipelines_label="Tasks to watch",
        pipelines_help="database.schema.task, one per line.",
        coming="Needs a paid account",
        color="#29B5E8",
        logo="snowflake",
        category="Data warehouse tasks",
    ),
    "aws_glue": PlatformSpec(
        platform="aws_glue",
        label="AWS Glue",
        description="Glue job runs: error message, CloudWatch logs, start_job_run to rerun.",
        fields=(
            FormField("region", "Region", placeholder="eu-central-1"),
            FormField("access_key_id", "Access key ID"),
            FormField("secret_access_key", "Secret access key", "secret"),
        ),
        pipelines_label="Glue jobs to watch",
        pipelines_help="Job names, one per line.",
        coming="Needs a paid account",
        color="#FF9900",
        logo="aws",
        category="Serverless ETL",
    ),
    "azure_data_factory": PlatformSpec(
        platform="azure_data_factory",
        label="Azure Data Factory",
        description="Pipeline runs: failed activity errors, createRun to rerun.",
        fields=(
            FormField("subscription_id", "Subscription ID"),
            FormField("resource_group", "Resource group"),
            FormField("factory", "Data factory name"),
            FormField("tenant_id", "Tenant ID"),
            FormField("client_id", "App (client) ID"),
            FormField("client_secret", "Client secret", "secret"),
        ),
        pipelines_label="Pipelines to watch",
        pipelines_help="Pipeline names, one per line.",
        coming="Needs a paid account",
        color="#0078D4",
        category="Cloud data integration",
    ),
    **{
        key: PlatformSpec(
            platform=key,
            label=label,
            description=desc,
            fields=(),
            pipelines_label="",
            pipelines_help="",
            coming="Coming",
            color=color,
            logo=logo,
            category=category,
        )  # fmt: skip
        for key, label, desc, color, logo, category in (
            (
                "airflow",
                "Apache Airflow",
                "DAG runs: failed task logs, clear and rerun a task.",
                "#017CEE",
                "airflow",
                "Orchestration",
            ),
            (
                "dbt_cloud",
                "dbt Cloud",
                "Job runs: failed model, compiled SQL, rerun from failure.",
                "#FF694B",
                "dbt",
                "Transformations",
            ),
            ("bigquery", "BigQuery", "Scheduled queries and Dataform runs.", "#669DF6", "bigquery", "Data warehouse"),
            (
                "github_actions",
                "GitHub Actions",
                "Workflow runs: failed step log, re-run jobs.",
                "#2088FF",
                "github_actions",
                "CI/CD workflows",
            ),
            ("kafka", "Kafka Connect", "Connector and task failures, restart a task.", "#231F20", "kafka", "Streaming"),
            ("fivetran", "Fivetran", "Connector syncs: sync errors, re-sync.", "#0073FF", "", "Ingestion"),
        )
    },
}


def platform_label(platform: str) -> str:
    spec = PLATFORMS.get(platform)
    return spec.label if spec else platform
