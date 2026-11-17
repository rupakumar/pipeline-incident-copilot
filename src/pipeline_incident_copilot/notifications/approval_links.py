"""Signed approve / deny links for emails: one incident, one decision, expiring, single use.

token = base64url(payload JSON) + "." + base64url(HMAC-SHA256(secret, payload))
The secret comes from COPILOT_LINK_SECRET, or is generated once into data/runtime/link_secret.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import quote

from pipeline_incident_copilot.config import RUNTIME_DIR

SECRET_FILE = RUNTIME_DIR / "link_secret"
DEFAULT_BASE_URL = "http://localhost:8787"
TTL_SECONDS = 24 * 3600


class InvalidLink(ValueError):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def load_secret() -> bytes:
    if env := os.getenv("COPILOT_LINK_SECRET"):
        return env.encode()
    if not SECRET_FILE.exists():
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRET_FILE.write_text(secrets.token_hex(32))
        SECRET_FILE.chmod(0o600)
    return SECRET_FILE.read_text().strip().encode()


class ApprovalLinks:
    def __init__(self, secret: bytes, base_url: str = DEFAULT_BASE_URL, ttl: int = TTL_SECONDS):
        self.secret = secret
        self.base_url = base_url.rstrip("/")
        self.ttl = ttl

    @classmethod
    def from_env(cls) -> "ApprovalLinks":
        return cls(load_secret(), os.getenv("COPILOT_APPROVAL_BASE_URL", DEFAULT_BASE_URL))

    def _sign(self, payload: bytes) -> str:
        return _b64(hmac.new(self.secret, payload, hashlib.sha256).digest())

    def token(
        self, incident_id: str, decision: str, now: float | None = None, via: str = "email link", who: str = ""
    ) -> str:
        """`via` names where the person clicked (email link, console, Grafana); `who` the approver the email was sent
        to. Both are signed and go in the history."""
        body = {"i": incident_id, "d": decision, "exp": int((now or time.time()) + self.ttl), "n": secrets.token_hex(8)}
        body |= {"v": via} | ({"w": who} if who else {})
        payload = json.dumps(body, separators=(",", ":")).encode()
        return f"{_b64(payload)}.{self._sign(payload)}"

    def url(self, incident_id: str, decision: str, via: str = "email link", who: str = "") -> str:
        return f"{self.base_url}/decide?token={quote(self.token(incident_id, decision, via=via, who=who))}"

    def verify(self, token: str, now: float | None = None) -> dict:
        """{'incident_id', 'decision', 'nonce', 'via', 'who'} or raises InvalidLink (tampered, expired, malformed)."""
        try:
            body, signature = token.split(".", 1)
            payload = _unb64(body)
        except ValueError as e:
            raise InvalidLink("malformed link") from e
        if not hmac.compare_digest(signature, self._sign(payload)):
            raise InvalidLink("this link was not issued by the copilot (signature mismatch)")
        data = json.loads(payload)
        if (now or time.time()) > data["exp"]:
            raise InvalidLink("this link has expired; run `copilot notify <incident>` for a new one")
        if data["d"] not in {"approve", "reject", "close"}:
            raise InvalidLink("unknown decision")
        return {
            "incident_id": data["i"],
            "decision": data["d"],
            "nonce": data["n"],
            "via": data.get("v", "email link"),
            "who": data.get("w", ""),
        }
