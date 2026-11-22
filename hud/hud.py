#!/usr/bin/env python3
"""Mouse Driver HUD — a click-through, target-anchored companion panel.

The panel sits inside the top-right corner of the window the driver is working
with (12 px inset), follows it at 8 Hz, and hides when the target closes,
minimises or moves to another workspace. With no target at all it falls back to
the bottom-right of the primary monitor and says "no target".

It is click-through by default (empty X input shape), so it never blocks the
page. Controls are global chords, installed with passive X grabs that replay
the keys so the browser still receives them:

    P                     pause / resume (hold position, keep the TTL)
    Esc Esc               disarm (small mistake, no lock)
    Ctrl Alt Shift Esc    panic: stop, disarm, LOCK arming
    Ctrl Alt Shift Enter  unlock (human only; no MCP tool can)
    Ctrl Alt Shift P      alias for pause / resume

``--interactive`` disables click-through and shows Disarm/STOP buttons for
setup. The panel is opaque, so no compositor is required.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import re
import select
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cairo  # noqa: E402
import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from mouse_driver import hud_layout  # noqa: E402
from mouse_driver.config import API_PORT, STOP_PATH  # noqa: E402
from mouse_driver.guard import Guard  # noqa: E402

API = f"http://127.0.0.1:{API_PORT}"
POLL_S = 0.125  # 8 Hz
MARGIN = 12
MIN_W, MAX_W, MIN_H = 320, 520, 150
COLLAPSED_W, EXPANDED_W = 430, 520
KEY_UNIT, KEY_H = 24, 26
ESC_DOUBLE_S = 0.6

# X11 constants
ShiftMask, LockMask, ControlMask, Mod1Mask, Mod2Mask = 1, 2, 4, 8, 16
AnyModifier = 1 << 15
GrabModeAsync = 1
ReplayKeyboard = 2
KeyPress, KeyRelease = 2, 3

BASE_FONT = {
    "default": 11, "brand": 8, "target": 10, "pill": 9, "live": 10, "cap": 9,
    "legend": 9, "hint": 8, "close": 16, "grip": 11, "mode": 8, "recent": 8,
    "key": 9, "val": 12, "action": 10, "offline": 9, "button": 9,
}


PAUSE_IDLE = "#d7e2d2"
PAUSE_ACTIVE = "#e8b23c"


def css_for(scale: float) -> str:
    """Every size in the panel derived from one scale factor."""
    s = max(0.85, min(1.8, float(scale)))

    def px(value: float) -> str:
        return f"{max(1.0, value * s):.1f}px"

    f = BASE_FONT
    return f"""
window#hud {{ background-color: #111314; border: 1px solid #2f3a2f; }}
label {{ color: #d7e2d2; font-family: "DejaVu Sans", sans-serif; font-size: {px(f['default'])}; }}
label.brand {{ color: #6f7f6b; font-size: {px(f['brand'])}; font-weight: bold; }}
label.target {{ color: #c6f24e; font-size: {px(f['target'])}; font-weight: bold; }}
label.pill {{ border-radius: {px(9)}; padding: {px(1)} {px(9)}; font-size: {px(f['pill'])}; font-weight: bold; }}
label.pill-armed {{ background-color: #c6f24e; color: #111314; }}
label.pill-paused {{ background-color: #e8b23c; color: #111314; }}
label.pill-locked {{ background-color: #d94a3d; color: #ffffff; }}
label.pill-idle {{ background-color: #262d27; color: #93a38e; }}
label.pill-offline {{ background-color: #2b1a1a; color: #e8836f; }}
label.live {{ color: #eaf3e4; font-family: "DejaVu Sans Mono", monospace; font-size: {px(f['live'])}; }}
label.cap {{ background-color: #1b201c; color: #c6f24e; border: 1px solid #33402f;
            border-radius: {px(4)}; padding: {px(1)} {px(5)}; font-family: "DejaVu Sans Mono", monospace;
            font-size: {px(f['cap'])}; font-weight: bold; }}
label.cap-hot {{ background-color: #c6f24e; color: #111314; border-color: #c6f24e; }}
label.cap-dim {{ color: #5c6a58; border-color: #232b23; }}
label.legend {{ color: #93a38e; font-size: {px(f['legend'])}; }}
label.legend-dim {{ color: #4f5c4b; font-size: {px(f['legend'])}; }}
label.hint {{ color: #6f7f6b; font-size: {px(f['hint'])}; }}
label.close {{ color: #7d868a; font-size: {px(f['close'])}; font-weight: bold; }}
label.close-hot {{ color: #eef1f2; }}
label.grip {{ color: #5d6a5f; font-size: {px(f['grip'])}; }}
label.grip-hot {{ color: #a9b8a3; }}
label.mode {{ color: #6f7f6b; font-size: {px(f['mode'])}; }}
label.pause {{ color: {PAUSE_IDLE}; font-size: {px(f['close'])}; font-weight: bold; }}
label.pause-active {{ color: {PAUSE_ACTIVE}; }}
label.pause-off {{ color: #4a5450; }}
label.recent {{ color: #6f7f6b; font-size: {px(f['recent'])}; font-weight: bold; }}
label.key {{ background-color: #1b201c; color: #cfdccb; border: 1px solid #2a332a;
            border-radius: {px(3)}; font-family: "DejaVu Sans Mono", monospace; font-size: {px(f['key'])}; }}
label.key-mod {{ color: #94a68f; }}
label.key-hot {{ background-color: #c6f24e; color: #111314; border: {px(2)} solid #c6f24e; }}
label.val {{ color: #eaf3e4; font-family: "DejaVu Sans Mono", monospace; font-size: {px(f['val'])}; }}
label.action {{ color: #c6f24e; font-size: {px(f['action'])}; }}
label.offline {{ color: #e8836f; font-size: {px(f['offline'])}; }}
button {{ background-image: none; background-color: #1b201c; color: #c6f24e;
         border: 1px solid #33402f; border-radius: {px(5)}; padding: {px(3)} {px(8)};
         font-size: {px(f['button'])}; font-weight: bold; }}
button.stop {{ background-color: #3a1517; color: #ff9b8a; border-color: #6b2a2a; }}
"""


# -- local control helpers ---------------------------------------------------
def post(path: str, timeout: float = 1.0) -> bool:
    try:
        req = urllib.request.Request(
            API + path, data=b"{}", method="POST", headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def post_json(path: str, payload: dict) -> bool:
    try:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            API + path, data=body, method="POST", headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=1.0) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def fetch_state() -> dict | None:
    try:
        with urllib.request.urlopen(API + "/api/state", timeout=0.6) as response:
            if response.status != 200:
                return None
            data = json.loads(response.read().decode())
            return data if isinstance(data, dict) else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def read_geometry(window_id: int) -> dict | None:
    """Fresh client geometry, re-read by the HUD on every poll."""
    try:
        out = subprocess.run(
            ["xwininfo", "-id", hex(int(window_id))], capture_output=True, text=True, timeout=2, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if not out or "No such window" in out:
        return None

    def grab(pattern: str) -> int:
        found = re.search(pattern, out)
        return int(found.group(1)) if found else 0

    width = grab(r"Width:\s*(\d+)")
    height = grab(r"Height:\s*(\d+)")
    if width <= 0 or height <= 0:
        return None
    return {
        "x": grab(r"Absolute upper-left X:\s*(-?\d+)"),
        "y": grab(r"Absolute upper-left Y:\s*(-?\d+)"),
        "w": width,
        "h": height,
        "mapped": "Map State: IsViewable" in out,
    }


class KeyGrab(threading.Thread):
    """Passive global grabs for pause, panic, unlock and Esc-double-tap.

    Every grabbed key is replayed with ``XAllowEvents(ReplayKeyboard)`` so the
    focused application still receives it.
    """

    def __init__(self, hud: "Hud") -> None:
        super().__init__(name="hud-key-grab", daemon=True)
        self.hud = hud
        self.stop_event = threading.Event()
        self.error: str | None = None
        self._x11 = None
        self._xtst = None
        self._display = None
        self._chords: dict[tuple[int, int], str] = {}
        self._last_press: dict[str, float] = {}

    def run(self) -> None:
        try:
            x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        except OSError as exc:
            self.error = f"libX11 unavailable: {exc}"
            return
        x11.XInitThreads()
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        display = x11.XOpenDisplay(os.environ.get("DISPLAY", ":0").encode())
        if not display:
            self.error = "cannot open display for key grabs"
            return
        self._x11, self._display = x11, display
        try:
            xtst = ctypes.CDLL(ctypes.util.find_library("Xtst") or "libXtst.so.6")
            xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
            xtst.XTestFakeButtonEvent.restype = ctypes.c_int
            self._xtst = xtst
        except OSError:
            self._xtst = None

        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        root = x11.XDefaultRootWindow(display)
        x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x11.XKeysymToKeycode.restype = ctypes.c_ubyte
        x11.XGrabKey.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int
        ]
        x11.XAllowEvents.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ulong]
        x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x11.XConnectionNumber.argtypes = [ctypes.c_void_p]
        x11.XConnectionNumber.restype = ctypes.c_int
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        x11.XPending.argtypes = [ctypes.c_void_p]
        x11.XPending.restype = ctypes.c_int

        errors: list[int] = []
        handler_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

        def on_error(_display, _event) -> int:
            errors.append(1)
            return 0

        handler = handler_type(on_error)
        x11.XSetErrorHandler.argtypes = [handler_type]
        x11.XSetErrorHandler.restype = ctypes.c_void_p
        x11.XSetErrorHandler(handler)

        escape = int(x11.XKeysymToKeycode(display, 0xFF1B))
        enter = int(x11.XKeysymToKeycode(display, 0xFF0D))
        letter_p = int(x11.XKeysymToKeycode(display, 0x0070))
        letter_k = int(x11.XKeysymToKeycode(display, 0x006B))
        letter_h = int(x11.XKeysymToKeycode(display, 0x0068))
        letter_r = int(x11.XKeysymToKeycode(display, 0x0072))
        # plain Escape for the double tap
        self._chords[(escape, 0)] = "esc"
        # chords: Ctrl+Alt+Shift + key, with/without CapsLock and NumLock
        chord_mods = ControlMask | Mod1Mask | ShiftMask
        for key, name in (
            (escape, "panic"),
            (enter, "unlock"),
            (letter_p, "pause"),
            (letter_k, "keyboard"),
            (letter_h, "hud"),
            (letter_r, "reset"),
        ):
            for extra in (0, LockMask, Mod2Mask, LockMask | Mod2Mask):
                self._chords[(key, chord_mods | extra)] = name

        for (keycode, modifiers) in self._chords:
            before = len(errors)
            x11.XGrabKey(display, keycode, modifiers, root, 0, GrabModeAsync, GrabModeAsync)
            x11.XSync(display, 0)
            if len(errors) != before:
                # A grab conflict (e.g. i3 owns that combination) — keep the rest.
                errors.clear()
        x11.XSync(display, 0)

        event = ctypes.create_string_buffer(192)
        fd = x11.XConnectionNumber(display)
        while not self.stop_event.is_set():
            ready, _, _ = select.select([fd], [], [], 0.25)
            if not ready:
                continue
            while x11.XPending(display):
                x11.XNextEvent(display, event)
                kind = int.from_bytes(event.raw[0:4], sys.byteorder)
                keycode = int.from_bytes(event.raw[84:88], sys.byteorder)
                state = int.from_bytes(event.raw[80:84], sys.byteorder)
                if os.environ.get("HUD_DEBUG"):
                    print(f"key event kind={kind} code={keycode} state={state:#x}", file=sys.stderr, flush=True)
                if kind not in (KeyPress, KeyRelease):
                    continue
                x11.XAllowEvents(display, ReplayKeyboard, 0)  # app still sees the key
                x11.XSync(display, 0)
                if kind != KeyPress:
                    continue
                chord = "esc" if keycode == escape and not (state & (ControlMask | Mod1Mask)) else None
                if chord is None:
                    for (key, modifiers), name in self._chords.items():
                        if name == "esc" or key != keycode:
                            continue
                        if (state & modifiers) == modifiers:
                            chord = name
                            break
                if chord is None:
                    continue
                now = time.monotonic()
                previous = self._last_press.get(chord, 0.0)
                if now - previous < 0.05:
                    continue  # key auto-repeat
                if os.environ.get("HUD_DEBUG"):
                    print(f"chord={chord} now={now:.3f} prev={previous:.3f}", file=sys.stderr, flush=True)
                self._last_press[chord] = now
                if chord == "esc":
                    if now - previous <= ESC_DOUBLE_S:
                        self._last_press[chord] = 0.0
                        GLib.idle_add(self.hud.on_esc_disarm)
                    continue
                GLib.idle_add(self.hud.on_chord, chord)

    def release_buttons(self) -> None:
        """Release primary buttons on our own connection (panic first frame)."""
        if not self._xtst or not self._display:
            return
        for number in (1, 2, 3):
            self._xtst.XTestFakeButtonEvent(self._display, number, 0, 0)
        self._x11.XFlush(self._display)


class Hud(Gtk.Window):
    def __init__(self, interactive: bool = False) -> None:
        super().__init__(title="Mouse Driver HUD")
        self.interactive = interactive
        self.state: dict | None = None
        self.target: dict | None = None
        self.guard = Guard()
        self._width = 430
        self._height = MIN_H
        self._shown = False
        self._caps: dict[str, tuple[Gtk.Label, Gtk.Label]] = {}
        self._compact = False
        self._key_labels: dict[str, list[Gtk.Label]] = {}
        self._hud_visible = True
        self._hud_mode = "auto"
        self._offset = (12, 12)
        self._realised = (0, 0)
        self._last_publish = 0.0
        self._dragging = False
        self._drag_offset = (0, 0)
        self._scale = 1.0
        self._interacting: str | None = None
        self._drag_start: tuple[float, float, int, int] | None = None
        self._drag_moved = False
        self._resize_start: tuple[float, int, int, int] | None = None
        self._last_resize_press = 0.0
        self._css_provider = None
        self._cursors: dict[str, object] = {}
        self._suppress_until = 0.0
        self._cursor_grab = None
        self._cursor_grabbing = None
        self._hot_until: dict[str, float] = {}
        self._keys_seen: list[dict] = []

        self.set_name("hud")
        self.set_default_size(self._width, self._height)
        self.set_resizable(True)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.stick()

        self._apply_css()
        self._build()
        self.connect("destroy", Gtk.main_quit)
        self.connect("size-allocate", lambda *_: self._click_through())
        # The pointer grab delivers motion/release to the toplevel, not the
        # EventBox, so handle them here as well while a drag is active.
        self.add_events(
            Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
        )
        self.connect("button-press-event", self._on_press)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("button-release-event", self._on_release)

        self.realize()
        gdk_window = self.get_window()
        if gdk_window is not None:
            gdk_window.set_override_redirect(True)
            gdk_window.set_keep_above(True)
        self.show_all()
        if not interactive:
            self.buttons_box.hide()
        self._click_through()
        self._place_fallback()

        threading.Thread(target=self._poll_loop, name="hud-poll", daemon=True).start()
        self.keygrab = KeyGrab(self)
        self.keygrab.start()
        GLib.timeout_add(1000, self._tick)
        GLib.timeout_add(1500, self._re_raise)
        GLib.timeout_add(40, self._update_highlights)
        GLib.timeout_add(500, self._refresh_shape)

    # -- look ------------------------------------------------------------
    def _apply_css(self) -> None:
        screen = Gdk.Screen.get_default()
        if screen is None:
            return
        if self._css_provider is not None:
            Gtk.StyleContext.remove_provider_for_screen(screen, self._css_provider)
        provider = Gtk.CssProvider()
        provider.load_from_data(css_for(self._scale).encode())
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self._css_provider = provider

    def _label(self, text: str = "", css: str = "", *, wrap: bool = False) -> Gtk.Label:
        label = Gtk.Label(label=text)
        for name in css.split():
            if name:
                label.get_style_context().add_class(name)
        label.set_xalign(0.0)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        if wrap:
            label.set_line_wrap(True)
            label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        return label

    def _keycap(self, caps: list[str] | str, description: str, key: str) -> Gtk.Box:
        """One legend entry: every chord key gets its own small keycap."""
        tokens = [caps] if isinstance(caps, str) else list(caps)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        cap_labels: list[Gtk.Label] = []
        for token in tokens:
            cap = self._label(token, "cap")
            cap.set_xalign(0.5)
            box.pack_start(cap, False, False, 0)
            cap_labels.append(cap)
        desc = self._label(description, "legend")
        box.pack_start(desc, False, False, 0)
        self._caps[key] = (cap_labels, desc)
        return box

    def _build(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        outer.set_border_width(8)
        self.add(outer)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        outer.pack_start(header, False, False, 0)

        self.drag_box = Gtk.EventBox()
        self.drag_box.set_visible_window(False)
        self.drag_box.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        grip_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.grip_label = self._label("⠿", "grip")
        self.grip_label.set_xalign(0.5)
        grip_row.pack_start(self.grip_label, False, False, 0)
        grip_row.pack_start(self._label("MOUSE DRIVER", "brand"), False, False, 0)
        self.drag_box.add(grip_row)
        self.drag_box.connect("enter-notify-event", self._on_grip_enter)
        self.drag_box.connect("leave-notify-event", lambda *_: self.get_window().set_cursor(None))
        # The whole header strip is the grab handle (up to the pill and X), so
        # the panel can be picked up anywhere along the title line.
        self.drag_box.set_size_request(-1, 24)
        header.pack_start(self.drag_box, True, True, 0)
        self.pill = self._label("IDLE", "pill pill-idle")
        self.pill.set_xalign(0.5)
        header.pack_start(self.pill, False, False, 0)

        self.pause_box = Gtk.EventBox()
        self.pause_box.set_visible_window(False)
        self.pause_box.set_size_request(22, 22)
        self.pause_label = self._label("⏸", "pause")
        self.pause_label.set_xalign(0.5)
        self.pause_label.set_yalign(0.5)
        self.pause_box.add(self.pause_label)
        self.pause_box.set_tooltip_text("Pause")
        header.pack_start(self.pause_box, False, False, 0)
        self._pause_enabled = False

        self.close_box = Gtk.EventBox()
        self.close_box.set_size_request(22, 22)
        self.close_box.set_visible_window(False)
        self.close_label = self._label("×", "close")
        self.close_label.set_xalign(0.5)
        self.close_label.set_yalign(0.5)
        self.close_label.set_size_request(18, 18)
        self.close_box.add(self.close_label)
        self.close_box.connect("enter-notify-event", lambda *_: self.close_label.get_style_context().add_class("close-hot"))
        self.close_box.connect("leave-notify-event", lambda *_: self.close_label.get_style_context().remove_class("close-hot"))
        header.pack_start(self.close_box, False, False, 0)

        self.target_label = self._label("no target", "target")
        self.target_label.set_max_width_chars(58)
        outer.pack_start(self.target_label, False, False, 0)

        live_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        outer.pack_start(live_row, False, False, 0)
        self.live = self._label("0 steps · 0px · 0/0 px/s · idle", "live")
        self.live.set_max_width_chars(46)
        live_row.pack_start(self.live, True, True, 0)
        self.canvas = Gtk.DrawingArea()
        self.canvas.set_size_request(120, 48)
        self.canvas.connect("draw", self._draw_path)
        live_row.pack_start(self.canvas, False, False, 0)

        self.legend = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        outer.pack_start(self.legend, False, False, 0)
        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.legend.pack_start(row1, False, False, 0)
        self.legend.pack_start(row2, False, False, 0)
        row1.pack_start(self._keycap(["P"], "pause / resume", "pause"), False, False, 0)
        row1.pack_start(Gtk.Box(), True, True, 0)
        row1.pack_start(self._keycap(["Esc", "Esc"], "disarm", "disarm"), False, False, 0)
        row2.pack_start(
            self._keycap(["Ctrl", "Alt", "Shift", "Esc"], "stop · lock", "stop"), False, False, 0
        )
        row2.pack_start(Gtk.Box(), True, True, 0)
        row2.pack_start(
            self._keycap(["Ctrl", "Alt", "Shift", "Enter"], "unlock", "unlock"), False, False, 0
        )

        # -- overlay keyboard -------------------------------------------
        recent_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        outer.pack_start(recent_row, False, False, 0)
        recent_row.pack_start(self._label("RECENT KEYS", "recent"), False, False, 0)
        self.recent_caps = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        recent_row.pack_start(self.recent_caps, True, True, 0)

        self.keyboard_grid = Gtk.Grid(row_spacing=3, column_spacing=2)
        self.keyboard_grid.set_border_width(4)
        outer.pack_start(self.keyboard_grid, False, False, 0)
        self._build_keyboard()
        self.keyboard_grid.show_all()
        self.keyboard_grid.hide()

        self.compact_legend = self._label("", "legend", wrap=True)
        self.compact_legend.set_max_width_chars(70)
        self.compact_legend.set_no_show_all(True)
        outer.pack_start(self.compact_legend, False, False, 0)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        outer.pack_start(footer, False, False, 0)
        self.hint = self._label("", "hint")
        self.hint.set_size_request(230, -1)
        self.hint.set_max_width_chars(38)
        footer.pack_start(self.hint, True, True, 0)
        self.mode_label = self._label("follow · auto", "mode")
        footer.pack_start(self.mode_label, False, False, 0)
        self.buttons_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        footer.pack_start(self.buttons_box, False, False, 0)
        self.disarm_btn = Gtk.Button(label="Disarm")
        self.disarm_btn.connect("clicked", lambda *_: self._disarm())
        self.buttons_box.pack_start(self.disarm_btn, False, False, 0)
        self.stop_btn = Gtk.Button(label="STOP")
        self.stop_btn.get_style_context().add_class("stop")
        self.stop_btn.connect("clicked", lambda *_: self._stop_now())
        self.buttons_box.pack_start(self.stop_btn, False, False, 0)
        self.buttons_box.show_all()

        self.resize_box = Gtk.EventBox()
        self.resize_box.set_visible_window(False)
        self.resize_box.set_size_request(18, 18)
        self.resize_label = self._label("⤡", "grip")
        self.resize_label.set_xalign(0.5)
        self.resize_label.set_yalign(0.5)
        self.resize_box.add(self.resize_label)
        footer.pack_start(self.resize_box, False, False, 0)

    def _build_keyboard(self) -> None:
        layout = [
            [("Esc", 1), ("1", 1), ("2", 1), ("3", 1), ("4", 1), ("5", 1), ("6", 1), ("7", 1),
             ("8", 1), ("9", 1), ("0", 1), ("-", 1), ("=", 1), ("Backspace", 2)],
            [("Tab", 1.5), ("Q", 1), ("W", 1), ("E", 1), ("R", 1), ("T", 1), ("Y", 1), ("U", 1),
             ("I", 1), ("O", 1), ("P", 1), ("[", 1), ("]", 1), ("\\", 1)],
            [("Caps", 1.75), ("A", 1), ("S", 1), ("D", 1), ("F", 1), ("G", 1), ("H", 1), ("J", 1),
             ("K", 1), ("L", 1), (";", 1), ("'", 1), ("Enter", 2)],
            [("Shift", 2), ("Z", 1), ("X", 1), ("C", 1), ("V", 1), ("B", 1), ("N", 1), ("M", 1),
             (",", 1), (".", 1), ("/", 1), ("Shift", 2)],
            [("Ctrl", 1.5), ("Alt", 1.5), ("Space", 5), ("Alt", 1.5), ("Fn", 1),
             ("←", 1), ("↑", 1), ("↓", 1), ("→", 1)],
        ]
        self._key_specs = []
        for row_index, row in enumerate(layout):
            column = 0
            for name, units in row:
                label = self._label(name, "key key-mod")
                label.set_xalign(0.5)
                label.set_size_request(int(KEY_UNIT * units) + int(2 * (units - 1)), KEY_H)
                self.keyboard_grid.attach(label, column, row_index, max(1, int(units)), 1)
                column += max(1, int(units))
                self._key_labels.setdefault(name, []).append(label)
                self._key_specs.append((label, max(1, int(units))))

    # -- placement -------------------------------------------------------
    def _widget_rect(self, widget) -> tuple[int, int, int, int] | None:
        allocation = widget.get_allocation()
        if allocation.width <= 0 or allocation.height <= 0:
            return None
        try:
            coords = widget.translate_coordinates(self, 0, 0)
        except (TypeError, AttributeError):
            return None
        if not coords or len(coords) < 2:
            return None
        return int(coords[0]), int(coords[1]), int(allocation.width), int(allocation.height)

    def _x_rect(self):
        return self._widget_rect(self.close_box) if hasattr(self, "close_box") else None

    def _drag_rect(self):
        return self._widget_rect(self.drag_box) if hasattr(self, "drag_box") else None

    def _resize_rect(self):
        return self._widget_rect(self.resize_box) if hasattr(self, "resize_box") else None

    def _pause_rect(self):
        return self._widget_rect(self.pause_box) if hasattr(self, "pause_box") else None

    @staticmethod
    def _inside(rect, x: float, y: float) -> bool:
        if not rect:
            return False
        rx, ry, rw, rh = rect
        return rx <= x < rx + rw and ry <= y < ry + rh

    def _cursor(self, name: str):
        if name not in self._cursors:
            try:
                self._cursors[name] = Gdk.Cursor.new_from_name(Gdk.Display.get_default(), name)
            except (TypeError, AttributeError):
                self._cursors[name] = None
        return self._cursors[name]

    def _click_through(self) -> None:
        """Three input regions: drag strip, X, resize grip. Everything else passes through."""
        if self.interactive:
            return
        gdk_window = self.get_window()
        if gdk_window is None:
            return
        if self._interacting:
            self._set_full_input_shape()
            return
        rects = [rect for rect in (self._drag_rect(), self._x_rect(), self._resize_rect()) if rect]
        if self._pause_enabled:
            rects.append(self._pause_rect())
        try:
            region = cairo.Region()
            for x, y, width, height in rects:
                region.union(cairo.RectangleInt(x, y, width, height))
            gdk_window.input_shape_combine_region(region, 0, 0)
        except (TypeError, AttributeError):
            pass

    def _set_full_input_shape(self) -> None:
        gdk_window = self.get_window()
        if gdk_window is None:
            return
        try:
            gdk_window.input_shape_combine_region(
                cairo.Region(cairo.RectangleInt(0, 0, self._width, self._height)), 0, 0
            )
        except (TypeError, AttributeError):
            pass

    def _grab_pointer(self, event, cursor_name: str) -> None:
        try:
            Gdk.Display.get_default().get_default_seat().grab(
                self.get_window(), Gdk.SeatCapabilities.ALL, False, self._cursor(cursor_name), event, event.time
            )
        except (TypeError, AttributeError):
            pass

    @staticmethod
    def _ungrab_pointer() -> None:
        try:
            Gdk.Display.get_default().get_default_seat().ungrab()
        except (TypeError, AttributeError):
            pass

    def _position(self) -> tuple[int, int]:
        position = self.get_position()
        if position and len(position) >= 2:
            return int(position[0]), int(position[1])
        return 0, 0

    # -- pointer: press / motion / release -------------------------------
    def _on_press(self, _widget, event) -> bool:
        if os.environ.get("HUD_DEBUG"):
            print(f"press at {event.x},{event.y} root {event.x_root},{event.y_root} rects x={self._x_rect()} drag={self._drag_rect()} resize={self._resize_rect()}", file=sys.stderr, flush=True)
        if event.button != 1:
            return False
        wx, wy = self._position()
        x, y = float(event.x_root) - wx, float(event.y_root) - wy
        # Explicit controls first; the drag strip never wins over them.
        if self._pause_enabled and self._inside(self._pause_rect(), x, y):
            self._toggle_pause_button()
            return True
        if self._inside(self._x_rect(), x, y):
            self._close_panel()
            return True
        if self._inside(self._resize_rect(), x, y):
            now = time.monotonic()
            if now - self._last_resize_press < 0.30:
                self._last_resize_press = 0.0
                self._apply_scale(1.0, persist=True)
                return True
            self._last_resize_press = now
            wx, wy = self._position()
            self._resize_start = (float(event.x_root), int(self._width), wx, wy)
            self._interacting = "resize"
            self._set_full_input_shape()
            self._grab_pointer(event, "nwse-resize")
            return True
        if self._inside(self._drag_rect(), x, y):
            wx, wy = self._position()
            self._drag_start = (float(event.x_root), float(event.y_root), wx, wy)
            self._drag_moved = False
            self._interacting = "drag"
            self._set_full_input_shape()
            self._grab_pointer(event, "grabbing")
            return True
        return False

    def _on_motion(self, _widget, event) -> bool:
        if self._interacting == "drag" and self._drag_start:
            dx = float(event.x_root) - self._drag_start[0]
            dy = float(event.y_root) - self._drag_start[1]
            if not self._drag_moved and abs(dx) + abs(dy) <= 3.0:
                return True  # click jitter, not a drag yet
            self._drag_moved = True
            screen = Gdk.Screen.get_default()
            width = screen.get_width() if screen is not None else 5120
            height = screen.get_height() if screen is not None else 1440
            x = max(0, min(int(self._drag_start[2] + dx), width - self._width))
            y = max(0, min(int(self._drag_start[3] + dy), height - self._height))
            self.move(x, y)
            return True
        if self._interacting == "resize" and self._resize_start:
            delta = float(event.x_root) - self._resize_start[0]
            self._apply_scale((self._resize_start[1] + delta) / self._base_width(), persist=False)
            return True
        # Hover cursor, only over the three interactive regions.
        gdk_window = self.get_window()
        if gdk_window is not None:
            wx, wy = self._position()
            hx, hy = float(event.x_root) - wx, float(event.y_root) - wy
            if self._inside(self._resize_rect(), hx, hy):
                gdk_window.set_cursor(self._cursor("nwse-resize"))
            elif self._inside(self._drag_rect(), hx, hy):
                gdk_window.set_cursor(self._cursor("grab"))
            else:
                gdk_window.set_cursor(None)
        return False

    def _on_release(self, _widget, event) -> bool:
        if not self._interacting:
            return False
        mode = self._interacting
        self._interacting = None
        self._ungrab_pointer()
        self._click_through()
        if mode == "drag" and self._drag_moved:
            x, y = self._position()
            target = self.target or {}
            if target:
                self._offset = hud_layout.offset_for_position(
                    (int(target["x"]), int(target["y"]), int(target["w"]), int(target["h"])),
                    (self._width, self._height),
                    x,
                    y,
                )
            self._hud_mode = "custom"
            self._set_mode_label("custom")
            payload = {
                "offset_x": int(self._offset[0]),
                "offset_y": int(self._offset[1]),
                "mode": "custom",
                "x": x,
                "y": y,
            }
            threading.Thread(target=lambda: post_json("/api/hud/offset", payload), daemon=True).start()
        elif mode == "resize":
            self._place_current()
            threading.Thread(
                target=lambda: post_json("/api/hud/scale", {"scale": self._scale}), daemon=True
            ).start()
        self._click_through()
        return True

    def _refresh_shape(self) -> bool:
        self._click_through()
        return True

    def _apply_pause_button(self, state: dict) -> None:
        armed = bool(state.get("armed"))
        paused = bool(state.get("paused"))
        locked = bool(state.get("locked"))
        info = hud_layout.pause_button(armed, paused, locked)
        self._pause_enabled = bool(info["enabled"])
        self.pause_label.set_text(info["glyph"])
        context = self.pause_label.get_style_context()
        for name in ("pause-active", "pause-off", "pause"):
            context.remove_class(name)
        context.add_class("pause")
        if info["tone"] == "active":
            context.add_class("pause-active")
        elif info["tone"] == "off":
            context.add_class("pause-off")
        self.pause_box.set_tooltip_text(info["tooltip"])

    def _toggle_pause_button(self) -> None:
        paused = bool((self.state or {}).get("paused"))
        path = "/api/resume" if paused else "/api/pause"

        def work() -> None:
            post(path)
            GLib.idle_add(self._hint, "resumed" if paused else "paused")

        threading.Thread(target=work, daemon=True).start()

    def _set_mode_label(self, mode: str) -> None:
        if hasattr(self, "mode_label"):
            self.mode_label.set_text(f"follow · {mode}")

    def _close_panel(self) -> None:
        if os.environ.get("HUD_DEBUG"):
            print("close panel", file=sys.stderr, flush=True)
        # Single writer: the driver owns data/hud.json, so only POST the intent.
        self._hide_panel()
        self._hud_visible = False
        self._suppress_until = time.time() + 2.0
        threading.Thread(target=lambda: post_json("/api/hud/visible", {"visible": False}), daemon=True).start()

    def _on_grip_enter(self, *_args) -> bool:
        gdk_window = self.get_window()
        if gdk_window is not None:
            gdk_window.set_cursor(self._cursor("grab"))
        return False

    def _toggle_hud(self) -> None:
        def work() -> None:
            if post_json("/api/hud/toggle", {}):
                GLib.idle_add(self._hint, "hud toggled")
            else:
                GLib.idle_add(self._hint, "hud toggle refused (api offline)")

        threading.Thread(target=work, daemon=True).start()

    def _reset_hud(self) -> None:
        def work() -> None:
            if post_json("/api/hud/reset", {}):
                GLib.idle_add(self._hint, "hud re-anchored (auto)")
            else:
                GLib.idle_add(self._hint, "reset refused (api offline)")

        threading.Thread(target=work, daemon=True).start()

    def _expanded(self) -> bool:
        return bool((self.state or {}).get("keyboard_expanded"))

    # -- size / scale ----------------------------------------------------
    def _base_width(self) -> int:
        return EXPANDED_W if self._expanded() else COLLAPSED_W

    def _scaled_width(self, target_width: int | None = None) -> int:
        width = int(round(self._base_width() * self._scale))
        width = max(366, min(774, width))
        if target_width:
            width = min(width, max(240, int(target_width) - 2 * MARGIN))
        return width

    def _fit(self, target_width: int | None = None) -> None:
        width = self._scaled_width(target_width)
        child = self.get_child()
        _, natural_h = child.get_preferred_height() if child is not None else (0, MIN_H)
        height = max(MIN_H, int(natural_h))
        if width == self._width and height == self._height:
            return
        self._width, self._height = width, height
        hints = Gdk.Geometry()
        hints.min_width = hints.max_width = width
        hints.min_height = hints.max_height = height
        self.set_geometry_hints(None, hints, Gdk.WindowHints.MIN_SIZE | Gdk.WindowHints.MAX_SIZE)
        self.set_size_request(width, height)
        self.resize(width, height)
        self._click_through()
        self._legend_mode(width)

    def _scale_keyboard(self) -> None:
        for label, units in getattr(self, "_key_specs", []):
            label.set_size_request(
                int(KEY_UNIT * self._scale * units + 2 * (units - 1)),
                int(KEY_H * self._scale),
            )

    def _apply_scale(self, scale: float, *, persist: bool = False) -> None:
        scale = max(0.85, min(1.8, float(scale)))
        if abs(scale - self._scale) < 0.0005 and not persist:
            return
        self._scale = scale
        self._apply_css()
        if hasattr(self, "canvas"):
            self.canvas.set_size_request(int(120 * scale), int(48 * scale))
        self._scale_keyboard()
        target_width = int((self.target or {}).get("w") or 0)
        self._fit(target_width)
        if not self._interacting:
            self._place_current()
        if persist:
            threading.Thread(
                target=lambda: post_json("/api/hud/scale", {"scale": scale}), daemon=True
            ).start()

    def _legend_mode(self, width: int) -> None:
        compact = width < 400 * self._scale
        if compact == self._compact:
            return
        self._compact = compact
        if compact:
            self.legend.hide()
            self.compact_legend.show()
        else:
            self.compact_legend.hide()
            self.legend.show_all()

    # -- placement -------------------------------------------------------
    def _screen_size(self) -> tuple[int, int]:
        screen = Gdk.Screen.get_default()
        if screen is None:
            return 5120, 1440
        return screen.get_width(), screen.get_height()

    def _place_current(self) -> None:
        if self._interacting:
            return
        if self._hud_mode == "manual":
            self._place_manual()
        elif self.target:
            self._place_on_target(self.target)
        else:
            self._place_fallback()

    def _place_fallback(self) -> None:
        display = Gdk.Display.get_default()
        geometry = None
        if display is not None:
            monitor = display.get_primary_monitor() or display.get_monitor(0)
            if monitor is not None:
                geometry = monitor.get_geometry()
        self._fit()
        if geometry is None:
            return
        x = geometry.x + geometry.width - self._width - MARGIN
        y = geometry.y + geometry.height - self._height - MARGIN
        self.move(max(0, x), max(0, y))
        if not self._shown:
            self.show()
            self._shown = True
        self._click_through()

    def _place_on_target(self, target: dict) -> None:
        tx, ty = int(target["x"]), int(target["y"])
        tw, th = int(target["w"]), int(target["h"])
        self._fit(tw)
        x, y = hud_layout.panel_position((tx, ty, tw, th), (self._width, self._height), self._offset)
        self._realised = (x, y) if not self._shown else self._realised
        screen_w, screen_h = self._screen_size()
        x = max(0, min(x, screen_w - self._width))
        y = max(0, min(y, screen_h - self._height))
        self.move(x, y)
        if not self._shown:
            self.show()
            self._shown = True
        self._click_through()
        if self.interactive:
            self.buttons_box.show_all()
        else:
            self.buttons_box.hide()
        self._publish_realised()

    def _publish_realised(self) -> None:
        if self._interacting or not self._shown:
            return
        now = time.monotonic()
        if now - self._last_publish < 1.0:
            return
        x, y = self._position()
        if abs(x - self._realised[0]) <= 2 and abs(y - self._realised[1]) <= 2:
            return
        self._realised = (x, y)
        self._last_publish = now
        payload = {
            "offset_x": int(self._offset[0]),
            "offset_y": int(self._offset[1]),
            "mode": self._hud_mode,
            "x": x,
            "y": y,
        }
        threading.Thread(target=lambda: post_json("/api/hud/offset", payload), daemon=True).start()

    def _hide_panel(self) -> None:
        if self._shown:
            self.hide()
            self._shown = False

    def _re_raise(self) -> bool:
        if self.target and self.target.get("active") and self._shown:
            gdk_window = self.get_window()
            if gdk_window is not None:
                gdk_window.raise_()
        return True

    # -- data ------------------------------------------------------------
    def _poll_loop(self) -> None:
        while True:
            state = fetch_state()
            target = (state or {}).get("target") if state else None
            geometry = None
            if isinstance(target, dict) and target.get("id"):
                geometry = read_geometry(int(target["id"]))
            GLib.idle_add(self._apply_state, state, target, geometry)
            # 8 Hz while visible, 2 Hz while hidden so the chord can bring it back.
            time.sleep(POLL_S if self._hud_visible else 0.5)

    def _apply_state(self, state: dict | None, target: dict | None, geometry: dict | None) -> bool:
        self.state = state
        self.target = target if isinstance(target, dict) else None

        if state is None:
            self._pill("OFFLINE", "pill-offline")
            self.target_label.set_text("driver offline")
            self.live.set_text("telemetry 127.0.0.1:%d unreachable" % API_PORT)
            self._emphasise(None)
            self._place_fallback()
            self.canvas.queue_draw()
            return False

        locked = bool(state.get("locked"))
        paused = bool(state.get("paused"))
        armed = bool(state.get("armed"))
        hud = state.get("hud") or {}
        self._hud_visible = bool(hud.get("visible", state.get("hud_visible", True)))
        self._hud_mode = "custom" if hud.get("mode") == "custom" else "auto"
        self._offset = (int(hud.get("offset_x") or 12), int(hud.get("offset_y") or 12))
        self._set_mode_label(self._hud_mode)
        try:
            state_scale = max(0.85, min(1.8, float(hud.get("scale") or 1.0)))
        except (TypeError, ValueError):
            state_scale = 1.0
        if abs(state_scale - self._scale) > 0.001 and not self._interacting:
            self._apply_scale(state_scale)
        if self._hud_visible and time.time() < self._suppress_until:
            self._hud_visible = False
        if not self._hud_visible:
            self._hide_panel()
            self._apply_pause_button(state)
            ttl = self._ttl_text(state)
            if locked:
                self._pill("LOCKED", "pill-locked")
            elif paused:
                self._pill(f"PAUSED {ttl}", "pill-paused")
            elif armed:
                self._pill(f"ARMED {ttl}", "pill-armed")
            else:
                self._pill("IDLE", "pill-idle")
            return False

        self._apply_pause_button(state)
        ttl = self._ttl_text(state)
        if locked:
            self._pill("LOCKED", "pill-locked")
        elif paused:
            self._pill(f"PAUSED {ttl}", "pill-paused")
        elif armed:
            self._pill(f"ARMED {ttl}", "pill-armed")
        else:
            self._pill("IDLE", "pill-idle")

        session = state.get("session") or {}
        action = session.get("last_action") or "idle"
        target_text = session.get("last_target")
        steps = int(session.get("steps") or 0)
        distance = float(session.get("distance_px") or 0)
        avg = float(session.get("avg_speed") or 0)
        peak = float(session.get("max_speed") or 0)
        lead = f"{action} {target_text}" if target_text else action
        self.live.set_text(f"{lead} · {steps} steps · {distance:.0f}px · {avg:.0f}/{peak:.0f} px/s")
        self._emphasise("locked" if locked else "paused" if paused else "armed" if armed else "idle")
        self._render_keyboard(state)

        if self.target is None:
            self.target_label.set_text("no target")
            if not self._interacting:
                self._place_fallback()
            self.canvas.queue_draw()
            return False

        app = (self.target.get("cls") or "").strip()
        title = (self.target.get("title") or "").strip() or "untitled"
        self.target_label.set_text(f"{app} — {title}" if app else title)

        if self._interacting:
            self.canvas.queue_draw()
            return False

        merged = dict(self.target)
        if geometry:
            for key in ("x", "y", "w", "h"):
                merged[key] = geometry[key]
            merged["mapped"] = geometry["mapped"]
        visible = bool(merged.get("visible", True)) and bool(merged.get("mapped", True))
        if not visible or int(merged.get("w", 0)) < 80 or int(merged.get("h", 0)) < 60:
            self._hide_panel()
        else:
            self._place_on_target(merged)
        self.canvas.queue_draw()
        return False

    # -- keyboard --------------------------------------------------------
    @staticmethod
    def _recent_groups(keys: list[dict]) -> list[tuple[str, int]]:
        groups: list[tuple[str, int]] = []
        for event in reversed(keys):
            name = str(event.get("key") or "")
            if not name:
                continue
            if groups and groups[-1][0] == name:
                groups[-1] = (name, groups[-1][1] + 1)
            else:
                groups.append((name, 1))
        return groups[:6]

    def _render_keyboard(self, state: dict) -> None:
        keys = state.get("keys_recent") or []
        self._keys_seen = keys
        for child in self.recent_caps.get_children():
            self.recent_caps.remove(child)
        groups = self._recent_groups(keys)
        if not groups:
            empty = self._label("none yet", "hint")
            self.recent_caps.pack_start(empty, False, False, 0)
        else:
            for name, count in groups:
                text = name if count == 1 else f"{name} ×{count}"
                cap = self._label(text, "cap")
                cap.set_xalign(0.5)
                cap.set_max_width_chars(18)
                self.recent_caps.pack_start(cap, False, False, 0)
        self.recent_caps.show_all()

        expanded = bool(state.get("keyboard_expanded"))
        if expanded:
            self.keyboard_grid.show_all()
        else:
            self.keyboard_grid.hide()

        now = time.time()
        for event in keys:
            if now - float(event.get("t") or 0) > 0.25:
                continue
            for part in str(event.get("key") or "").split("+"):
                name = part.strip()
                if name in self._key_labels:
                    self._hot_until[name] = now + 0.14
        self._update_highlights()

    def _update_highlights(self) -> bool:
        now = time.time()
        for name, labels in self._key_labels.items():
            hot = self._hot_until.get(name, 0.0) > now
            if not hot:
                self._hot_until.pop(name, None)
            for label in labels:
                context = label.get_style_context()
                if hot:
                    context.add_class("key-hot")
                else:
                    context.remove_class("key-hot")
        return True

    def _toggle_keyboard(self) -> None:
        def work() -> None:
            if not post("/api/keyboard/toggle"):
                try:
                    path = Path(os.environ.get("MOUSE_DRIVER_DATA", Path(__file__).resolve().parent.parent / "data")) / "keyboard.json"
                    current = False
                    try:
                        current = bool(json.loads(path.read_text()).get("expanded"))
                    except (FileNotFoundError, ValueError, OSError):
                        pass
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({"expanded": not current}) + chr(10))
                except OSError:
                    pass
            GLib.idle_add(self._hint, "keyboard toggled")

        threading.Thread(target=work, daemon=True).start()

    def _pill(self, text: str, css_class: str) -> None:
        self.pill.set_text(text)
        for name in ("pill-armed", "pill-idle", "pill-paused", "pill-locked", "pill-offline"):
            self.pill.get_style_context().remove_class(name)
        self.pill.get_style_context().add_class(css_class)

    def _emphasise(self, mode: str | None) -> None:
        hot: set[str] = set()
        dim: set[str] = set()
        if mode == "armed":
            hot = {"pause", "stop"}
        elif mode == "paused":
            hot = {"pause"}
            dim = {"disarm", "stop", "unlock"}
        elif mode == "locked":
            hot = {"unlock"}
            dim = {"pause", "disarm", "stop"}
        for key, (caps, desc) in self._caps.items():
            for cap in caps:
                context = cap.get_style_context()
                for name in ("cap-hot", "cap-dim"):
                    context.remove_class(name)
                if key in hot:
                    context.add_class("cap-hot")
                elif key in dim:
                    context.add_class("cap-dim")
            desc_context = desc.get_style_context()
            for name in ("legend", "legend-dim"):
                desc_context.remove_class(name)
            desc_context.add_class("legend-dim" if key in dim else "legend")

        if mode == "locked":
            self.compact_legend.set_markup(
                '<span foreground="#ffffff" background="#d94a3d"> Ctrl Alt Shift Enter </span>'
                ' <span foreground="#93a38e">unlock</span>   '
                '<span foreground="#4f5c4b">Ctrl Alt Shift Esc stop · lock</span>'
            )
        elif mode == "paused":
            self.compact_legend.set_markup(
                '<span foreground="#111314" background="#c6f24e"> P </span>'
                ' <span foreground="#93a38e">pause / resume</span>   '
                '<span foreground="#4f5c4b">Esc Esc disarm · Ctrl Alt Shift Esc stop · lock</span>'
            )
        else:
            self.compact_legend.set_markup(
                '<span foreground="#c6f24e">P</span> <span foreground="#93a38e">pause / resume</span>   '
                '<span foreground="#c6f24e">Esc Esc</span> <span foreground="#93a38e">disarm</span>   '
                '<span foreground="#c6a04e">Ctrl Alt Shift Esc</span> '
                '<span foreground="#93a38e">stop · lock</span>   '
                '<span foreground="#93a38e">Ctrl Alt Shift Enter unlock</span>'
            )

    @staticmethod
    def _ttl_text(state: dict) -> str:
        iso = state.get("arm_expires")
        if not iso:
            return "0:00"
        try:
            remaining = int(max(0.0, datetime.fromisoformat(iso).timestamp() - time.time()))
        except ValueError:
            return "--"
        return f"{remaining // 60}:{remaining % 60:02d}"

    def _tick(self) -> bool:
        if self.state and not self.state.get("locked"):
            ttl = self._ttl_text(self.state)
            if self.state.get("paused"):
                self.pill.set_text(f"PAUSED {ttl}")
            elif self.state.get("armed"):
                self.pill.set_text(f"ARMED {ttl}")
        return True

    # -- canvas ----------------------------------------------------------
    def _draw_path(self, widget: Gtk.Widget, cr) -> bool:
        allocation = widget.get_allocation()
        width, height = allocation.width, allocation.height
        cr.set_source_rgb(0x17 / 255, 0x1B / 255, 0x18 / 255)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        cr.set_source_rgb(0x24 / 255, 0x2C / 255, 0x25 / 255)
        cr.rectangle(0.5, 0.5, max(0, width - 1), max(0, height - 1))
        cr.set_line_width(1)
        cr.stroke()

        points = (self.state or {}).get("recent") or []
        if len(points) < 2:
            cr.set_source_rgb(0x4A / 255, 0x56 / 255, 0x47 / 255)
            cr.select_font_face("DejaVu Sans", 0, 0)
            cr.set_font_size(8)
            cr.move_to(6, height / 2 + 3)
            cr.show_text("no motion" if self.state is not None else "offline")
            return False

        xs = [float(p[0]) for p in points]
        ys = [float(p[1]) for p in points]
        min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
        span_x = max(40.0, max_x - min_x)
        span_y = max(20.0, max_y - min_y)
        pad = 6.0
        scale = min((width - 2 * pad) / span_x, (height - 2 * pad) / span_y)

        def map_point(x: float, y: float) -> tuple[float, float]:
            cx = pad + (x - min_x) * scale + ((width - 2 * pad) - span_x * scale) / 2
            cy = pad + (y - min_y) * scale + ((height - 2 * pad) - span_y * scale) / 2
            return cx, height - cy

        cr.set_line_width(1.4)
        cr.set_source_rgb(0xC6 / 255, 0xF2 / 255, 0x4E / 255)
        for index, point in enumerate(points):
            x, y = map_point(float(point[0]), float(point[1]))
            if index == 0:
                cr.move_to(x, y)
            else:
                cr.line_to(x, y)
        cr.stroke()
        x, y = map_point(xs[-1], ys[-1])
        cr.arc(x, y, 2.2, 0, 6.2832)
        cr.set_source_rgb(0xEA / 255, 0xF3 / 255, 0xE4 / 255)
        cr.fill()
        return False

    # -- actions ---------------------------------------------------------
    def _hint(self, text: str) -> None:
        self.hint.set_text(text[:38])

    def on_esc_disarm(self) -> bool:
        if self.state and self.state.get("locked"):
            self._hint("locked — Ctrl Alt Shift Enter")
            return False
        threading.Thread(target=self._disarm, daemon=True).start()
        return False

    def on_chord(self, chord: str) -> bool:
        if chord == "panic":
            self._panic()
        elif chord == "unlock":
            self._unlock()
        elif chord == "pause":
            self._toggle_pause()
        elif chord == "keyboard":
            self._toggle_keyboard()
        elif chord == "hud":
            self._toggle_hud()
        elif chord == "reset":
            self._reset_hud()
        return False

    def _panic(self) -> None:
        STOP_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            STOP_PATH.write_text(
                f"{datetime.now().isoformat(timespec='seconds')} panic chord\n", encoding="utf-8"
            )
        except OSError:
            pass
        if hasattr(self, "keygrab"):
            self.keygrab.release_buttons()
        try:
            self.guard.panic("Ctrl+Alt+Shift+Esc")
        except Exception as exc:  # never hide a safety failure
            print(f"hud: panic failed: {exc!r}", file=sys.stderr, flush=True)
        self._hint("locked — Ctrl Alt Shift Enter")

        def work() -> None:
            post("/api/panic", timeout=1.5)

        threading.Thread(target=work, daemon=True).start()

    def _unlock(self) -> None:
        try:
            self.guard.unlock()
        except Exception:
            pass
        self._hint("unlocked — arm again to drive")

        def work() -> None:
            post("/api/unlock")

        threading.Thread(target=work, daemon=True).start()

    def _toggle_pause(self) -> None:
        paused = bool((self.state or {}).get("paused"))
        try:
            if paused:
                self.guard.resume()
                self._hint("resumed")
            else:
                self.guard.pause(by="operator chord")
                self._hint("paused — Ctrl Alt Shift P")
        except Exception as exc:
            self._hint(str(exc)[:34])

        def work() -> None:
            post("/api/resume" if paused else "/api/pause")

        threading.Thread(target=work, daemon=True).start()

    def _disarm(self) -> None:
        self._hint("disarming…")
        ok = post("/api/disarm")
        if not ok:
            try:
                self.guard.disarm("hud")
            except Exception:
                pass
        GLib.idle_add(self._hint, "disarmed" if ok else "disarmed (api offline)")

    def _stop_now(self) -> None:
        STOP_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            STOP_PATH.write_text(
                f"{datetime.now().isoformat(timespec='seconds')} hud STOP button\n", encoding="utf-8"
            )
        except OSError:
            pass
        self._hint("STOP — re-arm to resume")

        def work() -> None:
            post("/api/stop")
            GLib.idle_add(self._hint, "stopped — re-arm to resume")

        threading.Thread(target=work, daemon=True).start()


def main() -> int:
    if not os.environ.get("DISPLAY"):
        print("hud: DISPLAY is not set (expected :0)", file=sys.stderr)
        return 2
    interactive = "--interactive" in sys.argv[1:]
    Hud(interactive=interactive)
    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
