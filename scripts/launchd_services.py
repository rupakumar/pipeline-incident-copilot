"""Install, remove or inspect the copilot's launchd jobs (macOS user agents; no sudo).

  python -m scripts.launchd_services install [watcher console nightly]   # default: all three
  python -m scripts.launchd_services uninstall [names…]
  python -m scripts.launchd_services status
  python -m scripts.launchd_services run-nightly-now                     # kickstart the nightly job once

Templates: infra/launchd/*.plist (__PROJECT__ is filled with this checkout's path), installed to
~/Library/LaunchAgents/. Logs: data/runtime/logs/ (rotated at each install, 5 MB × 3; launchd keeps them open).
The watcher and the console restart if they stop; the nightly job runs at 02:30 and only on AC power with enough
free memory. Stop everything with `uninstall`.
"""

import argparse
import os
import plistlib
import subprocess
import time
from pathlib import Path

from pipeline_incident_copilot.automation.guards import LOG_DIR, rotate_log
from pipeline_incident_copilot.config import PROJECT_ROOT

TEMPLATES = PROJECT_ROOT / "infra" / "launchd"
AGENTS = Path.home() / "Library" / "LaunchAgents"
SERVICES = ("watcher", "console", "nightly")
DOMAIN = f"gui/{os.getuid()}"


def label(name: str) -> str:
    return f"com.pipeline-copilot.{name}"


def render(name: str) -> bytes:
    text = (TEMPLATES / f"{label(name)}.plist").read_text().replace("__PROJECT__", str(PROJECT_ROOT))
    plistlib.loads(text.encode())  # refuse to install a malformed file
    return text.encode()


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def bootout(name: str, timeout: float = 15) -> None:
    """Stop a loaded copy and wait until launchd has really removed it (bootout returns before it is gone, and a
    bootstrap during teardown fails with "5: Input/output error")."""
    launchctl("bootout", f"{DOMAIN}/{label(name)}")
    deadline = time.monotonic() + timeout
    while launchctl("print", f"{DOMAIN}/{label(name)}").returncode == 0 and time.monotonic() < deadline:
        time.sleep(0.5)


def install(names: list[str]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    AGENTS.mkdir(parents=True, exist_ok=True)
    for name in names:
        bootout(name)  # replace a running copy
        rotate_log(LOG_DIR / f"{name}.log")
        target = AGENTS / f"{label(name)}.plist"
        target.write_bytes(render(name))
        r = launchctl("bootstrap", DOMAIN, str(target))
        if r.returncode != 0:  # one retry: teardown can lag even after print stops finding the job
            time.sleep(2)
            r = launchctl("bootstrap", DOMAIN, str(target))
        print(f"{name:<8} {'installed' if r.returncode == 0 else 'FAILED: ' + r.stderr.strip()}  ({target})")


def uninstall(names: list[str]) -> None:
    for name in names:
        bootout(name)
        (AGENTS / f"{label(name)}.plist").unlink(missing_ok=True)
        print(f"{name:<8} removed")


def status() -> None:
    for name in SERVICES:
        r = launchctl("print", f"{DOMAIN}/{label(name)}")
        if r.returncode != 0:
            print(f"{name:<8} not installed")
            continue
        state = next((line.split("=", 1)[1].strip() for line in r.stdout.splitlines() if "state =" in line), "?")
        last = next((line.split("=", 1)[1].strip() for line in r.stdout.splitlines() if "last exit code" in line), "-")
        print(f"{name:<8} {state:<12} last exit: {last}   log: data/runtime/logs/{name}.log")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["install", "uninstall", "status", "run-nightly-now"])
    parser.add_argument("names", nargs="*", help=f"any of {', '.join(SERVICES)} (default: all)")
    args = parser.parse_args()
    names = args.names or list(SERVICES)
    if unknown := set(names) - set(SERVICES):
        parser.error(f"unknown service(s) {sorted(unknown)}; choose from {', '.join(SERVICES)}")
    if args.action == "install":
        install(names)
    elif args.action == "uninstall":
        uninstall(names)
    elif args.action == "status":
        status()
    else:
        r = launchctl("kickstart", f"{DOMAIN}/{label('nightly')}")
        print("nightly started (see data/runtime/logs/nightly.log)" if r.returncode == 0 else r.stderr.strip())


if __name__ == "__main__":
    main()
