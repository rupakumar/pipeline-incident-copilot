"""AppSettings: settings people enter in the console (email delivery, Grafana) instead of editing .env.

Each setting is named after the environment variable it replaces. Lookup order: the console form (non-secret
values in SQLite `app_settings`, secrets in the SecretStore / macOS Keychain) → .env. So an existing .env keeps
working until someone saves the form, and the console shows where each value currently comes from.
Values are read on every use, so a change in the console reaches the watcher without a restart.
"""

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from pipeline_incident_copilot.security.secret_store import SecretStore

SECRETS_OWNER = "_settings"  # the SecretStore "connection" that holds app-level secrets


@dataclass(frozen=True)
class SettingField:
    name: str  # = the environment variable it replaces
    label: str
    section: str  # email | dashboards
    kind: str = "text"  # text | email | emails | url | secret
    placeholder: str = ""
    help: str = ""


FIELDS: tuple[SettingField, ...] = (
    SettingField(
        "GMAIL_SMTP_USER",
        "Sender Gmail address",
        "email",
        "email",
        "robot@gmail.com",
        "The account that sends every copilot email.",
    ),
    SettingField(
        "GMAIL_APP_PASSWORD",
        "Gmail app password",
        "email",
        "secret",
        "16 characters",
        "Google Account → Security → 2-Step Verification → App passwords. Not your login password.",
    ),
    SettingField(
        "NOTIFY_EMAIL_TO",
        "Default recipients",
        "email",
        "emails",
        "oncall@company.com",
        "Used for any connection without its own approvers or notify list.",
    ),
    SettingField(
        "GRAFANA_URL",
        "Grafana Cloud URL",
        "dashboards",
        "url",
        "https://yourstack.grafana.net",
        "Where the pipeline-health dashboard is published.",
    ),
    SettingField(
        "GRAFANA_SERVICE_ACCOUNT_TOKEN",
        "Grafana service-account token",
        "dashboards",
        "secret",
        "glsa_…",
        "Administration → Service accounts. Admin role: it creates the Databricks data source.",
    ),
)
BY_NAME = {f.name: f for f in FIELDS}


def split_emails(text: str | None) -> list[str]:
    seen: list[str] = []
    for part in (text or "").replace(";", ",").replace("\n", ",").split(","):
        if (e := part.strip()) and e not in seen:
            seen.append(e)
    return seen


class AppSettings:
    def __init__(self, conn: sqlite3.Connection, secrets: SecretStore):
        self.conn = conn
        self.secrets = secrets

    def _saved(self, name: str) -> str | None:
        if BY_NAME[name].kind == "secret":
            return self.secrets.get(SECRETS_OWNER, name) or None
        row = self.conn.execute("SELECT value FROM app_settings WHERE name = ?", (name,)).fetchone()
        return row[0] if row and row[0] else None

    def get(self, name: str) -> str | None:
        return self._saved(name) or os.getenv(name) or None

    def source(self, name: str) -> str | None:
        """'console', '.env' or None (not set anywhere)."""
        if self._saved(name):
            return "console"
        return ".env" if os.getenv(name) else None

    def save(self, values: dict[str, str]) -> None:
        """Save the given fields. An empty secret keeps the stored one; an empty text field clears it."""
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self.conn:
            for name, value in values.items():
                field = BY_NAME[name]
                value = value.strip()
                if field.kind == "secret":
                    if value:
                        self.secrets.set(SECRETS_OWNER, name, value.replace(" ", "") if "PASSWORD" in name else value)
                    continue
                if field.kind == "emails":
                    value = ", ".join(split_emails(value))
                self.conn.execute(
                    "INSERT INTO app_settings (name, value, updated_at) VALUES (?, ?, ?)"
                    " ON CONFLICT (name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (name, value, now),
                )

    def forget_secret(self, name: str) -> None:
        self.secrets.delete(SECRETS_OWNER, name)


def app_setting(name: str) -> str | None:
    """For scripts outside the App: the setting from the console store, else .env."""
    from pipeline_incident_copilot.security.secret_store import KeychainSecretStore
    from pipeline_incident_copilot.storage.database import connect

    return AppSettings(connect(), KeychainSecretStore()).get(name)
