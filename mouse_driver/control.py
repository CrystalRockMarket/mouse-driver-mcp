"""Service layer: guard + driver + telemetry wired together.

Both the MCP server and the CLI call :class:`MouseService`. Every pointer or key
action goes through :meth:`MouseService._require_armed`, every move is recorded,
and every motion loop aborts within one sample when the guard closes.
"""
from __future__ import annotations

import atexit
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import motion, task as task_model
from .config import ARM_DEFAULT_TTL_S, DATA_DIR, ensure_dirs
from .guard import Guard, GuardError
from .telemetry import Store, TelemetryAPI
from .x11 import X11Driver, X11Unavailable

MAX_SCROLL_TICKS = 40
MAX_DRAG_SEGMENT_S = 2.5
TARGET_PATH = DATA_DIR / "target.json"
KEYBOARD_PATH = DATA_DIR / "keyboard.json"
HUD_PATH = DATA_DIR / "hud.json"
TASK_PATH = DATA_DIR / "task.json"
KEYS_WINDOW_S = 2.0
KEYS_MAX = 24

SPECIAL_LABELS = {
    "\n": "Enter",
    "\t": "Tab",
    "\b": "Backspace",
    " ": "Space",
    "\x1b": "Esc",
    "return": "Enter",
    "escape": "Esc",
    "esc": "Esc",
    "space": "Space",
    "tab": "Tab",
    "backspace": "Backspace",
    "ctrl": "Ctrl",
    "control": "Ctrl",
    "shift": "Shift",
    "alt": "Alt",
    "super": "Super",
    "enter": "Enter",
}


HUD_SCALE_MIN, HUD_SCALE_MAX = 0.85, 2.0
HUD_SCALE_DEFAULT = 1.35

HUD_DEFAULTS = {
    "visible": True,
    "mode": "auto",
    "offset_x": 12,
    "offset_y": 12,
    "x": 0,
    "y": 0,
    "scale": HUD_SCALE_DEFAULT,
}
_HUD_CACHE: dict = {"mtime": -1.0, "state": dict(HUD_DEFAULTS)}


def read_hud_state() -> dict:
    """Read data/hud.json, re-reading when its mtime changes (autonomy backstop)."""
    try:
        mtime = HUD_PATH.stat().st_mtime
    except OSError:
        return dict(HUD_DEFAULTS)
    if mtime != _HUD_CACHE["mtime"]:
        state = dict(HUD_DEFAULTS)
        try:
            raw = json.loads(HUD_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state.update(
                    {
                        k: raw[k]
                        for k in ("visible", "mode", "offset_x", "offset_y", "x", "y", "scale")
                        if k in raw
                    }
                )
        except (ValueError, OSError, json.JSONDecodeError):
            pass
        state["visible"] = bool(state["visible"])
        state["mode"] = "custom" if state["mode"] == "custom" else "auto"
        state["x"], state["y"] = int(state["x"] or 0), int(state["y"] or 0)
        try:
            state["scale"] = min(HUD_SCALE_MAX, max(HUD_SCALE_MIN, float(state.get("scale") or HUD_SCALE_DEFAULT)))
        except (TypeError, ValueError):
            state["scale"] = HUD_SCALE_DEFAULT
        _HUD_CACHE.update({"mtime": mtime, "state": state})
    return dict(_HUD_CACHE["state"])


def write_hud_state(**patch) -> dict:
    state = read_hud_state()
    state.update(patch)
    state["visible"] = bool(state["visible"])
    state["mode"] = "custom" if state.get("mode") == "custom" else "auto"
    state["offset_x"] = int(state.get("offset_x") or 12)
    state["offset_y"] = int(state.get("offset_y") or 12)
    state["x"], state["y"] = int(state.get("x") or 0), int(state.get("y") or 0)
    try:
        state["scale"] = min(HUD_SCALE_MAX, max(HUD_SCALE_MIN, float(state.get("scale") or HUD_SCALE_DEFAULT)))
    except (TypeError, ValueError):
        state["scale"] = HUD_SCALE_DEFAULT
    try:
        HUD_PATH.parent.mkdir(parents=True, exist_ok=True)
        HUD_PATH.write_text(json.dumps(state) + "\n", encoding="utf-8")
        _HUD_CACHE.update({"mtime": HUD_PATH.stat().st_mtime, "state": dict(state)})
    except OSError:
        pass
    return {"ok": True, "hud": state, "hud_visible": state["visible"]}


def read_hud_visible() -> bool:
    return read_hud_state()["visible"]


def write_hud_visible(visible: bool) -> dict:
    return write_hud_state(visible=visible)


def key_label(raw: str) -> str:
    if raw in SPECIAL_LABELS:
        return SPECIAL_LABELS[raw]
    lowered = raw.lower()
    if lowered in SPECIAL_LABELS:
        return SPECIAL_LABELS[lowered]
    if len(raw) == 1:
        return raw.upper()
    return raw[:1].upper() + raw[1:] if raw else raw


class MouseService:
    def __init__(self, *, dry_run: bool | None = None, start_api: bool = True) -> None:
        ensure_dirs()
        self.guard = Guard()
        self.driver_error: str | None = None
        self.driver: X11Driver | None = None
        try:
            self.driver = X11Driver(dry_run=dry_run)
        except X11Unavailable as exc:
            self.driver_error = str(exc) or "XOpenDisplay failed"
            print(f"mouse-driver: driver unavailable: {self.driver_error}", file=sys.stderr, flush=True)
        self.store = Store()
        self.store.ensure_session()
        self.started_at = time.time()
        self._target_id = self._load_target()
        self._keys: list[dict] = []
        self.task_runner = None
        self._keys_lock = threading.Lock()
        self.api = TelemetryAPI(
            self.store,
            guard=self.guard,
            driver=self.driver,
            started_at=self.started_at,
            target_provider=self.target_payload,
            keyboard_provider=self.keyboard_payload,
            control=self,
        )
        self.api_error: str | None = None
        if start_api:
            self.api_error = self.api.start().get("error")
        self.guard.add_disarm_hook(lambda reason: self.store.add_event("disarm", reason))
        self.guard.add_disarm_hook(self._task_disarm_hook)
        atexit.register(self.close)

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        try:
            self.store.end_session()
        finally:
            self.api.stop_server()

    def _driver(self) -> "X11Driver":
        if self.driver is None:
            raise GuardError(
                f"{self.driver_error or 'XOpenDisplay failed'}; start the server inside the desktop session",
                code="driver_unavailable",
            )
        return self.driver

    def status(self) -> dict:
        state = self.guard.state()
        if self.driver is None:
            return {
                "ok": False,
                "driver": "unavailable",
                "error": self.driver_error or "XOpenDisplay failed",
                "armed": False,
                "locked": state["locked"],
                "paused": state["paused"],
            }
        cursor = self._driver().pointer()
        geometry = self._driver().geometry()
        return {
            "ok": True,
            "armed": state["armed"],
            "arm_expires": state["arm_expires"],
            "ttl_remaining_s": state["ttl_remaining_s"],
            "stop_sentinel": state["stop_sentinel"],
            "backend": geometry["backend"],
            "screen": geometry,
            "cursor": {"x": cursor[0], "y": cursor[1]},
            "session": self.store.session(),
            "target": self.target_payload(),
            "locked": state["locked"],
            "lock_reason": state["lock_reason"],
            "paused": state["paused"],
            "api": self.api_error or f"http://127.0.0.1:{self.api.port}",
        }

    def arm(self, ttl_s: float = ARM_DEFAULT_TTL_S) -> dict:
        result = self.guard.arm(ttl_s, by="agent")
        self.store.add_event("arm", f"ttl={ttl_s:g}s")
        return {"ok": True, **{k: result[k] for k in ("armed", "ttl_s", "arm_expires", "ttl_remaining_s")}}

    def disarm(self) -> dict:
        result = self.guard.disarm("agent")
        self.store.add_event("disarm", "agent")
        return {"ok": True, "armed": False, "reason": result["reason"]}

    def stop(self) -> dict:
        result = self.guard.stop("agent stop")
        self.store.add_event("stop", "agent stop")
        return {"ok": True, "armed": False, "stop_sentinel": True, "reason": result["reason"]}

    def pause(self) -> dict:
        result = self.guard.pause(by="operator")
        cursor = self._driver().pointer()
        self.store.add_event("pause", f"{cursor[0]},{cursor[1]}")
        return {"ok": True, "paused": True, "armed": True, "cursor": {"x": cursor[0], "y": cursor[1]}}

    def resume(self) -> dict:
        result = self.guard.resume()
        self.store.add_event("resume", "operator")
        return {"ok": True, "paused": False, **result}

    def lock(self) -> dict:
        result = self.guard.lock("operator lock")
        self.store.add_event("lock", "operator")
        return {**result, "note": "mouse_arm refuses until Ctrl+Alt+Shift+Enter"}

    def unlock(self) -> dict:
        result = self.guard.unlock()
        self.store.add_event("unlock", "operator")
        return {**result, "note": "unlocked; arm again to drive"}

    def panic(self) -> dict:
        cursor = self._driver().pointer()
        window = self._driver().window_info(self._driver().active_window()) or {}
        result = self.guard.panic("operator panic key")
        released = self._driver().release_buttons()
        self.store.add_event(
            "panic",
            f"{cursor[0]},{cursor[1]} {(window.get('title') or 'unknown')[:80]}",
        )
        return {
            **result,
            "cursor": {"x": cursor[0], "y": cursor[1]},
            "window": window.get("title") or "",
            "buttons_released": released,
        }

    def where(self) -> dict:
        cursor = self._driver().pointer()
        return {
            "ok": True,
            "cursor": {"x": cursor[0], "y": cursor[1]},
            "screen": self._driver().geometry(),
            "armed": self.guard.is_armed(),
        }

    # -- repeat task ------------------------------------------------------
    def _task_disarm_hook(self, reason: str) -> None:
        if self.task_runner is not None:
            self.task_runner.stop(f"disarmed ({reason})")

    def task_model(self) -> dict | None:
        return task_model.load_task(TASK_PATH)

    def task_set(self, data: dict) -> dict:
        model = task_model.save_task(TASK_PATH, data)
        if self.task_runner is not None and not self.task_runner.is_alive():
            self.task_runner = None
        self.store.add_event("task", f"set {model['name']} {len(model['steps'])} steps")
        return {"ok": True, "task": model}

    def task_start(self) -> dict:
        self._driver()
        self.guard.require_armed("mouse_task_start")
        model = self.task_model()
        if model is None:
            raise ValueError("no valid task: call mouse_task_set first")
        if self.task_runner is not None and self.task_runner.is_alive():
            self.task_runner.stop("restarted")
            self.task_runner.join(timeout=2)
        runner = task_model.TaskRunner(self, model)
        self.task_runner = runner
        runner.start()
        self.store.add_event("task", f"start {model['name']}")
        return {"ok": True, "task": runner.snapshot()}

    def task_stop(self) -> dict:
        if self.task_runner is None:
            return {"ok": True, "task": self.task_status(), "note": "not running"}
        self.task_runner.stop("stopped by operator")
        self.task_runner.join(timeout=2)
        snapshot = self.task_runner.snapshot()
        self.store.add_event("task", "stop")
        return {"ok": True, "task": snapshot}

    def task_status(self) -> dict:
        # A finished runner is history: the file is the current model, so a new
        # task saved after a run shows its own name and steps immediately.
        if self.task_runner is not None and self.task_runner.is_alive():
            snapshot = self.task_runner.snapshot()
            model = self.task_model() or {}
            snapshot.setdefault("steps", len(model.get("steps") or []))
            snapshot.setdefault("repeat", model.get("repeat"))
            return snapshot
        if self.task_runner is not None:
            self.task_runner = None
        model = self.task_model()
        return {
            "name": (model or {}).get("name") or "idle",
            "running": False,
            "cycle": 0,
            "cycles_done": 0,
            "next_at": None,
            "last_result": "idle",
            "error": None,
            "steps": len((model or {}).get("steps") or []),
            "repeat": (model or {}).get("repeat"),
        }

    # -- keyboard telemetry ----------------------------------------------
    def _load_keyboard(self) -> bool:
        try:
            return bool(json.loads(KEYBOARD_PATH.read_text(encoding="utf-8")).get("expanded"))
        except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError):
            return False

    def keyboard_expanded(self) -> bool:
        return self._load_keyboard()

    def toggle_keyboard(self) -> dict:
        expanded = not self._load_keyboard()
        try:
            KEYBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
            KEYBOARD_PATH.write_text(json.dumps({"expanded": expanded}) + "\n", encoding="utf-8")
        except OSError:
            pass
        self.store.add_event("keyboard", "expanded" if expanded else "collapsed")
        return {"ok": True, "keyboard_expanded": expanded}

    def record_keys(self, labels: list[str], *, source: str = "driver") -> None:
        now = time.time()
        with self._keys_lock:
            for label in labels[:KEYS_MAX]:
                self._keys.append({"key": label, "code": 0, "t": round(now, 3), "source": source})
            self._keys = self._keys[-KEYS_MAX:]

    def keys_recent(self) -> list[dict]:
        cutoff = time.time() - KEYS_WINDOW_S
        with self._keys_lock:
            self._keys = [event for event in self._keys if event["t"] >= cutoff]
            return list(self._keys)

    def hud_state(self) -> dict:
        return read_hud_state()

    def hud_visible(self) -> bool:
        return self.hud_state()["visible"]

    def set_hud_visible(self, visible: bool) -> dict:
        result = write_hud_state(visible=bool(visible))
        self.store.add_event("hud", "visible" if visible else "hidden")
        return result

    def toggle_hud_visible(self) -> dict:
        return self.set_hud_visible(not self.hud_visible())

    def set_hud_position(self, x: int, y: int, mode: str = "custom") -> dict:
        result = write_hud_state(x=int(x), y=int(y), mode="custom" if mode == "custom" else "auto")
        self.store.add_event("hud", f"position {result['hud']['mode']} {int(x)},{int(y)}")
        return result

    def set_hud_offset(
        self,
        offset_x: int,
        offset_y: int,
        mode: str = "custom",
        x: int | None = None,
        y: int | None = None,
    ) -> dict:
        patch = {
            "offset_x": int(offset_x),
            "offset_y": int(offset_y),
            "mode": "custom" if mode == "custom" else "auto",
        }
        if x is not None and y is not None:
            patch["x"], patch["y"] = int(x), int(y)
        result = write_hud_state(**patch)
        self.store.add_event(
            "hud", f"offset {result['hud']['mode']} {result['hud']['offset_x']},{result['hud']['offset_y']}"
        )
        return result

    def set_hud_scale(self, scale: float) -> dict:
        result = write_hud_state(scale=float(scale))
        self.store.add_event("hud", f"scale {result['hud']['scale']:.2f}")
        return result

    def reset_hud_position(self) -> dict:
        result = write_hud_state(mode="auto", offset_x=12, offset_y=12)
        self.store.add_event("hud", "offset auto")
        return result

    def keyboard_payload(self) -> dict:
        return {
            "keyboard_expanded": self.keyboard_expanded(),
            "keys_recent": self.keys_recent(),
            "hud_visible": self.hud_visible(),
            "hud": self.hud_state(),
            "task": self.task_status(),
        }

    # -- target window ---------------------------------------------------
    def _load_target(self) -> int:
        """Read the shared target written by whichever MCP instance last acted.

        The file is tiny; reading it on each state poll keeps the HUD on the
        window that is actually being controlled, even when several MCP client
        processes exist and only one owns the telemetry port.
        """
        try:
            data = json.loads(TARGET_PATH.read_text(encoding="utf-8"))
            return int(data.get("id") or 0) or getattr(self, "_target_id", 0)
        except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError):
            return getattr(self, "_target_id", 0) or 0

    def _save_target(self) -> None:
        try:
            TARGET_PATH.parent.mkdir(parents=True, exist_ok=True)
            TARGET_PATH.write_text(
                json.dumps({"id": self._target_id, "updated": time.time()}, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    def set_target_window(self, window_id: int | None, reason: str) -> None:
        """Remember the window the driver is working with; the HUD follows it."""
        if not window_id or self.driver is None:
            return
        top = self.driver.top_level_window(int(window_id))
        if not top or self.driver.is_hud_window(top):
            return
        if self.driver.is_frame_info(self.driver.window_info(top)):
            return
        if top != self._target_id:
            self._target_id = int(top)
            self._save_target()
            info = self.driver.window_info(top) or {}
            self.store.add_event("target", f"{top} {info.get('title', '')}"[:120])

    def set_target_from_pointer(self, reason: str) -> None:
        # In a stacked/tabbed layout several clients share one rectangle; the
        # visible one is the active window, so prefer it when the pointer is
        # inside its client area.
        if self.driver is None:
            return
        window_id = self.driver.client_under_pointer()
        if window_id:
            self.set_target_window(window_id, reason)

    def target_window(self) -> int:
        if self.driver is None:
            return 0
        shared = self._load_target()
        if shared and shared != self._target_id:
            self._target_id = shared
        if self._target_id and self.driver.window_info(self._target_id):
            return self._target_id
        if self._target_id:
            self._target_id = 0
            self._save_target()
        return self.driver.active_window()

    def target_payload(self) -> dict | None:
        if self.driver is None:
            return None
        window_id = self.target_window()
        if not window_id:
            return None
        info = self.driver.window_info(window_id)
        if info is None or self.driver.is_frame_info(info):
            active = self.driver.active_window()
            info = self.driver.window_info(active) if active and active != window_id else None
            if info is None or self.driver.is_frame_info(info):
                self._target_id = 0
                self._save_target()
                return None
            window_id = active
            self._target_id = int(active)
            self._save_target()
        info["active"] = window_id == self.driver.active_window()
        return info

    # -- internals -------------------------------------------------------
    def _require_armed(self, action: str) -> None:
        self._driver()
        self.guard.require_armed(action)

    def _should_continue(self) -> bool:
        return self.guard.action_state() == "run"

    def _run(self, plan: motion.MovePlan, action: str, target: str, speed: float = 1.0) -> dict:
        result = self._driver().play(
            plan,
            speed=speed,
            state_fn=self.guard.action_state,
            on_pause=self.driver.release_buttons,
        )
        recorded = self.store.record_move(
            result.path, action=action, target=target, duration_s=result.duration_s
        )
        if result.aborted:
            self.store.add_event("aborted", f"{action} {target}")
        else:
            self.store.add_event(action, target)
        cursor = self._driver().pointer()
        return {
            "ok": True,
            "aborted": result.aborted,
            "points": result.points,
            "duration_s": round(result.duration_s, 3),
            "distance_px": recorded["distance_px"],
            "cursor": {"x": cursor[0], "y": cursor[1]},
        }

    # -- actions ---------------------------------------------------------
    def move(self, x: int, y: int, duration_s: float | None = None, curve: str = "human") -> dict:
        self._require_armed("mouse_move")
        tx, ty = self.guard.check_point(x, y, self._driver().screen)
        sx, sy = self._driver().sync_position_from_pointer()
        plan = motion.plan_move(sx, sy, tx, ty, duration_s=duration_s, curve=curve)
        result = self._run(plan, "move", f"{tx},{ty}")
        if not result["aborted"]:
            self.set_target_from_pointer("move")
        return result

    def click(self, button: str = "left", count: int = 1, x: int | None = None, y: int | None = None) -> dict:
        self._require_armed("mouse_click")
        count = max(1, min(3, int(count)))
        target = f"{button}x{count}"
        payload: dict[str, Any] = {"ok": True, "button": button, "count": count}
        if x is not None and y is not None:
            tx, ty = self.guard.check_point(x, y, self._driver().screen)
            sx, sy = self._driver().sync_position_from_pointer()
            plan = motion.plan_move(sx, sy, tx, ty, click_approach=True)
            moved = self._run(plan, "move", f"{tx},{ty}")
            payload.update({k: moved[k] for k in ("points", "duration_s", "aborted")})
            if moved["aborted"]:
                payload["clicked"] = False
                return payload
            if plan.settle_ms:
                time.sleep(plan.settle_ms / 1000.0)
            target = f"{tx},{ty} {button}x{count}"
        else:
            time.sleep(0.05)
        if not self._should_continue():
            raise self._aborted_error("mouse_click")
        self.set_target_from_pointer("click")
        info = self.driver.click(button, count)
        payload["clicked"] = True
        payload["hold_ms"] = info["hold_ms"]
        payload["cursor"] = self._cursor()
        self.store.add_event("click", target)
        return payload

    def drag(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        button: str = "left",
    ) -> dict:
        self._require_armed("mouse_drag")
        sx, sy = self.guard.check_point(x1, y1, self._driver().screen)
        ex, ey = self.guard.check_point(x2, y2, self._driver().screen)
        cx, cy = self._driver().sync_position_from_pointer()
        approach = motion.plan_move(cx, cy, sx, sy, click_approach=True)
        moved = self._run(approach, "move", f"{sx},{sy}")
        if moved["aborted"]:
            return {"ok": True, "aborted": True, "dragged": False, "points": moved["points"]}
        time.sleep(max(0.05, approach.settle_ms / 1000.0))
        if not self._should_continue():
            raise self._aborted_error("mouse_drag")
        self._driver().button(button, True)
        released = False
        try:
            travel = motion.plan_move(sx, sy, ex, ey, duration_s=min(MAX_DRAG_SEGMENT_S, motion.fitts_duration(
                ((ex - sx) ** 2 + (ey - sy) ** 2) ** 0.5
            )))
            dragged = self._driver().play(
                travel,
                state_fn=lambda: "run" if self.guard.action_state() == "run" else "stop",
            )
            time.sleep(0.08)
        finally:
            self._driver().button(button, False)
            released = True
        self.store.record_move(
            dragged.path, action="drag", target=f"{sx},{sy}->{ex},{ey}", duration_s=dragged.duration_s
        )
        self.set_target_from_pointer("drag")
        self.store.add_event("drag", f"{sx},{sy}->{ex},{ey} {button}")
        return {
            "ok": True,
            "aborted": dragged.aborted,
            "dragged": True,
            "released": released,
            "points": moved["points"] + dragged.points,
            "duration_s": round(dragged.duration_s, 3),
            "cursor": self._cursor(),
        }

    def scroll(self, dy: int, dx: int = 0) -> dict:
        self._require_armed("mouse_scroll")
        dy, dx = int(dy), int(dx)
        if dy == 0 and dx == 0:
            raise ValueError("scroll needs a non-zero dy or dx")
        if abs(dy) > MAX_SCROLL_TICKS or abs(dx) > MAX_SCROLL_TICKS:
            raise ValueError(f"scroll delta must be within ±{MAX_SCROLL_TICKS} ticks")
        steps = motion.plan_scroll(dy, dx)
        emitted = 0
        for step in steps:
            if not self._should_continue():
                raise self._aborted_error("mouse_scroll")
            if dy:
                self._driver().wheel(step.ticks, horizontal=False, down=dy < 0)
            if dx:
                self._driver().wheel(step.ticks, horizontal=True, down=dx < 0)
            emitted += step.ticks
            time.sleep(step.gap_s)
        self.store.add_event("scroll", f"dy={dy} dx={dx} events={len(steps)}")
        return {"ok": True, "events": len(steps), "ticks": emitted, "dy": dy, "dx": dx}

    def type_text(self, text: str) -> dict:
        self._require_armed("mouse_type")
        self.set_target_window(self._driver().active_window(), "type")
        self.record_keys([key_label(char) for char in text])
        info = self._driver().type_text(text)
        self.store.add_event("type", f"{info['chars']} chars")
        return {"ok": True, **info}

    def key(self, combo: str) -> dict:
        self._require_armed("mouse_key")
        self.set_target_window(self._driver().active_window(), "key")
        parts = [key_label(part) for part in str(combo).split("+")]
        self.record_keys(["+".join(parts)])
        info = self._driver().key(combo)
        self.store.add_event("key", combo)
        return {"ok": True, **info}

    # -- observation -----------------------------------------------------
    def list_windows(self) -> dict:
        windows = self._driver().list_windows()
        return {"ok": True, "count": len(windows), "windows": windows[:40]}

    def focus_window(self, title: str) -> dict:
        self._require_armed("mouse_focus_window")
        result = self._driver().focus_window(title)
        if result.get("ok"):
            self.set_target_window(int(result["window"]["id"], 16), "focus")
        self.store.add_event("focus", title if result.get("ok") else f"miss:{title}")
        return result

    def telemetry(self, limit: int = 20) -> dict:
        limit = max(1, min(50, int(limit)))
        return {
            "ok": True,
            "state": self.api.build_state(),
            "sessions": self.store.sessions(limit),
        }

    def replay_session(self, session_id: int, speed: float = 2.0) -> dict:
        self._require_armed("mouse_replay_session")
        data = self.store.session_path(int(session_id), max_points=1200)
        if not data.get("ok"):
            raise ValueError(data.get("error") or "session not found")
        raw = data["points"]
        if not raw:
            return {"ok": True, "points": 0, "note": "session has no recorded path"}
        step = motion.SAMPLE_INTERVAL_S
        points = [motion.Point(float(x), float(y), i * step) for i, (x, y, _t) in enumerate(raw)]
        plan = motion.MovePlan(
            start=(points[0].x, points[0].y),
            end=(points[-1].x, points[-1].y),
            duration_s=points[-1].t,
            points=points,
            curve="replay",
        )
        speed = max(0.25, min(8.0, float(speed)))
        self.store.add_event("replay", f"session {session_id} speed {speed:g}x")
        return self._run(plan, "replay", f"session {session_id}", speed=speed)

    # -- helpers ---------------------------------------------------------
    def _cursor(self) -> dict:
        pos = self._driver().pointer()
        return {"x": pos[0], "y": pos[1]}

    @staticmethod
    def _aborted_error(action: str):
        from .guard import GuardError

        return GuardError(f"{action} aborted: the driver was disarmed mid-action", code="aborted")


_service: MouseService | None = None


def get_service(*, dry_run: bool | None = None, start_api: bool = True) -> MouseService:
    global _service
    if _service is None:
        _service = MouseService(dry_run=dry_run, start_api=start_api)
    return _service
