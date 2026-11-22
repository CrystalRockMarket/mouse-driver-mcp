# Mouse Driver

Human-shaped control of the real X11 pointer on **the desktop host** (the desktop box),
with a small always-on-top companion HUD and a local telemetry API. Built for
agents that need to operate the actual browser, not a headless replica.

- Two 2560×1440 monitors, virtual screen 5120×1440, `DISPLAY=:0`, i3.
- XTEST via `libX11`/`libXtst` (ctypes) — no `xdotool` in the motion path.
- Opaque GTK3 HUD, no compositor needed.

## Safety first — the chords

The driver only acts while **armed**, and three global chords let a human take
control back instantly, from any application, without clicking the panel:

| Chord | What it does | When to use it |
| --- | --- | --- |
| `Ctrl Alt Shift Esc` | **Panic.** Stops motion in one frame, releases any held button, disarms and **locks** arming. | Something looks wrong. Nothing can re-arm until you unlock. |
| `Ctrl Alt Shift Enter` | **Unlock.** Clears the lock and leaves the driver disarmed. | Only after you have looked. |
| `Ctrl Alt Shift H` | **Show / hide the HUD.** Works while the panel is hidden. |
| `Ctrl Alt Shift R` | **Re-anchor the HUD.** Returns a dragged panel to auto mode inside the target window. |
| `Ctrl Alt Shift K` | **Overlay keyboard.** Expands or collapses the on-screen keyboard inside the panel. |
| `Ctrl Alt Shift P` | **Pause / resume.** Holds the pointer where it is (releasing any held button) and keeps the session armed with its TTL ticking. A second press resumes the interrupted move from the paused position. | Step in and look without ending the session. |
| `Esc` `Esc` | **Disarm.** No lock, no sentinel. | A small mistake: wrong window, wrong target. |
| `Ctrl Alt Shift P` (while paused, TTL expired) | Resume is refused (`arm expired`); the session stays disarmed. | — |

**The lock is deliberately not an MCP tool.** `mouse_arm` refuses with
`operator lock: press Ctrl+Alt+Shift+Enter to allow arming`, and no tool in the
server can clear it. Physically holding `Ctrl Alt Shift Enter` at the keyboard
is the only way back. The same applies to the pause: agents cannot resume
themselves; the human chord or `POST /api/resume` (loopback, operator use) does.

The panel shows the same four shortcuts as keycaps at all times and highlights
the ones that matter for the current state (`ARMED`, `PAUSED`, `LOCKED`, `IDLE`).

## Install

```bash
cd ~/projects/mouse-driver-kituwa
python3 -m venv .venv
.venv/bin/pip install 'mcp==2.3.0' pytest
.venv/bin/python -m pytest -q
```

The HUD runs on the system Python because it needs GTK3 (`python3-gi`); the MCP
server and CLI run in the venv.

## Start

```bash
# MCP server (stdio) — also starts the telemetry API on 127.0.0.1:8340
bin/mouse-driver serve

# HUD (needs the desktop session)
bin/mouse-hud                 # click-through companion
bin/mouse-hud --interactive   # normal clicking, Disarm/STOP buttons visible

# CLI
bin/mouse-driver arm 300
bin/mouse-driver move 1700 900
bin/mouse-driver pause | resume | toggle
bin/mouse-driver stop | panic | lock | unlock
bin/mouse-driver status
```

`deploy/mouse-driver-hud.service` is a **user** systemd unit draft (not
installed): `Environment=DISPLAY=:0`, `XAUTHORITY=$HOME/.Xauthority`,
`WantedBy=graphical-session.target`, `Restart=always`.

## MCP configuration (opencode)

```jsonc
{
  "mcp": {
    "mouse-driver": {
      "type": "local",
      "command": [
        "$HOME/mouse-driver/.venv/bin/python",
        "-m",
        "mouse_driver.mcp_server"
      ],
      "environment": {
        "DISPLAY": ":0",
        "XAUTHORITY": "$HOME/.Xauthority"
      },
      "enabled": true
    }
  }
}
```

`DISPLAY` defaults to `:0` inside the process when unset (and `XAUTHORITY` to
`~/.Xauthority` when that file exists), so the handshake succeeds even from a
process that did not inherit the desktop environment. If X is genuinely
unreachable the server still completes `initialize` and `tools/list`;
`mouse_status` then reports `driver: "unavailable"` and every acting tool
returns a JSON refusal.

## HUD

The panel is a companion to the **target window**, not a screen-corner widget:

- Anchored inside the top-right of the target's client area, 12 px in,
  clamped so it never leaves the window.
- Width `min(430, max(320, 0.42 × window width))`, height ~200 px.
- Re-reads the target geometry at each 8 Hz poll, so it follows moves, resizes,
  maximise/unmaximise and workspace changes.
- Click-through: an empty X input shape, so it never blocks the page.
- Override-redirect and keep-above: visible over a fullscreen target, never
  focused, and re-raised every 1.5 s only while the target is the active window.
- Visible whenever `hud.visible` is true and the target has a mapped rectangle
  that intersects any monitor. Desktop index is ignored: with i3's per-output
  desktops two desktops can be physically visible at once.
- Hidden when the target closes, minimises or its rectangle leaves every
  monitor; with no target at all it falls back to the bottom-right of the
  primary monitor and says “no target”.
- A plain click on the header never moves the panel: the drag only starts after
  3 px of pointer travel.
- The **X** at the top-right hides the panel without quitting the process
  (`hud.visible=false`); `Ctrl Alt Shift H` brings it back. The X is the only
  clickable area besides the drag strip: the rest stays click-through.
- The **⏸/▶ pause button** sits left of the X: 22×22, amber `▶` while paused,
  white `⏸` while running, grey and inert when disarmed or locked. It toggles
  `POST /api/pause` / `/api/resume` and matches the PAUSED pill within one poll.
- The **⤡ resize grip** at the bottom-right scales the whole panel (CSS base
  table × scale, keyboard keycaps and canvas included): `s = width/430`, clamped
  0.85–1.8 (366–774 px). Double-clicking it returns to 1.0. Scale persists in
  `data/hud.json` and via `POST /api/hud/scale` / `mouse-driver hud scale 1.3`.
- The **⠿ grip** and `MOUSE DRIVER` line drag the panel, but it never floats
  free: the drag stores an offset from the target's top-right corner
  (`offset_x`, `offset_y`; default 12,12). The panel then follows the target
  through moves, resizes, maximise and workspace changes at that same offset.
  The footer reads `follow · auto` or `follow · custom`; `Ctrl Alt Shift R` or
  `mouse-driver hud reset` restores the 12,12 offset. Offsets are clamped so the
  panel stays inside the target (a smaller target aligns top-left and only
  overhangs as needed). Visibility, mode, offset, scale and last realised
  position persist in `data/hud.json` with the driver as the single writer.

![HUD over the browser](docs/images/hud-target.png)

States, in precedence order: `LOCKED` (red) → `PAUSED` (amber) → `ARMED`
(lime, with countdown) → `IDLE` (grey).

## Overlay keyboard

The panel carries a display-only keyboard that lights up the keys the driver
presses. It never accepts input and never blocks a click.

- **Collapsed (default):** one row under the legend reads
  `RECENT KEYS  Ctrl+Alt+Shift+T ×4 · Enter · Esc · A` — newest first, last six
  entries, consecutive repeats collapsed to `×N`. Panel ~430×170 (content-sized).
- **Expanded** (`Ctrl Alt Shift K`): the same recent-keys row plus a 60 % layout
  (`Esc 1…0 - = Backspace`, `Tab Q…P [ ] \`, `Caps A…; ' Enter`,
  `Shift Z…/ Shift`, `Ctrl Alt Space Alt Fn` + arrows), 24 px keycaps. Panel
  520×360. Keys light lime for ~140 ms; a combo lights every modifier it used.
- The toggle lives in `data/keyboard.json` and `POST /api/keyboard/toggle`, so
  the CLI (`mouse-driver keyboard`) and the panel agree.
- Physical key observation would need XRecord, which is not wired here; the
  strip is therefore labelled `RECENT KEYS` and carries driver-synthesised
  events only (`mouse_type`, `mouse_key`). A passive grab on typing keys was
  rejected on purpose.

## Repeat task

The panel's `TASK` row and the driver share one model in `data/task.json`
(1–20 steps of `key`/`type`/`wait`, repeat `infinite`/`count`/`duration`,
`interval_s`). `mouse_task_set/start/stop/status`, `mouse-driver task ...` and
the editor's Start/Stop all use it. A task never arms itself: start requires an
armed session, and it stops on pause, lock, panic, STOP, disarm or an expired
TTL, with hard caps of 10 000 cycles or 8 hours. The HUD editor opens from `▸`
below `RECENT KEYS`; while open the panel takes input (for the entries) instead
of being click-through, and `close` restores pass-through. Chords are unaffected
because only modifier combos are grabbed.

## Tools

| Tool | Purpose |
| --- | --- |
| `mouse_status` | Arming, lock, pause, TTL, cursor, screen, session totals, API URL. Read-only. |
| `mouse_arm(ttl_s)` | Time-boxed arming, 1–900 s. Refused while locked. |
| `mouse_disarm` | Disarm, aborting motion. Does not lock. |
| `mouse_stop` | Sentinel + disarm (no lock). |
| `mouse_where` | Cursor, screen size and monitor count. Read-only. |
| `mouse_move(x,y,duration_s,curve)` | Fitts-duration curved minimum-jerk path with tremor and occasional overshoot. |
| `mouse_click(button,count,x,y)` | Slowed 24 px approach, 35–90 ms hold. |
| `mouse_drag(x1,y1,x2,y2,button)` | Press, human-shaped drag, release even on abort. |
| `mouse_scroll(dy,dx)` | 4–9 decaying wheel events with 15–40 ms gaps. |
| `mouse_type(text)` | XTEST keys at 55–140 ms per character; text never in argv. |
| `mouse_key(combo)` | One xdotool-syntax combination. |
| `mouse_list_windows` | Managed windows with geometry and title. Read-only. |
| `mouse_focus_window(title)` | Focus by title substring; sets the HUD target. |
| `mouse_telemetry(limit)` | `/api/state` payload plus recent sessions. Read-only. |
| `mouse_replay_session(id,speed)` | Visual replay of a recorded path, no clicks. |
| `mouse_task_set(name,steps,repeat,interval_s,enabled)` | Validate and save the repeat task. |
| `mouse_task_start` / `mouse_task_stop` | Run or stop it (start requires arm). |
| `mouse_task_status` | Task model plus runner status. Read-only. |

There is intentionally no `mouse_unlock` and no `mouse_resume`.

## Telemetry API

Loopback only (`127.0.0.1:8340`), threaded stdlib server, no CORS, no auth.
Exact shapes are in [docs/DESIGN.md](docs/DESIGN.md#telemetry-json).

```
GET  /healthz
GET  /api/state
GET  /api/stream          (SSE, same payload every 250 ms)
GET  /api/sessions
GET  /api/sessions/<id>/path
POST /api/disarm  /api/stop  /api/pause  /api/resume
POST /api/panic   /api/lock  /api/unlock  /api/keyboard/toggle
```

## Motion model

- Duration: `0.12 + 0.22·log2(d/20 + 1)` s, clamped 0.18–1.60 s.
- Cubic Bezier with perpendicular control points (curvature grows with distance).
- Minimum-jerk profile `s = 10t³ − 15t⁴ + 6t⁵`.
- 1.5–3 px tremor, fading to zero at both ends.
- ~12 % of moves overshoot 2–4 % of the distance, pause 40–90 ms, then correct.
- Clicks slow the final 24 px and settle 55–120 ms before button down.
- Points emitted at 120 Hz; every loop re-reads the arming/pause/lock state and
  the `data/STOP` sentinel, so a human chord stops motion within ~8 ms.

## Limits

- Single desktop, single X session; not multi-head aware beyond the virtual screen.
- No clickjacking guard: while armed an agent can press anything the user could.
- The panic chord cannot beat a frozen X server; the sentinel file remains the
  cross-process backstop.
- Typing covers the active keymap plus an xdotool fallback for other scripts.
