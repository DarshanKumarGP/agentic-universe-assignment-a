"""Shared constants and default locations."""
from __future__ import annotations

from datetime import timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

# solution/ledger/config.py -> repo root is two levels above the package directory.
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMAS_DIR = REPO_ROOT / "assignment-a-data" / "schemas"
DEFAULT_DB_PATH = "ledger.db"

SECRET_ENV = "LEDGER_SECRET"  # overrides the key file (use a secret manager in production)
