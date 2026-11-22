"""Test bootstrap: dry-run everything and keep state inside a temp directory.

The environment is set before importing ``mouse_driver.config`` so no test can
reach X11 or the real telemetry database.
"""
from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="mouse-driver-tests-")
os.environ["MOUSE_DRIVER_DRY"] = "1"
os.environ["MOUSE_DRIVER_DATA"] = _TMP
os.environ.setdefault("MOUSE_DRIVER_API_PORT", "0")

import pytest  # noqa: E402

from mouse_driver.guard import Guard  # noqa: E402
from mouse_driver.telemetry import Store  # noqa: E402
from mouse_driver.x11 import X11Driver  # noqa: E402


@pytest.fixture()
def guard(tmp_path):
    return Guard(
        state_path=tmp_path / "armed.json",
        stop_path=tmp_path / "STOP",
        lock_path=tmp_path / "guard.lock",
    )


@pytest.fixture()
def store(tmp_path):
    store = Store(tmp_path / "telemetry.db")
    store.start_session()
    yield store
    store.end_session()


@pytest.fixture()
def driver():
    return X11Driver(dry_run=True)
