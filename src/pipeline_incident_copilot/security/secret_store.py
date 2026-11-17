"""SecretStore: connection secrets (API tokens, passwords) by connection and field name.

KeychainSecretStore keeps them in the macOS Keychain (encrypted by the OS, unlocked with the login); the
production equivalent is Vault / AWS Secrets Manager / Azure Key Vault behind the same three methods.
"""

from typing import Protocol

SERVICE = "pipeline-incident-copilot"


class SecretStore(Protocol):
    def get(self, connection_id: str, field: str) -> str | None: ...

    def set(self, connection_id: str, field: str, value: str) -> None: ...

    def delete(self, connection_id: str, field: str) -> None: ...


class KeychainSecretStore:
    def __init__(self, service: str = SERVICE):
        import keyring  # macOS backend: the login Keychain

        self._keyring = keyring
        self.service = service

    @staticmethod
    def _account(connection_id: str, field: str) -> str:
        return f"{connection_id}/{field}"

    def get(self, connection_id: str, field: str) -> str | None:
        return self._keyring.get_password(self.service, self._account(connection_id, field))

    def set(self, connection_id: str, field: str, value: str) -> None:
        self._keyring.set_password(self.service, self._account(connection_id, field), value)

    def delete(self, connection_id: str, field: str) -> None:
        from keyring.errors import PasswordDeleteError

        try:
            self._keyring.delete_password(self.service, self._account(connection_id, field))
        except PasswordDeleteError:
            pass  # already gone


class MemorySecretStore:
    """For tests and evals: secrets in a dict, gone when the process ends."""

    def __init__(self):
        self._data: dict[tuple[str, str], str] = {}

    def get(self, connection_id: str, field: str) -> str | None:
        return self._data.get((connection_id, field))

    def set(self, connection_id: str, field: str, value: str) -> None:
        self._data[(connection_id, field)] = value

    def delete(self, connection_id: str, field: str) -> None:
        self._data.pop((connection_id, field), None)
