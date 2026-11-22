"""Paths and environment configuration.

Every path is derived from the project root so a test run can redirect all
state with ``MOUSE_DRIVER_DATA``. The driver never reads a home directory
outside these known locations.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


DATA_DIR = Path(os.environ.get("MOUSE_DRIVER_DATA", PROJECT_ROOT / "data")).expanduser()
DB_PATH = DATA_DIR / "telemetry.db"
STATE_PATH = DATA_DIR / "armed.json"
STOP_PATH = DATA_DIR / "STOP"
LOCK_PATH = DATA_DIR / "guard.lock"

API_HOST = os.environ.get("MOUSE_DRIVER_API_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("MOUSE_DRIVER_API_PORT", "8340"))

#: ``1`` records motion instead of emitting X11 events (tests, dry runs).
DRY_RUN = _env_flag("MOUSE_DRIVER_DRY")
#: ``1`` simulates a missing X server (headless handshake tests).
FORCE_NO_X = _env_flag("MOUSE_DRIVER_FORCE_NO_X")
X_DISPLAY_FALLBACK = os.environ.get("MOUSE_DRIVER_X_DISPLAY", ":0")

#: Fallback screen geometry when X is unreachable or in dry-run mode.
DEFAULT_SCREEN = (
    int(os.environ.get("MOUSE_DRIVER_SCREEN_W", "5120")),
    int(os.environ.get("MOUSE_DRIVER_SCREEN_H", "1440")),
    int(os.environ.get("MOUSE_DRIVER_SCREEN_MONITORS", "2")),
)

ARM_DEFAULT_TTL_S = 300
ARM_MAX_TTL_S = 900


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
