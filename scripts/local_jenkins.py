"""Run a local Jenkins in Docker with two LogHub demo jobs (http://localhost:8080).

  python -m scripts.local_jenkins start   # create or start the container, wait until Jenkins answers
  python -m scripts.local_jenkins stop    # stop it (frees ~1 GB); the Jenkins home and builds are kept
  python -m scripts.local_jenkins status

Everything is set up as code: infra/jenkins/init.groovy.d (login, API token, jobs) and infra/jenkins/jobs/*.xml.
The Jenkins home is the git-ignored data/runtime/jenkins/home; the LogHub sample files are mounted read-only.
The admin password and the API token are written to files in that home and never printed: open
copilot/admin_password to log in, copy copilot/api_token into the copilot's connect form.
Needs Docker Desktop (open -a Docker).
"""

import argparse
import re
import secrets
import subprocess
import sys
import time

import requests

from pipeline_incident_copilot.config import EXTERNAL_DATA_DIR, PROJECT_ROOT, RUNTIME_DIR

NAME = "copilot-jenkins"
IMAGE = "jenkins/jenkins:lts-jdk21"
URL = "http://localhost:8080"
HOME = RUNTIME_DIR / "jenkins" / "home"
INFRA = PROJECT_ROOT / "infra" / "jenkins"
MIN_FREE_PERCENT = 45  # Jenkins + the Docker VM take ~1–1.5 GB


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def free_memory_percent() -> int:
    out = subprocess.run(["memory_pressure"], capture_output=True, text=True).stdout
    m = re.search(r"free percentage: (\d+)%", out)
    return int(m.group(1)) if m else 100


def container_state() -> str | None:
    r = docker("inspect", "-f", "{{.State.Status}}", NAME, check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def wait_until_up(timeout: int = 240) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if requests.get(f"{URL}/login", timeout=5).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(3)
    sys.exit(f"Jenkins did not answer within {timeout} s: docker logs {NAME}")


def start() -> None:
    if docker("info", check=False).returncode != 0:
        sys.exit("Docker is not running: open -a Docker, wait for it, then try again")
    if (free := free_memory_percent()) < MIN_FREE_PERCENT:
        sys.exit(f"only {free}% memory free (need {MIN_FREE_PERCENT}%): close something first")
    (HOME / "copilot").mkdir(parents=True, exist_ok=True)
    password = HOME / "copilot" / "admin_password"
    if not password.exists():
        password.write_text(secrets.token_urlsafe(18))
        password.chmod(0o600)
    state = container_state()
    if state == "running":
        print(f"{NAME} already running")
    elif state:
        docker("start", NAME)
    else:
        print(f"creating {NAME} from {IMAGE} (first start downloads the image, ~500 MB)")
        docker(
            "run",
            "-d",
            "--name",
            NAME,
            "-p",
            "127.0.0.1:8080:8080",  # this Mac only
            "--memory",
            "1g",
            "-e",
            "JAVA_OPTS=-Djenkins.install.runSetupWizard=false -Xmx512m",
            "-v",
            f"{HOME}:/var/jenkins_home",
            "-v",
            f"{INFRA / 'init.groovy.d'}:/var/jenkins_home/init.groovy.d:ro",
            "-v",
            f"{INFRA / 'jobs'}:/copilot/jobs:ro",
            "-v",
            f"{EXTERNAL_DATA_DIR / 'loghub'}:/data/loghub:ro",
            IMAGE,
        )
    wait_until_up()
    print(f"Jenkins is up: {URL}  (user admin, password in {password.relative_to(PROJECT_ROOT)})")
    print(f"API token for the connect form: {(HOME / 'copilot' / 'api_token').relative_to(PROJECT_ROOT)}")
    print("connect it: copilot serve → http://localhost:8787/connections/new?platform=jenkins")


def stop() -> None:
    if container_state() == "running":
        docker("stop", NAME)
        print(f"{NAME} stopped (home kept in {HOME.relative_to(PROJECT_ROOT)})")
    else:
        print(f"{NAME} is not running")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["start", "stop", "status"])
    args = parser.parse_args()
    if args.action == "start":
        start()
    elif args.action == "stop":
        stop()
    else:
        print(f"{NAME}: {container_state() or 'not created'}")


if __name__ == "__main__":
    main()
