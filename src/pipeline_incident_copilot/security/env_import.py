"""Move what is still in .env into the console's stores (`copilot settings import-env`), printing names only.

- email + Grafana settings → AppSettings (secrets in the Keychain); a value already saved in the console wins
- the original Databricks connection (credentials "env") → host in its settings, token (and the Grafana read-only
  token) in the Keychain, after a connection test with exactly those values; the test failing changes nothing
Afterwards the app no longer needs those .env lines; deleting them is left to a person.
"""

import os

from pipeline_incident_copilot.security.app_settings import FIELDS, AppSettings


def import_env(settings: AppSettings, connections, secrets, test_databricks) -> list[str]:
    """Returns one line per thing moved or skipped (no values). `test_databricks(host, token) -> ConnectionTest`."""
    lines = []
    for f in FIELDS:
        if settings.source(f.name) == "console":
            lines.append(f"{f.name}: already saved in the console (kept)")
        elif os.getenv(f.name):
            settings.save({f.name: os.environ[f.name]})
            lines.append(f"{f.name}: moved to the console{' (Keychain)' if f.kind == 'secret' else ''}")
        else:
            lines.append(f"{f.name}: not in .env (skipped)")
    for c in connections.all():
        if c.platform != "databricks" or c.settings.get("credentials") != "env" or c.settings.get("host"):
            continue
        host, token = os.getenv("DATABRICKS_HOST"), os.getenv("DATABRICKS_TOKEN")
        if not (host and token):
            lines.append(f"{c.connection_id}: DATABRICKS_HOST / DATABRICKS_TOKEN not in .env (skipped)")
            continue
        result = test_databricks(host.rstrip("/"), token)
        if not result.ok:
            lines.append(f"{c.connection_id}: connection test failed, nothing changed ({result.message[:120]})")
            continue
        secrets.set(c.connection_id, "token", token)
        fields = ["token"]
        if grafana := os.getenv("DATABRICKS_GRAFANA_TOKEN"):
            secrets.set(c.connection_id, "grafana_token", grafana)
            fields.append("grafana_token")
        settings_ = {k: v for k, v in c.settings.items() if k != "credentials"} | {"host": host.rstrip("/")}
        connections.save(c.model_copy(update={"settings": settings_, "secret_fields": fields}))
        connections.record_check(c.connection_id, True, result.message)
        lines.append(
            f"{c.connection_id}: workspace URL saved, {' + '.join(fields)} moved to the Keychain (test passed)"
        )
    return lines
