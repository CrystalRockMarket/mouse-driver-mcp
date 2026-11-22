"""X11 driver: the real pointer and keyboard on ``DISPLAY``.

XTEST is the primary backend (``libX11`` + ``libXtst`` through ctypes) because
it needs no subprocess, keeps a sub-pixel accumulator and cannot be confused by
argument quoting. ``xdotool`` is only a fallback when XTEST cannot be loaded.

Set ``MOUSE_DRIVER_DRY=1`` to record motion instead of emitting it; tests use
that so they never touch the real cursor.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import random
import sys
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .motion import MovePlan, Point

BUTTONS = {"left": 1, "middle": 2, "right": 3}
#: X11 keysyms for the control characters we type.
SPECIAL_KEYSYMS = {"\n": 0xFF0D, "\t": 0xFF09, "\b": 0xFF08, "\x1b": 0xFF1B}
COMBO_RE = re.compile(r"^[A-Za-z0-9_+ \-]{1,60}$")
SHIFT_L = 0xFFE1


class X11Unavailable(RuntimeError):
    """No usable X display or input backend."""


@dataclass
class PlayResult:
    points: int = 0
    emitted: int = 0
    aborted: bool = False
    paused: bool = False
    duration_s: float = 0.0
    path: list[Point] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "points": self.points,
            "emitted": self.emitted,
            "aborted": self.aborted,
            "paused": self.paused,
            "duration_s": round(self.duration_s, 3),
        }


class X11Driver:
    """Human-shaped pointer/keyboard control with a dry-run mode."""

    def __init__(self, *, dry_run: bool | None = None, display: str | None = None) -> None:
        self.dry = config.DRY_RUN if dry_run is None else dry_run
        if display is None:
            display = os.environ.get("DISPLAY") or ""
            if not display and not self.dry:
                display = config.X_DISPLAY_FALLBACK
                os.environ["DISPLAY"] = display
                print(f"mouse-driver: DISPLAY unset, defaulted to {display}", file=sys.stderr, flush=True)
        if not self.dry:
            xauthority = Path.home() / ".Xauthority"
            if not os.environ.get("XAUTHORITY") and xauthority.exists():
                os.environ["XAUTHORITY"] = str(xauthority)
                print(f"mouse-driver: XAUTHORITY defaulted to {xauthority}", file=sys.stderr, flush=True)
        self.display_name = display or None
        self.rng = random.Random()
        self._lock = threading.RLock()
        self.backend = "dry" if self.dry else "none"
        self._x11 = None
        self._xtst = None
        self._dpy = None
        self._screen_index = 0
        self._root = 0
        self._pos: tuple[float, float] = (0.0, 0.0)
        self._emitted: tuple[int, int] | None = None
        self.emit_count = 0
        self.recorded: list[tuple[int, int]] = []
        self.monitors = config.DEFAULT_SCREEN[2]
        self.screen = (config.DEFAULT_SCREEN[0], config.DEFAULT_SCREEN[1])

        if not self.dry:
            self._init_x()
        self._refresh_screen()

    # -- setup -----------------------------------------------------------
    def _init_x(self) -> None:
        if config.FORCE_NO_X:
            raise X11Unavailable("XOpenDisplay failed")
        libname = ctypes.util.find_library("X11") or "libX11.so.6"
        try:
            x11 = ctypes.CDLL(libname)
        except OSError as exc:  # pragma: no cover - host dependent
            raise X11Unavailable(f"cannot load libX11: {exc}") from exc

        # Xlib is only thread-safe after XInitThreads: the HUD/API thread reads
        # the pointer while the motion thread emits events.
        x11.XInitThreads.argtypes = []
        x11.XInitThreads.restype = ctypes.c_int
        x11.XInitThreads()

        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XDisplayWidth.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x11.XDisplayWidth.restype = ctypes.c_int
        x11.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x11.XDisplayHeight.restype = ctypes.c_int
        x11.XDefaultScreen.argtypes = [ctypes.c_void_p]
        x11.XDefaultScreen.restype = ctypes.c_int
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XQueryPointer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
        ]
        x11.XQueryPointer.restype = ctypes.c_int
        x11.XQueryTree.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
            ctypes.POINTER(ctypes.c_uint),
        ]
        x11.XQueryTree.restype = ctypes.c_int
        x11.XDisplayKeycodes.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        x11.XDisplayKeycodes.restype = ctypes.c_int
        x11.XGetKeyboardMapping.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ubyte,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        x11.XGetKeyboardMapping.restype = ctypes.POINTER(ctypes.c_ulong)
        x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x11.XKeysymToKeycode.restype = ctypes.c_ubyte
        x11.XFree.argtypes = [ctypes.c_void_p]

        dpy = x11.XOpenDisplay(self.display_name.encode() if self.display_name else None)
        if not dpy:
            raise X11Unavailable("XOpenDisplay failed")
        self._x11 = x11
        self._dpy = dpy
        self._screen_index = x11.XDefaultScreen(dpy)
        self._root = x11.XDefaultRootWindow(dpy)
        self.backend = "xdotool"

        try:
            xtst = ctypes.CDLL(ctypes.util.find_library("Xtst") or "libXtst.so.6")
            xtst.XTestFakeMotionEvent.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_ulong,
            ]
            xtst.XTestFakeMotionEvent.restype = ctypes.c_int
            xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
            xtst.XTestFakeButtonEvent.restype = ctypes.c_int
            xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
            xtst.XTestFakeKeyEvent.restype = ctypes.c_int
            self._xtst = xtst
            self.backend = "xtest"
        except OSError:
            self._xtst = None

    def _refresh_screen(self) -> None:
        if self.dry or self._x11 is None:
            return
        width = int(self._x11.XDisplayWidth(self._dpy, self._screen_index))
        height = int(self._x11.XDisplayHeight(self._dpy, self._screen_index))
        if width > 0 and height > 0:
            self.screen = (width, height)
        try:
            out = subprocess.run(
                ["xrandr", "--listmonitors"], capture_output=True, text=True, timeout=2, check=False
            ).stdout
            match = re.search(r"Monitors:\s*(\d+)", out)
            if match:
                self.monitors = int(match.group(1))
        except (OSError, subprocess.SubprocessError):
            pass

    @property
    def available(self) -> bool:
        return self.dry or self.backend in {"xtest", "xdotool"}

    def geometry(self) -> dict:
        return {"w": self.screen[0], "h": self.screen[1], "monitors": self.monitors, "backend": self.backend}

    # -- pointer ---------------------------------------------------------
    def pointer(self) -> tuple[int, int]:
        with self._lock:
            return self._pointer_locked()

    def pointer_window(self) -> int:
        """Top-level window under the pointer (child of root), 0 when on root."""
        with self._lock:
            return self._pointer_window_locked()

    def _pointer_window_locked(self) -> int:
        if self.dry or self._xtst is None or not self._dpy:
            return 0
        root_ret = ctypes.c_ulong()
        child_ret = ctypes.c_ulong()
        coords = [ctypes.c_int() for _ in range(4)]
        mask = ctypes.c_uint()
        ok = self._x11.XQueryPointer(
            self._dpy,
            self._root,
            ctypes.byref(root_ret),
            ctypes.byref(child_ret),
            ctypes.byref(coords[0]),
            ctypes.byref(coords[1]),
            ctypes.byref(coords[2]),
            ctypes.byref(coords[3]),
            ctypes.byref(mask),
        )
        return int(child_ret.value) if ok else 0

    def _pointer_locked(self) -> tuple[int, int]:
        if self._xtst is not None and self._dpy:
            root_ret = ctypes.c_ulong()
            child_ret = ctypes.c_ulong()
            rx, ry = ctypes.c_int(), ctypes.c_int()
            wx, wy = ctypes.c_int(), ctypes.c_int()
            mask = ctypes.c_uint()
            ok = self._x11.XQueryPointer(
                self._dpy,
                self._root,
                ctypes.byref(root_ret),
                ctypes.byref(child_ret),
                ctypes.byref(rx),
                ctypes.byref(ry),
                ctypes.byref(wx),
                ctypes.byref(wy),
                ctypes.byref(mask),
            )
            if ok:
                return (int(rx.value), int(ry.value))
        if self.dry:
            return (int(round(self._pos[0])), int(round(self._pos[1])))
        try:
            out = subprocess.run(
                ["xdotool", "getmouselocation", "--shell"],
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            ).stdout
            values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
            return (int(values.get("X", 0)), int(values.get("Y", 0)))
        except (OSError, subprocess.SubprocessError, ValueError) as exc:  # pragma: no cover
            raise X11Unavailable(f"cannot read pointer: {exc}") from exc

    def move_to(self, x: float, y: float) -> tuple[int, int]:
        """Emit one integer pointer position; sub-pixel state is accumulated."""
        self._pos = (float(x), float(y))
        target = (int(round(x)), int(round(y)))
        if self.dry:
            self._emitted = target
            self.recorded.append(target)
            self.emit_count += 1
            return target
        with self._lock:
            if target == self._emitted:
                return target
            if self._xtst is not None:
                self._xtst.XTestFakeMotionEvent(self._dpy, -1, target[0], target[1], 0)
                self._x11.XFlush(self._dpy)
            else:  # pragma: no cover - only without XTEST
                subprocess.run(
                    ["xdotool", "mousemove", "--sync", str(target[0]), str(target[1])],
                    check=True,
                    timeout=2,
                )
        self._emitted = target
        self.emit_count += 1
        return target

    def sync_position_from_pointer(self) -> tuple[int, int]:
        pos = self.pointer()
        self._pos = (float(pos[0]), float(pos[1]))
        self._emitted = pos
        return pos

    def play(
        self,
        plan: MovePlan,
        should_continue: Callable[[], bool] | None = None,
        *,
        on_point: Callable[[Point], None] | None = None,
        speed: float = 1.0,
        state_fn: Callable[[], str] | None = None,
        on_pause: Callable[[], None] | None = None,
    ) -> PlayResult:
        """Emit a planned path, honouring pause and abort within one sample.

        ``state_fn`` returns ``run``, ``pause`` or ``stop``. A pause holds the
        current position (releasing any held button through ``on_pause``) and
        resumes with the remaining points of the same plan; ``stop`` ends the
        move immediately.
        """
        speed = max(0.1, min(8.0, float(speed)))
        if state_fn is None:
            gate = should_continue or (lambda: True)
            state_fn = lambda: "run" if gate() else "stop"  # noqa: E731
        result = PlayResult()
        started = time.monotonic()
        t0 = started
        paused_once = False
        for point in plan.points:
            state = state_fn()
            while state == "pause":
                if not paused_once:
                    paused_once = True
                    if on_pause is not None:
                        on_pause()
                time.sleep(0.01)
                state = state_fn()
            if state != "run":
                result.aborted = True
                break
            due = t0 + point.t / speed
            now = time.monotonic()
            if due > now:
                time.sleep(min(due - now, 0.25))
            self.move_to(point.x, point.y)
            result.path.append(point)
            if on_point is not None:
                on_point(point)
        result.points = len(result.path)
        result.emitted = self.emit_count
        result.paused = paused_once
        result.duration_s = time.monotonic() - started
        return result

    # -- buttons ---------------------------------------------------------
    def button(self, button: str | int, pressed: bool) -> None:
        number = BUTTONS.get(button, button) if isinstance(button, str) else button
        number = int(number)
        if number not in (1, 2, 3, 4, 5, 6, 7):
            raise ValueError(f"unsupported mouse button {number}")
        if self.dry:
            return
        with self._lock:
            if self._xtst is not None:
                self._xtst.XTestFakeButtonEvent(self._dpy, number, 1 if pressed else 0, 0)
                self._x11.XFlush(self._dpy)
            else:  # pragma: no cover
                verb = "mousedown" if pressed else "mouseup"
                subprocess.run(["xdotool", verb, str(number)], check=True, timeout=2)

    def click(self, button: str = "left", count: int = 1) -> dict:
        number = BUTTONS.get(button, None)
        if number is None:
            raise ValueError(f"unknown button {button!r}; use left, middle or right")
        count = max(1, min(3, int(count)))
        holds: list[int] = []
        for index in range(count):
            hold_ms = self.rng.randint(35, 90)
            holds.append(hold_ms)
            if self.dry:
                continue
            if index:
                time.sleep(self.rng.uniform(0.06, 0.14))
            self.button(number, True)
            time.sleep(hold_ms / 1000.0)
            self.button(number, False)
        return {"button": button, "count": count, "hold_ms": holds}

    def wheel(self, ticks: int, *, horizontal: bool, down: bool) -> None:
        number = (7 if down else 6) if horizontal else (5 if down else 4)
        for _ in range(abs(int(ticks))):
            if self.dry:
                continue
            self.button(number, True)
            self.button(number, False)

    # -- keyboard --------------------------------------------------------
    def _build_keymap(self) -> dict[int, tuple[int, bool]]:
        if self._x11 is None or not self._dpy:
            return {}
        low, high = ctypes.c_int(), ctypes.c_int()
        self._x11.XDisplayKeycodes(self._dpy, ctypes.byref(low), ctypes.byref(high))
        first, count = int(low.value), int(high.value) - int(low.value) + 1
        per = ctypes.c_int()
        ptr = self._x11.XGetKeyboardMapping(self._dpy, first, count, ctypes.byref(per))
        mapping: dict[int, tuple[int, bool]] = {}
        if not ptr:
            return mapping
        try:
            width = max(1, int(per.value))
            for kc_index in range(count):
                base = kc_index * width
                for level in range(min(2, width)):
                    keysym = int(ptr[base + level])
                    if keysym and 0x20 <= keysym <= 0x10FFFF and keysym not in mapping:
                        mapping[keysym] = (first + kc_index, level == 1)
        finally:
            self._x11.XFree(ctypes.cast(ptr, ctypes.c_void_p))
        return mapping

    def _tap_keycode(self, keycode: int, shift: bool) -> None:
        with self._lock:
            self._tap_keycode_locked(keycode, shift)

    def _tap_keycode_locked(self, keycode: int, shift: bool) -> None:
        shift_kc = self._x11.XKeysymToKeycode(self._dpy, SHIFT_L)
        if shift:
            self._xtst.XTestFakeKeyEvent(self._dpy, shift_kc, 1, 0)
        self._xtst.XTestFakeKeyEvent(self._dpy, keycode, 1, 0)
        self._xtst.XTestFakeKeyEvent(self._dpy, keycode, 0, 0)
        if shift:
            self._xtst.XTestFakeKeyEvent(self._dpy, shift_kc, 0, 0)
        self._x11.XFlush(self._dpy)

    def type_text(self, text: str) -> dict:
        """Type text with 55–140 ms per character. Never passes text in argv."""
        if not isinstance(text, str) or not text:
            raise ValueError("text must be a non-empty string")
        text = text[:2000]
        if self.dry:
            return {"chars": len(text), "typed": len(text), "fallback": 0, "backend": "dry"}
        keymap = self._build_keymap() if self._xtst is not None else {}
        typed = fallback = 0
        pending: list[str] = []

        def flush_pending() -> None:
            nonlocal fallback
            if not pending:
                return
            chunk = "".join(pending)
            pending.clear()
            subprocess.run(
                [
                    "xdotool",
                    "type",
                    "--clearmodifiers",
                    "--delay",
                    str(self.rng.randint(55, 140)),
                    "--file",
                    "-",
                ],
                input=chunk,
                text=True,
                check=False,
                timeout=10,
            )
            fallback += len(chunk)

        for char in text:
            keysym = SPECIAL_KEYSYMS.get(char, ord(char))
            entry = keymap.get(keysym)
            if entry is None:
                pending.append(char)
                continue
            flush_pending()
            self._tap_keycode(entry[0], entry[1])
            typed += 1
            time.sleep(self.rng.uniform(0.055, 0.140))
        flush_pending()
        return {"chars": len(text), "typed": typed, "fallback": fallback, "backend": "xtest"}

    def key(self, combo: str) -> dict:
        if not COMBO_RE.match(combo or ""):
            raise ValueError("combo must use xdotool key syntax, e.g. 'ctrl+alt+t'")
        if self.dry:
            return {"combo": combo, "sent": True, "backend": "dry"}
        subprocess.run(["xdotool", "key", "--clearmodifiers", combo], check=True, timeout=5)
        return {"combo": combo, "sent": True, "backend": "xdotool"}

    # -- windows ---------------------------------------------------------
    def top_level_window(self, window_id: int) -> int:
        """Walk parents until the root; returns the client window id."""
        if self.dry or not self._x11 or not self._dpy or not window_id:
            return 0
        current = int(window_id)
        with self._lock:
            for _ in range(32):
                root_ret = ctypes.c_ulong()
                parent_ret = ctypes.c_ulong()
                children = ctypes.POINTER(ctypes.c_ulong)()
                count = ctypes.c_uint()
                ok = self._x11.XQueryTree(
                    self._dpy,
                    ctypes.c_ulong(current),
                    ctypes.byref(root_ret),
                    ctypes.byref(parent_ret),
                    ctypes.byref(children),
                    ctypes.byref(count),
                )
                if children:
                    self._x11.XFree(ctypes.cast(children, ctypes.c_void_p))
                if not ok:
                    break
                parent = int(parent_ret.value)
                if parent in (0, int(root_ret.value), int(self._root)):
                    break
                current = parent
        # i3 reparents clients into container windows; the app is the client.
        return self._descend_to_client(current)

    def _children(self, window_id: int) -> list[int]:
        if not self._x11 or not self._dpy:
            return []
        with self._lock:
            root_ret = ctypes.c_ulong()
            parent_ret = ctypes.c_ulong()
            children = ctypes.POINTER(ctypes.c_ulong)()
            count = ctypes.c_uint()
            ok = self._x11.XQueryTree(
                self._dpy,
                ctypes.c_ulong(int(window_id)),
                ctypes.byref(root_ret),
                ctypes.byref(parent_ret),
                ctypes.byref(children),
                ctypes.byref(count),
            )
            if not ok or not children:
                return []
            try:
                return [int(children[i]) for i in range(min(int(count.value), 32))]
            finally:
                self._x11.XFree(ctypes.cast(children, ctypes.c_void_p))

    def _is_client_window(self, window_id: int) -> bool:
        try:
            out = subprocess.run(
                ["xprop", "-id", hex(int(window_id)), "-notype", "WM_CLASS"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        found = re.search(r'WM_CLASS(?:\(STRING\))? = "([^"]*)", "([^"]*)"', out)
        if not found:
            return False
        if (found.group(2) or found.group(1)).lower() in {"i3-frame", "i3-frame-2"}:
            return False  # i3's own container, not the application
        return True

    def _descend_to_client(self, window_id: int, depth: int = 2) -> int:
        if self._is_client_window(window_id):
            return window_id
        if depth <= 0:
            return window_id
        for child in self._children(window_id):
            if self._is_client_window(child):
                return child
            found = self._descend_to_client(child, depth - 1)
            if found != child:
                return found
        return window_id

    def active_window(self) -> int:
        if self.dry or self._x11 is None:
            return 0
        try:
            out = subprocess.run(
                ["xprop", "-root", "_NET_ACTIVE_WINDOW"], capture_output=True, text=True, timeout=2, check=False
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return 0
        match = re.search(r"window id # (0x[0-9a-fA-F]+)", out)
        return int(match.group(1), 16) if match else 0

    def current_desktop(self) -> int:
        if self.dry or self._x11 is None:
            return 0
        try:
            out = subprocess.run(
                ["xprop", "-root", "_NET_CURRENT_DESKTOP"], capture_output=True, text=True, timeout=2, check=False
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return 0
        match = re.search(r"=\s*(\d+)", out)
        return int(match.group(1)) if match else 0

    def monitor_rects(self) -> list[tuple[int, int, int, int]]:
        """Physical monitor rectangles (root coordinates); cached for a second."""
        now = time.monotonic()
        cached = getattr(self, "_monitors_cache", None)
        if cached and now - cached[0] < 1.0:
            return cached[1]
        rects: list[tuple[int, int, int, int]] = []
        try:
            out = subprocess.run(
                ["xrandr", "--listmonitors"], capture_output=True, text=True, timeout=2, check=False
            ).stdout
            for match in re.finditer(r"(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)", out):
                width, height, x, y = (int(match.group(i)) for i in range(1, 5))
                rects.append((x, y, width, height))
        except (OSError, subprocess.SubprocessError):
            pass
        if not rects:
            rects = [(0, 0, self.screen[0], self.screen[1])]
        self._monitors_cache = (now, rects)
        return rects

    def intersects_any_monitor(self, x: int, y: int, w: int, h: int) -> bool:
        if w <= 0 or h <= 0:
            return False
        for mx, my, mw, mh in self.monitor_rects():
            if x < mx + mw and x + w > mx and y < my + mh and y + h > my:
                return True
        return False

    def window_info(self, window_id: int) -> dict | None:
        """Geometry, title, class and visibility of a top-level client window."""
        if not window_id or self.dry or self._x11 is None:
            return None
        try:
            info = subprocess.run(
                ["xwininfo", "-id", hex(int(window_id))], capture_output=True, text=True, timeout=3, check=False
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        if "No such window" in info or not info:
            return None

        def grab(pattern: str, default: int = 0) -> int:
            found = re.search(pattern, info)
            return int(found.group(1)) if found else default

        x = grab(r"Absolute upper-left X:\s*(-?\d+)")
        y = grab(r"Absolute upper-left Y:\s*(-?\d+)")
        w = grab(r"Width:\s*(\d+)")
        h = grab(r"Height:\s*(\d+)")
        mapped = "Map State: IsViewable" in info
        if w <= 0 or h <= 0:
            return None

        title, cls = "", ""
        try:
            props = subprocess.run(
                ["xprop", "-id", hex(int(window_id)), "-notype", "_NET_WM_NAME", "WM_CLASS", "_NET_WM_DESKTOP"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            ).stdout
            name = re.search(r'_NET_WM_NAME(?:\(UTF8_STRING\))? = "(.*)"', props)
            if name:
                title = name.group(1)
            klass = re.search(r'WM_CLASS(?:\(STRING\))? = "([^"]*)", "([^"]*)"', props)
            if klass:
                cls = klass.group(2) or klass.group(1)
            desk = re.search(r"_NET_WM_DESKTOP(?:\(CARDINAL\))? = (\d+)", props)
            desktop = int(desk.group(1)) if desk else -1
        except (OSError, subprocess.SubprocessError):
            desktop = -1

        if not title:
            name = re.search(r'xwininfo: Window id: \S+ "(.*)"', info)
            title = name.group(1) if name else ""
        if title.endswith(" — Mozilla Firefox"):
            title = title[: -len(" — Mozilla Firefox")]
        elif title.endswith(" - Mozilla Firefox"):
            title = title[: -len(" - Mozilla Firefox")]

        # Visibility is geometry-based: i3's per-output desktops make the
        # desktop index a poor proxy (two desktops can be physically visible).
        return {
            "id": int(window_id),
            "title": title[:120],
            "cls": cls[:40],
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "desktop": desktop,
            "mapped": mapped,
            "visible": mapped and self.intersects_any_monitor(x, y, w, h),
        }

    @staticmethod
    def is_frame_info(info: dict | None) -> bool:
        if not info:
            return True
        if (info.get("cls") or "").lower() in {"i3-frame", "i3-frame-2"}:
            return True
        return (info.get("title") or "").startswith("[i3 con]")

    def _child_at(self, window_id: int) -> int:
        """Deepest child of ``window_id`` that currently contains the pointer."""
        if not self._x11 or not self._dpy:
            return 0
        with self._lock:
            root_ret = ctypes.c_ulong()
            child_ret = ctypes.c_ulong()
            coords = [ctypes.c_int() for _ in range(4)]
            mask = ctypes.c_uint()
            ok = self._x11.XQueryPointer(
                self._dpy,
                ctypes.c_ulong(int(window_id)),
                ctypes.byref(root_ret),
                ctypes.byref(child_ret),
                ctypes.byref(coords[0]),
                ctypes.byref(coords[1]),
                ctypes.byref(coords[2]),
                ctypes.byref(coords[3]),
                ctypes.byref(mask),
            )
            return int(child_ret.value) if ok else 0

    def client_under_pointer(self) -> int:
        """Application client actually on top under the pointer.

        Follows the stacking chain from the root child down through subwindows
        and returns the deepest window that carries a WM_CLASS, so i3 frames,
        hidden tabs and render widgets are skipped correctly.
        """
        if self.dry:
            return 0
        window = self.pointer_window()
        if not window:
            return 0
        chain = [window]
        for _ in range(8):
            child = self._child_at(chain[-1])
            if not child or child in chain:
                break
            chain.append(child)
        for candidate in reversed(chain):
            if self._is_client_window(candidate) and not self.is_hud_window(candidate):
                return candidate
        return 0

    def release_buttons(self) -> list[int]:
        """Release the primary mouse buttons (panic path)."""
        released = []
        for number in (1, 2, 3):
            try:
                self.button(number, False)
                released.append(number)
            except Exception:
                pass
        return released

    def is_hud_window(self, window_id: int) -> bool:
        if not window_id or self.dry or self._x11 is None:
            return False
        try:
            out = subprocess.run(
                ["xprop", "-id", hex(int(window_id)), "-notype", "WM_CLASS", "_NET_WM_NAME", "WM_NAME"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return "hud.py" in out.lower() or "mouse driver hud" in out.lower()

    def list_windows(self) -> list[dict]:
        try:
            out = subprocess.run(
                ["wmctrl", "-lpG"], capture_output=True, text=True, timeout=3, check=False
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        windows = []
        for line in out.splitlines():
            parts = line.split(None, 7)
            if len(parts) < 8:
                continue
            wid, desktop, pid, x, y, w, h = parts[:7]
            title = parts[7].strip()
            try:
                geometry = [int(x), int(y), int(w), int(h)]
            except ValueError:
                continue
            windows.append(
                {
                    "id": wid,
                    "desktop": int(desktop) if desktop.lstrip("-").isdigit() else -1,
                    "pid": int(pid) if pid.isdigit() else 0,
                    "x": geometry[0],
                    "y": geometry[1],
                    "w": geometry[2],
                    "h": geometry[3],
                    "title": title,
                }
            )
        return windows

    def focus_window(self, title: str) -> dict:
        needle = (title or "").strip().lower()
        if not needle:
            raise ValueError("title must not be empty")
        matches = [w for w in self.list_windows() if needle in w["title"].lower()]
        if not matches:
            return {"ok": False, "error": f"no window title contains {title!r}", "windows": []}
        target = matches[0]
        if not self.dry:
            subprocess.run(["wmctrl", "-i", "-a", target["id"]], check=False, timeout=3)
        return {"ok": True, "window": target, "matches": len(matches)}
