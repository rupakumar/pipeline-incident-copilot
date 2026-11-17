"""FailureWatcher: find failed runs on every enabled connection that have no incident yet.

Each connection keeps its own start point (`watch_from:<connection>`), so connecting a platform never
floods the desk with its old failures. One connection failing (bad token, platform down) is recorded on
the connection and the others are still checked.
"""

from collections.abc import Iterator
from datetime import UTC, datetime

from pipeline_incident_copilot.connectors.hub import ConnectorHub
from pipeline_incident_copilot.schemas.incident import FailedRun
from pipeline_incident_copilot.storage.connection_repository import ConnectionRepository
from pipeline_incident_copilot.storage.incident_repository import IncidentRepository


class FailureWatcher:
    def __init__(self, repo: IncidentRepository, connections: ConnectionRepository, hub: ConnectorHub):
        self.repo = repo
        self.connections = connections
        self.hub = hub

    def watch_from(self, connection_id: str, since: datetime | None = None) -> datetime:
        """Runs that started before this are ignored. Set once (first poll = now, or --since), then kept."""
        key = f"watch_from:{connection_id}"
        if since is not None:
            self.repo.set_state(key, since.isoformat())
        stored = self.repo.get_state(key)
        if stored is None:
            stored = datetime.now(UTC).isoformat()
            self.repo.set_state(key, stored)
        return datetime.fromisoformat(stored)

    def poll(self, lookback: int = 50, since: datetime | None = None) -> Iterator[FailedRun]:
        """New failed runs (no incident yet) on all enabled connections, oldest first per connection."""
        for connection in self.connections.enabled():
            cid = connection.connection_id
            try:
                connector = self.hub.get(cid)
                result = connector.poll(self.watch_from(cid, since), lookback)
            except Exception as e:  # noqa: BLE001 — one broken connection must not stop the others
                message = f"{e.__class__.__name__}: {e}"
                self.connections.record_check(cid, False, message)
                if not (connection.last_check_ok is False and connection.last_check_message == message[:500]):
                    print(f"[watch] {cid}: check failed: {message}"[:300] + " (said once; retrying every poll)")
                continue
            if connection.last_check_ok is False:
                print(f"[watch] {cid}: reachable again")
            self.connections.save_health(result.health)
            failing = sum(1 for h in result.health if h.last_state and h.last_state not in ("SUCCESS", "SUCCEEDED"))
            self.connections.record_check(
                cid, True, f"{len(result.health)} pipeline(s) checked, {failing} with a failed last run"
            )
            self.repo.set_state(f"last_poll:{cid}", datetime.now(UTC).isoformat(timespec="seconds"))
            new = [
                r
                for r in result.failed_runs
                if not self.repo.exists_for_run(r) and not self.repo.started_by_copilot(r)  # own fix reruns
            ]
            for run in sorted(new, key=lambda r: r.started_at or datetime.min.replace(tzinfo=UTC)):
                yield connector.complete(run)
