"""Arming gate and emergency stop.

The armed state is a small JSON file guarded by ``flock`` so the MCP server,
the CLI and any short-lived helper share one truth. Motion loops re-check the
file and the ``data/STOP`` sentinel on every sampled point, which makes disarm
and stop effective within about 8 ms even across processes.

Safety rules enforced here:

* no pointer or key action while disarmed;
* ``arm`` TTL is 1–900 s and expires on its own (background watcher);
* ``stop`` writes the sentinel *before* disarming, so in-flight motion aborts;
* ``arm`` clears a stale sentinel — it is the only way to clear one;
* coordinates are clamped to the virtual screen, never wrapped.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from .config import ARM_DEFAULT_TTL_S, ARM_MAX_TTL_S, LOCK_PATH, STATE_PATH, STOP_PATH, ensure_dirs


LOCKED_HINT = "operator lock: press Ctrl+Alt+Shift+Enter to allow arming"
PAUSED_HINT = "paused: press Ctrl+Alt+Shift+P to resume"


class GuardError(RuntimeError):
    """A refused or invalid action. ``code`` is a stable machine identifier."""

    def __init__(self, message: str, code: str = "refused") -> None:
        super().__init__(message)
        self.code = code

    def as_dict(self) -> dict:
        return {"ok": False, "error": str(self), "code": self.code}


def _iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


class Guard:
    """File-backed arming gate shared by every process on this desktop."""

    def __init__(
        self,
        state_path: Path = STATE_PATH,
        stop_path: Path = STOP_PATH,
        lock_path: Path = LOCK_PATH,
    ) -> None:
        self.state_path = Path(state_path)
        self.stop_path = Path(stop_path)
        self.lock_path = Path(lock_path)
        ensure_dirs()
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._halt = threading.Event()
        self._disarm_hooks: list[Callable[[str], None]] = []
        self._last_disarm: str | None = None

    # -- file primitives -------------------------------------------------
    def _read_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _write_state(self, data: dict) -> None:
        ensure_dirs()
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _locked(self, mutate: Callable[[dict], dict]) -> dict:
        ensure_dirs()
        with open(self.lock_path, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                state = mutate(self._read_state())
                self._write_state(state)
                return state
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # -- sentinel --------------------------------------------------------
    def sentinel_present(self) -> bool:
        return self.stop_path.exists()

    def clear_sentinel(self) -> None:
        try:
            self.stop_path.unlink()
        except FileNotFoundError:
            pass

    # -- queries ---------------------------------------------------------
    def state(self) -> dict:
        state = self._read_state()
        expires = float(state.get("expires") or 0.0)
        locked = bool(state.get("locked"))
        armed = bool(state.get("armed")) and expires > time.time() and not self.sentinel_present() and not locked
        return {
            "armed": armed,
            "expires": expires if armed else 0.0,
            "arm_expires": _iso(expires) if armed else None,
            "ttl_remaining_s": round(max(0.0, expires - time.time()), 1) if armed else 0.0,
            "ttl_s": state.get("ttl_s"),
            "armed_at": _iso(state.get("armed_at")),
            "armed_by": state.get("by"),
            "paused": bool(state.get("paused")),
            "paused_at": _iso(state.get("paused_at")),
            "locked": locked,
            "lock_reason": state.get("lock_reason") or "",
            "locked_at": _iso(state.get("locked_at")),
            "stop_sentinel": self.sentinel_present(),
            "halted": self._halt.is_set(),
            "last_disarm_reason": state.get("reason") or self._last_disarm,
        }

    def is_armed(self) -> bool:
        """Armed and unexpired (a paused session still counts as armed)."""
        # ``_halt`` is a local fast path only; the shared file is the truth, so
        # another process arming must clear a stale halt from this one.
        if self.sentinel_present():
            return False
        state = self._read_state()
        if not state.get("armed") or state.get("locked"):
            return False
        return float(state.get("expires") or 0.0) > time.time()

    def is_locked(self) -> bool:
        return bool(self._read_state().get("locked"))

    def is_paused(self) -> bool:
        return bool(self._read_state().get("paused")) and self.is_armed()

    def action_state(self) -> str:
        """``run``, ``pause`` or ``stop`` for a motion loop, re-read every point."""
        if self.sentinel_present():
            return "stop"
        state = self._read_state()
        if state.get("locked") or not state.get("armed"):
            return "stop"
        if float(state.get("expires") or 0.0) <= time.time():
            return "stop"
        return "pause" if state.get("paused") else "run"

    def require_armed(self, action: str) -> None:
        """Raise :class:`GuardError` unless the session may act right now."""
        state = self._read_state()
        if state.get("locked"):
            raise GuardError(f"{action} refused: {LOCKED_HINT}", code="locked")
        if self.sentinel_present():
            raise GuardError(
                f"{action} refused: STOP is active. Re-arm with mouse_arm to clear the sentinel.",
                code="stopped",
            )
        if not state.get("armed"):
            raise GuardError(f"{action} refused: mouse driver is not armed. Call mouse_arm first.", code="not_armed")
        if float(state.get("expires") or 0.0) <= time.time():
            raise GuardError(
                f"{action} refused: the arming window expired. Call mouse_arm again.", code="arm_expired"
            )
        if state.get("paused"):
            raise GuardError(f"{action} refused: {PAUSED_HINT}", code="paused")

    # -- transitions -----------------------------------------------------
    def arm(self, ttl_s: float = ARM_DEFAULT_TTL_S, *, by: str = "agent", dry: bool = False) -> dict:
        if self.is_locked():
            raise GuardError(f"arm refused: {LOCKED_HINT}", code="locked")
        ttl = float(ttl_s)
        if not (0 < ttl <= ARM_MAX_TTL_S):
            raise GuardError(
                f"arm refused: ttl_s must be between 0 and {ARM_MAX_TTL_S} seconds (got {ttl:g}).",
                code="bad_ttl",
            )
        if not dry:
            self.clear_sentinel()
        self._halt.clear()
        now = time.time()
        state = self._locked(
            lambda old: {
                **old,
                "armed": True,
                "expires": now + ttl,
                "armed_at": now,
                "ttl_s": ttl,
                "by": by,
                "reason": "armed",
                "paused": False,
                "locked": False,
                "lock_reason": "",
            }
        )
        self._start_watcher()
        return {
            "ok": True,
            "armed": True,
            "ttl_s": ttl,
            "arm_expires": _iso(state["expires"]),
            **self.state(),
        }

    def disarm(self, reason: str = "manual") -> dict:
        now = time.time()
        state = self._locked(
            lambda old: {
                **old,
                "armed": False,
                "expires": 0.0,
                "disarmed_at": now,
                "reason": reason,
                "paused": False,
            }
        )
        self._last_disarm = reason
        for hook in list(self._disarm_hooks):
            try:
                hook(reason)
            except Exception:  # hooks must never break safety
                pass
        return {"ok": True, "armed": False, "reason": reason, "arm_expires": None, "last_disarm_reason": reason}

    def stop(self, reason: str = "stop") -> dict:
        """Write the sentinel first, then disarm. In-flight motion aborts now."""
        ensure_dirs()
        self._halt.set()
        self.stop_path.write_text(f"{_iso(time.time())} {reason}\n", encoding="utf-8")
        result = self.disarm(reason)
        result["stop_sentinel"] = True
        result["note"] = "motion halted; call mouse_arm to clear the sentinel"
        return result

    # -- pause / lock ----------------------------------------------------
    def pause(self, *, by: str = "operator") -> dict:
        if self.is_locked():
            raise GuardError(f"pause refused: {LOCKED_HINT}", code="locked")
        if not self.is_armed():
            raise GuardError("pause refused: the driver is not armed", code="not_armed")
        now = time.time()
        self._locked(lambda old: {**old, "paused": True, "paused_at": now, "paused_by": by})
        return {"ok": True, "paused": True, "armed": True, **self.state()}

    def resume(self) -> dict:
        state = self._read_state()
        if state.get("locked"):
            raise GuardError(f"resume refused: {LOCKED_HINT}", code="locked")
        if not state.get("paused"):
            return {"ok": True, "paused": False, "armed": self.is_armed(), "note": "not paused"}
        if float(state.get("expires") or 0.0) <= time.time():
            self._locked(lambda old: {**old, "paused": False, "armed": False, "expires": 0.0, "reason": "arm expired while paused"})
            raise GuardError("resume refused: arm expired", code="arm_expired")
        if not self.is_armed():
            self._locked(lambda old: {**old, "paused": False})
            raise GuardError("resume refused: the driver is not armed", code="not_armed")
        self._locked(lambda old: {**old, "paused": False, "resumed_at": time.time()})
        return {"ok": True, "paused": False, "armed": True}

    def lock(self, reason: str = "operator lock") -> dict:
        now = time.time()
        self._halt.set()
        self._locked(
            lambda old: {
                **old,
                "locked": True,
                "lock_reason": reason,
                "locked_at": now,
                "armed": False,
                "expires": 0.0,
                "reason": reason,
            }
        )
        return {"ok": True, "locked": True, "armed": False, "lock_reason": reason}

    def unlock(self) -> dict:
        self._locked(lambda old: {**old, "locked": False, "lock_reason": "", "unlocked_at": time.time()})
        return {"ok": True, "locked": False, "armed": False}

    def panic(self, reason: str = "panic") -> dict:
        """Hard stop: halt motion, release intent, disarm and lock out arming."""
        self._halt.set()
        self.stop_path.parent.mkdir(parents=True, exist_ok=True)
        self.stop_path.write_text(f"{_iso(time.time())} panic {reason}\n", encoding="utf-8")
        now = time.time()
        self._locked(
            lambda old: {
                **old,
                "locked": True,
                "lock_reason": reason,
                "locked_at": now,
                "armed": False,
                "expires": 0.0,
                "reason": reason,
            }
        )
        self._last_disarm = reason
        return {
            "ok": True,
            "panic": True,
            "locked": True,
            "armed": False,
            "paused": bool(self._read_state().get("paused")),
            "stop_sentinel": True,
            "lock_reason": reason,
        }

    def add_disarm_hook(self, hook: Callable[[str], None]) -> None:
        self._disarm_hooks.append(hook)

    # -- watcher ---------------------------------------------------------
    def _start_watcher(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._watch, name="mouse-guard-ttl", daemon=True)
        self._thread.start()

    def _watch(self) -> None:
        while True:
            state = self._read_state()
            expires = float(state.get("expires") or 0.0)
            if state.get("armed") and expires and expires <= time.time():
                self.disarm("ttl expired")
            self._wake.wait(1.0)
            self._wake.clear()

    # -- screen bounds ---------------------------------------------------
    @staticmethod
    def check_point(x: float, y: float, screen: tuple[int, int]) -> tuple[int, int]:
        width, height = screen
        if not (-0.5 <= float(x) < width + 0.5 and -0.5 <= float(y) < height + 0.5):
            raise GuardError(
                f"point ({x:g},{y:g}) is outside the virtual screen {width}x{height}",
                code="out_of_bounds",
            )
        return int(round(min(width - 1, max(0, float(x))))), int(round(min(height - 1, max(0, float(y)))))

    @staticmethod
    def clamp_point(x: float, y: float, screen: tuple[int, int]) -> tuple[int, int]:
        width, height = screen
        return (
            int(round(min(width - 1, max(0, float(x))))),
            int(round(min(height - 1, max(0, float(y))))),
        )


def install_signal_handlers(guard: Guard, *, exit_code: int = 0) -> None:
    """SIGTERM/SIGINT disarm, halt motion and exit cleanly."""

    def handler(signum, _frame):
        guard.stop(reason=f"signal {signal.Signals(signum).name}")
        raise SystemExit(exit_code)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
