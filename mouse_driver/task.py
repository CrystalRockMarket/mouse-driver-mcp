"""Repeat-task model and runner.

The model is stored in ``data/task.json`` (driver is the single writer). A task
is a short list of key/type/wait steps repeated by cycles; starting requires an
armed session and the runner stops on pause, lock, panic, STOP, disarm or an
expired TTL. Hard caps: 10 000 cycles or 8 hours.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

MAX_STEPS = 20
MAX_CYCLES = 10_000
MAX_DURATION_MIN = 480
MAX_RUNTIME_S = 8 * 3600
WAIT_MIN, WAIT_MAX = 0.1, 3600.0
INTERVAL_MAX = 3600.0

DEFAULT_TASK = {
    "name": "demo",
    "steps": [{"kind": "key", "value": "c"}],
    "repeat": {"mode": "count", "count": 1, "minutes": 30},
    "interval_s": 0,
    "enabled": False,
}


class TaskError(ValueError):
    """Invalid task definition."""


def validate_task(data: object) -> dict:
    if not isinstance(data, dict):
        raise TaskError("task must be an object")
    name = str(data.get("name") or "task").strip()[:80] or "task"
    steps_raw = data.get("steps")
    if not isinstance(steps_raw, list) or not (1 <= len(steps_raw) <= MAX_STEPS):
        raise TaskError(f"steps must be a list of 1–{MAX_STEPS} items")
    steps = []
    for raw in steps_raw:
        if not isinstance(raw, dict):
            raise TaskError("each step must be an object")
        kind = str(raw.get("kind") or "").lower()
        if kind in {"key", "type"}:
            value = str(raw.get("value") or "")
            if not value or len(value) > 200:
                raise TaskError(f"{kind} step needs a value of 1–200 characters")
            steps.append({"kind": kind, "value": value})
        elif kind == "wait":
            try:
                seconds = float(raw.get("seconds"))
            except (TypeError, ValueError):
                raise TaskError("wait step needs seconds") from None
            if not (WAIT_MIN <= seconds <= WAIT_MAX):
                raise TaskError(f"wait seconds must be {WAIT_MIN}–{WAIT_MAX}")
            steps.append({"kind": "wait", "seconds": round(seconds, 2)})
        else:
            raise TaskError("step kind must be key, type or wait")

    repeat_raw = data.get("repeat") or {}
    mode = str(repeat_raw.get("mode") or "count").lower()
    if mode not in {"infinite", "count", "duration"}:
        raise TaskError("repeat.mode must be infinite, count or duration")
    count = repeat_raw.get("count", 1)
    minutes = repeat_raw.get("minutes", 30)
    try:
        count = int(count)
        minutes = int(minutes)
    except (TypeError, ValueError):
        raise TaskError("repeat count/minutes must be integers") from None
    if not (1 <= count <= MAX_CYCLES):
        raise TaskError(f"repeat.count must be 1–{MAX_CYCLES}")
    if not (1 <= minutes <= MAX_DURATION_MIN):
        raise TaskError(f"repeat.minutes must be 1–{MAX_DURATION_MIN}")

    try:
        interval = float(data.get("interval_s") or 0)
    except (TypeError, ValueError):
        raise TaskError("interval_s must be a number") from None
    if not (0 <= interval <= INTERVAL_MAX):
        raise TaskError(f"interval_s must be 0–{INTERVAL_MAX}")

    return {
        "name": name,
        "steps": steps,
        "repeat": {"mode": mode, "count": count, "minutes": minutes},
        "interval_s": round(interval, 2),
        "enabled": bool(data.get("enabled", False)),
    }


def load_task(path: Path) -> dict | None:
    try:
        return validate_task(json.loads(Path(path).read_text(encoding="utf-8")))
    except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError):
        return None


def save_task(path: Path, model: dict) -> dict:
    model = validate_task(model)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(model, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return model


class TaskRunner(threading.Thread):
    """Run a validated task against a service that exposes guard/key/type."""

    def __init__(self, service, model: dict, *, clock=time.monotonic, sleeper=time.sleep) -> None:
        super().__init__(name="mouse-task", daemon=True)
        self.service = service
        self.model = model
        self.clock = clock
        self.sleeper = sleeper
        self.stop_event = threading.Event()
        self._stop_reason = "stopped by operator"
        self.lock = threading.Lock()
        self.status = {
            "name": model["name"],
            "running": True,
            "cycle": 0,
            "cycles_done": 0,
            "next_at": None,
            "last_result": "starting",
            "error": None,
        }

    # -- public ----------------------------------------------------------
    def stop(self, reason: str = "stopped") -> None:
        self._stop_reason = reason
        self.stop_event.set()

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.status)

    def _set(self, **fields) -> None:
        with self.lock:
            self.status.update(fields)

    # -- gating ----------------------------------------------------------
    def _gate(self) -> str:
        """run/pause/stop from the guard, re-read continuously."""
        try:
            return self.service.guard.action_state()
        except Exception:
            return "stop"

    def _wait_while_paused(self) -> bool:
        while True:
            state = self._gate()
            if state == "stop":
                return False
            if state == "run":
                return True
            self.sleeper(0.1)

    def _sleep_checked(self, seconds: float) -> bool:
        """Sleep in 100 ms slices; pause freezes the remaining time."""
        remaining = float(seconds)
        while remaining > 0:
            if not self._wait_while_paused():
                return False
            if self.stop_event.is_set():
                return False
            slice_s = min(0.1, remaining)
            self.sleeper(slice_s)
            remaining -= slice_s
        return True

    # -- run -------------------------------------------------------------
    def run(self) -> None:
        started = self.clock()
        repeat = self.model["repeat"]
        mode = repeat["mode"]
        limit_cycles = MAX_CYCLES if mode in {"infinite", "duration"} else min(MAX_CYCLES, repeat["count"])
        deadline = started + MAX_RUNTIME_S
        if mode == "duration":
            deadline = min(deadline, started + repeat["minutes"] * 60)
        reason = "finished"
        try:
            while True:
                if self.stop_event.is_set():
                    reason = self._stop_reason
                    break
                if self.status["cycles_done"] >= limit_cycles:
                    reason = "cycle cap reached" if mode == "infinite" else "finished"
                    break
                if self.clock() >= deadline:
                    reason = "duration cap reached"
                    break
                if not self._wait_while_paused():
                    reason = "stopped by guard"
                    break
                self._set(cycle=self.status["cycles_done"] + 1)
                for step in self.model["steps"]:
                    if not self._wait_while_paused():
                        raise _TaskStopped("stopped by guard")
                    if step["kind"] == "key":
                        self.service.key(step["value"])
                    elif step["kind"] == "type":
                        self.service.type_text(step["value"])
                    elif not self._sleep_checked(step["seconds"]):
                        raise _TaskStopped("stopped by guard")
                self._set(cycles_done=self.status["cycles_done"] + 1, last_result="cycle complete")
                if self.clock() >= deadline:
                    reason = "duration cap reached"
                    break
                interval = self.model.get("interval_s") or 0
                if interval:
                    self._set(next_at=self.clock() + interval)
                    if not self._sleep_checked(interval):
                        raise _TaskStopped("stopped by guard")
                    self._set(next_at=None)
        except _TaskStopped as exc:
            reason = str(exc)
        except Exception as exc:  # never crash the driver
            reason = f"error: {exc}"
        finally:
            with self.lock:
                self.status["running"] = False
                self.status["last_result"] = reason
                self.status["error"] = None if reason in {"finished", "cycle cap reached", "duration cap reached"} else reason
                self.status["next_at"] = None


class _TaskStopped(Exception):
    pass
