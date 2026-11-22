"""Stdio MCP server exposing Mouse Driver to agents.

Motion is human-shaped (Fitts-law duration, curved minimum-jerk path, tremor,
occasional overshoot). Nothing moves, clicks or types unless the session was
armed with ``mouse_arm``; ``mouse_stop`` halts instantly. A small always-on-top
HUD on the desktop shows ARMED/IDLE, the countdown, steps, distance, speed, the
last two seconds of path and a STOP button.

Each tool returns compact JSON. Errors are JSON objects with ``ok: false`` and a
stable ``code`` rather than protocol errors, so an agent can react to them.
"""
from __future__ import annotations

import json
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .control import get_service
from .guard import GuardError, install_signal_handlers
from .x11 import X11Unavailable

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
ACT = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)

INSTRUCTIONS = (
    "Human-shaped control of the real X11 pointer on this desktop (the desktop host, two 2560x1440 monitors). "
    "Start with mouse_status. Every pointer or key action is refused unless mouse_arm was called and the "
    "TTL has not expired. mouse_stop halts motion immediately and leaves a STOP sentinel that only "
    "mouse_arm clears. The HUD stays on the window being controlled and shows ARMED/PAUSED/LOCKED/IDLE, "
    "the countdown, the current target, steps, distance, average and maximum speed, the last two seconds "
    "of path, a pause button, a recent-keys row and its shortcut legend. Operators can also "
    "touch data/STOP to stop motion instantly."
)

mcp = MCPServer(
    name="mouse-driver",
    title="Mouse Driver",
    version="1.0.0",
    instructions=INSTRUCTIONS,
)


def _json(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"))


def _guard_call(action: str, fn, *args, **kwargs) -> str:
    try:
        return _json(fn(*args, **kwargs))
    except GuardError as exc:
        return _json(exc.as_dict())
    except ValueError as exc:
        return _json({"ok": False, "error": str(exc), "code": "bad_argument"})
    except X11Unavailable as exc:
        return _json({"ok": False, "error": str(exc), "code": "x11_unavailable"})


def _svc():
    return get_service()


# -- session ---------------------------------------------------------------
@mcp.tool(
    name="mouse_status",
    description=(
        "Read the arming state, TTL remaining, cursor position, screen geometry, measurement session "
        "totals and the HUD/API URL. Read-only, no motion."
    ),
    annotations=READ,
)
def mouse_status() -> str:
    return _json(_svc().status())


@mcp.tool(
    name="mouse_arm",
    description=(
        "Arm the driver for ttl_s seconds (default 300, maximum 900). Arms are time-boxed on purpose: "
        "every move, click, drag, scroll, typed character and key refuses while disarmed. Arming also "
        "clears the STOP sentinel. The HUD pill turns ARMED and counts down."
    ),
    annotations=ACT,
)
def mouse_arm(ttl_s: int = 300) -> str:
    return _guard_call("arm", _svc().arm, ttl_s)


@mcp.tool(
    name="mouse_disarm",
    description=(
        "Immediately disarm. In-flight motion aborts within about 8 ms and further pointer/key actions "
        "are refused until mouse_arm is called again. Does not write the STOP sentinel."
    ),
    annotations=ACT,
)
def mouse_disarm() -> str:
    return _json(_svc().disarm())


@mcp.tool(
    name="mouse_stop",
    description=(
        "Emergency stop: writes data/STOP (checked on every motion sample), halts any motion and "
        "disarms. Use after anything unexpected. Only mouse_arm clears the sentinel."
    ),
    annotations=ACT,
)
def mouse_stop() -> str:
    return _json(_svc().stop())


@mcp.tool(
    name="mouse_where",
    description="Return the current pointer position, virtual screen size and monitor count. Read-only.",
    annotations=READ,
)
def mouse_where() -> str:
    return _guard_call("where", _svc().where)


# -- motion ----------------------------------------------------------------
@mcp.tool(
    name="mouse_move",
    description=(
        "Move the real pointer to (x,y) with a human-shaped path: Fitts-law duration (0.18–1.6 s), a "
        "curved minimum-jerk trajectory, 1.5–3 px tremor, and a 12% chance of a small overshoot followed "
        "by a corrective sub-move. curve='straight' skips curvature and tremor. duration_s overrides the "
        "Fitts duration. Requires arm; coordinates outside the virtual screen are refused. Returns the "
        "emitted point count, duration and final cursor."
    ),
    annotations=WRITE,
)
def mouse_move(x: int, y: int, duration_s: float | None = None, curve: str = "human") -> str:
    return _guard_call("mouse_move", _svc().move, x, y, duration_s, curve)


@mcp.tool(
    name="mouse_click",
    description=(
        "Click the real pointer, optionally after moving to (x,y). The approach slows the final 24 px and "
        "pauses 55–120 ms before button down; the press is held 35–90 ms. button is left/middle/right, "
        "count 1–3. Requires arm."
    ),
    annotations=ACT,
)
def mouse_click(button: str = "left", count: int = 1, x: int | None = None, y: int | None = None) -> str:
    return _guard_call("mouse_click", _svc().click, button, count, x, y)


@mcp.tool(
    name="mouse_drag",
    description=(
        "Press, drag along a human-shaped path and release. Moves to (x1,y1) with a slowed click approach, "
        "holds the button through the drag to (x2,y2), then releases even if the guard closes mid-drag. "
        "Requires arm."
    ),
    annotations=ACT,
)
def mouse_drag(x1: int, y1: int, x2: int, y2: int, button: str = "left") -> str:
    return _guard_call("mouse_drag", _svc().drag, x1, y1, x2, y2, button)


@mcp.tool(
    name="mouse_scroll",
    description=(
        "Scroll the wheel as 4–9 decaying events with 15–40 ms gaps (dy>0 up, dy<0 down, dx>0 right, "
        "dx<0 left; ±40 ticks maximum). Requires arm."
    ),
    annotations=WRITE,
)
def mouse_scroll(dy: int, dx: int = 0) -> str:
    return _guard_call("mouse_scroll", _svc().scroll, dy, dx)


@mcp.tool(
    name="mouse_type",
    description=(
        "Type text into the focused window at 55–140 ms per character using XTEST key events (the text is "
        "never placed in a process argument list). Characters outside the active keymap fall back to "
        "xdotool on stdin. Requires arm."
    ),
    annotations=ACT,
)
def mouse_type(text: str) -> str:
    return _guard_call("mouse_type", _svc().type_text, text)


@mcp.tool(
    name="mouse_key",
    description=(
        "Send one key combination in xdotool syntax, e.g. 'Return', 'ctrl+l', 'alt+Tab'. Requires arm."
    ),
    annotations=ACT,
)
def mouse_key(combo: str) -> str:
    return _guard_call("mouse_key", _svc().key, combo)


# -- windows ---------------------------------------------------------------
@mcp.tool(
    name="mouse_list_windows",
    description="List managed X11 windows with id, geometry and title (wmctrl). Read-only.",
    annotations=READ,
)
def mouse_list_windows() -> str:
    return _json(_svc().list_windows())


@mcp.tool(
    name="mouse_focus_window",
    description=(
        "Focus the first window whose title contains the given text; returns the match and geometry. "
        "Changes desktop focus, so it requires arm."
    ),
    annotations=WRITE,
)
def mouse_focus_window(title: str) -> str:
    return _guard_call("mouse_focus_window", _svc().focus_window, title)


# -- telemetry -------------------------------------------------------------
@mcp.tool(
    name="mouse_telemetry",
    description=(
        "Return the live telemetry state (same payload as GET /api/state on 127.0.0.1:8340) plus the last "
        "sessions with steps, distance and speeds. Read-only."
    ),
    annotations=READ,
)
def mouse_telemetry(limit: int = 20) -> str:
    return _guard_call("mouse_telemetry", _svc().telemetry, limit)


@mcp.tool(
    name="mouse_replay_session",
    description=(
        "Visually replay a recorded session path (downsampled to at most 1200 points) with no clicks, at "
        "the given speed multiplier. Requires arm because it moves the real pointer."
    ),
    annotations=WRITE,
)
def mouse_replay_session(session_id: int, speed: float = 2.0) -> str:
    return _guard_call("mouse_replay_session", _svc().replay_session, session_id, speed)


def main() -> None:
    service = get_service()
    install_signal_handlers(service.guard)
    try:
        mcp.run(transport="stdio")
    except (KeyboardInterrupt, SystemExit):
        service.guard.stop(reason="server exit")
        raise SystemExit(0)


if __name__ == "__main__":
    main()
