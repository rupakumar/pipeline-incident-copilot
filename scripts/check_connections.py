"""Check that every external service in .env is reachable and authenticated.

Usage: python -m scripts.check_connections
"""

from pipeline_incident_copilot.integrations.databricks_client import get_client
from pipeline_incident_copilot.observability.langfuse_tracer import get_langfuse, tracing_enabled


def check_databricks() -> None:
    w = get_client()
    print(f"Databricks: authenticated as {w.current_user.me().user_name}")
    jobs = list(w.jobs.list(limit=10))
    print(f"  jobs visible: {[j.settings.name for j in jobs if j.settings]}")


def check_langfuse() -> None:
    if not tracing_enabled():
        print("Langfuse: keys not set in .env (tracing disabled)")
        return
    print(f"Langfuse: {'authenticated' if get_langfuse().auth_check() else 'REJECTED — check keys and region'}")


if __name__ == "__main__":
    check_databricks()
    check_langfuse()
