"""Project settings: .env secrets and filesystem paths, defined once."""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR = PROJECT_ROOT / "data"
EXTERNAL_DATA_DIR = DATA_DIR / "external"  # downloaded inputs, re-creatable by scripts
RUNTIME_DIR = DATA_DIR / "runtime"  # indexes, caches, local databases
EVALS_DIR = PROJECT_ROOT / "evals"
DATABRICKS_DIR = PROJECT_ROOT / "databricks"


def require(name: str) -> str:
    value = os.getenv(name)
    if not value or "<" in value or value.endswith("..."):
        raise RuntimeError(f"{name} is not set — copy .env.example to .env and fill it in")
    return value
