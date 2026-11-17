"""JenkinsClient: the few Jenkins REST calls the copilot needs. Basic auth with a user API token, so no
CSRF crumb is needed for POSTs. Errors become JenkinsError with a message a person can act on."""

import time
from urllib.parse import quote, urlparse

import requests


class JenkinsError(RuntimeError):
    pass


class JenkinsClient:
    def __init__(self, url: str, username: str, api_token: str, timeout: float = 20):
        if urlparse(url).scheme not in ("http", "https"):
            raise JenkinsError(f"Jenkins URL must start with http:// or https:// (got {url!r})")
        self.base = url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = (username, api_token)

    @staticmethod
    def job_path(name: str) -> str:
        """`folder/job` → `job/folder/job/job` (each part URL-quoted)."""
        return "/".join(f"job/{quote(part, safe='')}" for part in name.strip("/").split("/"))

    def _request(self, method: str, path_or_url: str, **kwargs) -> requests.Response:
        url = path_or_url if path_or_url.startswith("http") else f"{self.base}/{path_or_url.lstrip('/')}"
        try:
            r = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.RequestException as e:
            raise JenkinsError(f"cannot reach Jenkins at {self.base}: {e.__class__.__name__}") from e
        if r.status_code in (401, 403):
            raise JenkinsError(f"Jenkins rejected the user name or API token (HTTP {r.status_code})")
        if r.status_code == 404:
            raise JenkinsError(f"not found on Jenkins: /{urlparse(url).path.lstrip('/')}")
        if r.status_code >= 400:
            raise JenkinsError(f"Jenkins answered HTTP {r.status_code} for {urlparse(url).path}")
        return r

    def json(self, path: str, tree: str | None = None) -> dict:
        return self._request("GET", f"{path.rstrip('/')}/api/json", params={"tree": tree} if tree else None).json()

    def whoami(self) -> str:
        return self.json("me", tree="id").get("id", "?")

    def job_names(self) -> list[str]:
        return [j["name"] for j in self.json("", tree="jobs[name]").get("jobs", [])]

    def console(self, job: str, number: int, max_chars: int = 400_000) -> str:
        text = self._request("GET", f"{self.job_path(job)}/{number}/consoleText").text
        return text[-max_chars:]

    def build_with_parameters(self, job: str, parameters: dict[str, str]) -> str:
        """Queue a build; returns the queue item URL."""
        r = self._request("POST", f"{self.job_path(job)}/buildWithParameters", data=parameters)
        location = r.headers.get("Location")
        if not location:
            raise JenkinsError("Jenkins did not return a queue item for the build")
        return location

    def wait_for_queue(self, queue_url: str, timeout: float = 120, poll: float = 2) -> dict:
        """The queue item's build {number, url} once Jenkins starts it."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            item = self.json(queue_url, tree="cancelled,why,executable[number,url]")
            if item.get("cancelled"):
                raise JenkinsError("the queued build was cancelled")
            if item.get("executable"):
                return item["executable"]
            time.sleep(poll)
        raise JenkinsError(f"the build did not start within {timeout:.0f} s ({item.get('why') or 'still queued'})")
