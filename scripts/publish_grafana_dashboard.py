"""Publish the pipeline status dashboard to Grafana Cloud.

Creates or updates (1) the Databricks data source, pointed at the SQL warehouse with the Grafana read-only
token from the Databricks connection form (Keychain; else DATABRICKS_GRAFANA_TOKEN from .env), (2) the folder,
and (3) the dashboard from
infra/grafana/cloud/dashboards/pipeline_health.json. Idempotent: rerun after editing the JSON.

Usage: python -m scripts.publish_grafana_dashboard [--check]   (--check: test the data source only)
"""

import json
import sys
import urllib.error
import urllib.request

from pipeline_incident_copilot.config import PROJECT_ROOT, require
from pipeline_incident_copilot.integrations.databricks_client import get_client
from pipeline_incident_copilot.schemas.connection import DEFAULT_CONNECTION_ID
from pipeline_incident_copilot.security.app_settings import app_setting
from pipeline_incident_copilot.security.secret_store import KeychainSecretStore

DASHBOARD_FILE = PROJECT_ROOT / "infra" / "grafana" / "cloud" / "dashboards" / "pipeline_health.json"
DATASOURCE_UID = "databricks-pipeline"
DATASOURCE_TYPE = "grafana-databricks-datasource"
FOLDER_UID = "pipeline-incident-copilot"
FOLDER_TITLE = "Pipeline Incident Copilot"


def setting(name: str) -> str:
    """Grafana Cloud settings from the console (Settings page), else .env."""
    return app_setting(name) or require(name)


def grafana_token() -> str:
    """The separate read-only PAT Grafana queries Databricks with (revocable on its own)."""
    return KeychainSecretStore().get(DEFAULT_CONNECTION_ID, "grafana_token") or require("DATABRICKS_GRAFANA_TOKEN")


class Grafana:
    """Minimal Grafana HTTP API client (service-account token auth)."""

    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")


def sql_warehouse() -> tuple[str, str]:
    """(hostname, http path): the warehouse named in the Databricks connection form, else the first one."""
    from urllib.parse import urlparse

    from pipeline_incident_copilot.storage.connection_repository import ConnectionRepository
    from pipeline_incident_copilot.storage.database import connect

    c = ConnectionRepository(connect()).get(DEFAULT_CONNECTION_ID)
    if c and (path := c.settings.get("sql_warehouse_http_path")):
        return urlparse(get_client().config.host).netloc, path
    warehouse = next(iter(get_client().warehouses.list()), None)
    if warehouse is None or warehouse.odbc_params is None:
        sys.exit("no SQL warehouse found in the Databricks workspace")
    return warehouse.odbc_params.hostname, warehouse.odbc_params.path


def upsert_datasource(g: Grafana) -> None:
    host, http_path = sql_warehouse()
    body = {
        "uid": DATASOURCE_UID,
        "name": "Databricks · pipeline_demo",
        "type": DATASOURCE_TYPE,
        "access": "proxy",
        "jsonData": {"host": host, "httpPath": http_path, "authType": "Pat"},
        "secureJsonData": {"token": grafana_token()},
    }
    status, existing = g.request("GET", f"/api/datasources/uid/{DATASOURCE_UID}")
    if status == 200:
        status, resp = g.request("PUT", f"/api/datasources/uid/{DATASOURCE_UID}", {**body, "id": existing["id"]})
    else:
        status, resp = g.request("POST", "/api/datasources", body)
    if status not in (200, 201):
        sys.exit(f"data source not saved: {status} {resp.get('message')}")
    print(f"data source '{body['name']}' saved ({host}{http_path})")


def check_datasource(g: Grafana) -> None:
    """Run one real query against pipeline_runs through Grafana."""
    query = {
        "refId": "A",
        "datasource": {"type": DATASOURCE_TYPE, "uid": DATASOURCE_UID},
        "rawSql": "SELECT count(*) AS runs FROM workspace.pipeline_demo.pipeline_runs",
        "format": 1,
    }
    status, resp = g.request("POST", "/api/ds/query", {"queries": [query], "from": "now-30d", "to": "now"})
    result = resp.get("results", {}).get("A", {})
    if status != 200 or result.get("error"):
        sys.exit(f"test query failed: {status} {result.get('error') or resp.get('message')}")
    values = result["frames"][0]["data"]["values"]
    print(f"test query ok: pipeline_runs has {values[0][0]} rows")


def upsert_dashboard(g: Grafana) -> str:
    status, _ = g.request("GET", f"/api/folders/{FOLDER_UID}")
    if status != 200:
        g.request("POST", "/api/folders", {"uid": FOLDER_UID, "title": FOLDER_TITLE})
    dashboard = json.loads(DASHBOARD_FILE.read_text())
    dashboard.pop("id", None)
    status, resp = g.request(
        "POST",
        "/api/dashboards/db",
        {"dashboard": dashboard, "folderUid": FOLDER_UID, "overwrite": True, "message": "published by script"},
    )
    if status != 200:
        sys.exit(f"dashboard not saved: {status} {resp.get('message')}")
    return g.url + resp["url"]


def main() -> None:
    g = Grafana(setting("GRAFANA_URL"), setting("GRAFANA_SERVICE_ACCOUNT_TOKEN"))
    upsert_datasource(g)
    check_datasource(g)
    if "--check" in sys.argv:
        return
    print(f"dashboard published: {upsert_dashboard(g)}")


if __name__ == "__main__":
    main()
