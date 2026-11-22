# Mouse Driver — design

## Motion model

The planner is pure and testable (`mouse_driver/motion.py`); the driver only
paces it.

- **Duration** — Fitts' law, `0.12 + 0.22·log2(d/20 + 1)` s, clamped to
  0.18–1.60 s; `duration_s` overrides it.
- **Path** — cubic Bezier whose control points are offset perpendicular to the
  straight line by up to 16 % of the distance, more for longer moves.
- **Speed** — minimum-jerk `s(t) = 10t³ − 15t⁴ + 6t⁵`, sampled at 120 Hz
  (~8.3 ms), so the pointer accelerates out of rest and settles.
- **Tremor** — two out-of-phase sinusoids, 1.5–3 px, fading to zero at both ends.
- **Overshoot** — 12 % of moves travel 2–4 % past the target, pause 40–90 ms and
  correct with a short low-tremor sub-move.
- **Click approach** — the last 24 px take ~22 % of the duration and a 55–120 ms
  settle separates arrival from button down.
- **Drag** — approach, press, a human-shaped travel, then release in a `finally`
  so a pause, abort or exception never leaves a button held.
- **Scroll** — 4–9 wheel events with decaying tick counts and 15–40 ms gaps.

Every sample re-reads the shared guard state and the `data/STOP` sentinel, which
is what makes pause/stop effective within a frame even across processes.

## Arming, pause and lock

State lives in `data/armed.json`, guarded by `flock`, so the MCP server, CLI,
HUD and any helper agree without a daemon.

| Field | Meaning |
| --- | --- |
| `armed`, `expires` | Time-boxed permission (TTL 1–900 s, default 300). |
| `paused` | Armed but intentionally held. Actions refuse; TTL keeps running. |
| `locked` | Operator lock. `mouse_arm` refuses; only the unlock chord clears it. |

Transitions:

- `arm(ttl)` — refused while locked; clears a stale sentinel, resets pause.
- `disarm` — armed=false, paused=false; no sentinel, no lock.
- `stop` — sentinel + disarm; no lock.
- `pause` — requires armed; releases held buttons; keeps `expires`.
- `resume` — clears pause; refuses `arm_expired` when the TTL ran out while
  paused; refuses `not_armed` if the session was disarmed (e.g. after unlock).
- `panic` — halts motion, writes the sentinel, releases held buttons, disarms
  and locks. The pre-panic pause flag is preserved, so unlock leaves the session
  exactly as paused/unpaused as it was, but disarmed.

Motion loops call `Guard.action_state()` per sample: `run`, `pause` or `stop`.
On `pause` the driver holds position, releases any button, waits, and on
`resume` continues the **remaining** points of the same plan. On `stop` the move
ends immediately.

Global chords are passive X grabs that replay every key with
`XAllowEvents(ReplayKeyboard)`, so the focused page still receives it:

| Keysym | Modifiers | Action |
| --- | --- | --- |
| `Escape` | none | double tap (≤600 ms) = disarm |
| `Escape` | Ctrl+Alt+Shift | panic (stop + lock) |
| `Return` | Ctrl+Alt+Shift | unlock |
| `p` | Ctrl+Alt+Shift | pause / resume |

Each combination is grabbed with CapsLock/NumLock variants. A grab that another
client already owns (i3 mode bindings) simply does not install; the rest remain.

## Target window and HUD anchoring

The driver keeps `target_window` in session state and persists its id in
`data/target.json`. It is set when the agent focuses a window, clicks, ends a
move inside a window, drags or types. Resolution follows the stacking chain from
the root child down through subwindows and returns the deepest window carrying a
`WM_CLASS`, skipping i3 frames (`i3-frame`), hidden tabs and render widgets.
Failing any explicit target, the currently active window
(`_NET_ACTIVE_WINDOW`) is used.

`GET /api/state` recomputes geometry, mapped state, workspace and activation on
every request, so the HUD sees fresh values at 8 Hz. The HUD also re-reads
`xwininfo -id <target>` itself each poll, then:

- width `min(430, max(320, 0.42·w))`, clamped to `w − 24` for tiny windows;
- collapsed height is content-sized (~170 px); expanded is 360 px;
- origin `(x + w − width − 12, y + 12)`, clamped inside the client area;
- override-redirect + keep-above, never focused, re-raised at 1.5 s intervals
  only while the target is the active window;
- empty input shape (click-through) so it never intercepts a click;
- visible whenever `hud.visible` and the target's mapped rectangle intersects a
  monitor; visibility is geometric, never based on the i3 desktop index (two
  per-output desktops are physically visible at once);
- hidden when the target closes, unmaps or leaves every monitor; bottom-right of
  the primary monitor with “no target” when there is none;
- the X hides (`hud.visible=false`) without exiting; the ⏸/▶ header button
  toggles pause/resume (22×22, fourth input region, disabled while locked or
  disarmed);
- the ⠿ grip drags the panel into a **custom offset** (3 px threshold; a click
  never moves it). Offsets are measured from the target's top-right corner and
  clamped inside it, so the panel follows moves, resizes, maximise and workspace
  changes instead of floating free; `Ctrl Alt Shift R` resets to the 12,12 auto
  offset. A target smaller than the panel aligns top-left and overhangs only as
  needed. The ⤡ corner grip scales the panel from
  a single factor (`width/430`, 0.85–1.8) by regenerating the CSS base table,
  keycaps and canvas; double-click resets to 1.0. The driver is the single
  writer of `data/hud.json`, re-reading it on mtime change.

The panel always shows the shortcut legend as keycaps and emphasises the
relevant ones: `ARMED` → pause + stop, `PAUSED` → resume, `LOCKED` → unlock,
`IDLE` → all neutral.

## Telemetry JSON

The site contract is implemented exactly; two fields are appended (`driver` in
`/api/state`, and `paused`/`locked` in `/healthz`) and `target` is the agreed
addition. Session rows carry a few extra keys (`started_at`, `ended_at`,
`actor`) that clients may ignore.

```jsonc
// GET /api/state
{
  "armed": true,                     // false when locked, stopped or expired
  "arm_expires": "2026-11-21T17:20:31-05:00",  // ISO 8601 local offset, or null
  "cursor": { "x": 1700, "y": 900 },
  "screen": { "w": 5120, "h": 1440, "monitors": 2 },
  "session": {
    "id": 4,
    "started": "2026-11-21T17:15:02-05:00",
    "started_at": 1795299302.51,      // epoch seconds (extra)
    "ended_at": null,                 // (extra)
    "actor": "agent",                 // (extra)
    "steps": 541,
    "distance_px": 1950.4,
    "avg_speed": 256.8,               // px/s over the session span
    "max_speed": 975.2,               // px/s, peak sample
    "last_action": "move",
    "last_target": "3100,620"
  },
  "recent": [[3000, 500], [3001, 502]],   // last 2 s, [x, y]
  "events": [{"t": "2026-11-21T17:16:04-05:00", "kind": "pause", "detail": "3000,500"}],
  "target": {
    "id": 44040195,                   // X11 window id (decimal)
    "title": "bbc.com",               // _NET_WM_NAME, browser suffix stripped
    "cls": "firefox",                 // WM_CLASS class part
    "x": 2563, "y": 90, "w": 2554, "h": 1347,   // client area, root coords
    "desktop": 1,                     // _NET_WM_DESKTOP (-1 unknown)
    "mapped": true,
    "visible": true,                  // mapped and on the current desktop
    "active": true                    // _NET_ACTIVE_WINDOW match
  },
  "locked": false,
  "lock_reason": "",
  "paused": false,
  "driver": "ok",                     // "unavailable" when X is unreachable
  "hud": {
    "visible": true,
    "mode": "auto",              // "auto" (12,12) or "custom" (dragged)
    "offset_x": 12,              // from the target's right edge to the panel's right
    "offset_y": 12,              // from the target's top edge
    "x": 2118, "y": 12,          // last realised screen position (telemetry)
    "scale": 1.0                 // 0.85–1.8
  },
  "keyboard_expanded": false,         // overlay keyboard state (persisted)
  "keys_recent": [                    // last 2 s of keys, max 24, oldest → newest
    {"key": "Ctrl+Alt+Shift+T", "code": 0, "t": 1795299546.49, "source": "driver"}
  ]
}
```

`target` is `null` when no window can be resolved.

```jsonc
// GET /healthz
{"ok": true, "armed": true, "paused": false, "locked": false, "driver": "xtest", "uptime_s": 41.2}

// GET /api/sessions
{"ok": true, "sessions": [ /* up to 20 session objects, newest first */ ]}

// GET /api/sessions/<id>/path
{"ok": true, "session_id": 4, "total_points": 1200, "stride": 1,
 "points": [[3000, 500, 0.0], [3001, 502, 0.008]]}   // [x, y, t seconds within the move]

// GET /api/stream
// text/event-stream; one "data: <same JSON as /api/state>" frame every 250 ms

// POST /api/disarm | /api/stop | /api/panic | /api/lock | /api/unlock
//      /api/pause | /api/resume | /api/keyboard/toggle
//      /api/hud/visible {"visible": bool} | /api/hud/toggle
//      /api/hud/position {"x":int,"y":int,"mode":"manual"} | /api/hud/reset
//      /api/hud/scale {"scale": 0.85..1.8}
//      /api/hud/offset {"offset_x":int,"offset_y":int,"mode":"custom","x":int,"y":int}
{"ok": true, ...}
```

Everything is loopback-only (`127.0.0.1:8340`), with `Cache-Control: no-store`,
`X-Content-Type-Options: nosniff` and no CORS headers. There is no remote motion
endpoint: motion is only reachable through the MCP tools or the CLI, and both
obey arming. Control endpoints exist so the HUD and CLI can drive the same
safety transitions the human chords use.

## Overlay keyboard

`/api/state` publishes `keys_recent` (last 2 s, max 24, newest last) and
`keyboard_expanded`. Each entry is `{key, code, t, source}`; `key` is a display
label such as `Ctrl+Shift+T` for a `mouse_key` combo (all modifiers of the
combination are highlighted) or a single character for `mouse_type`. The HUD
renders entries within 250 ms, lights each component for 140 ms, and collapses
consecutive repeats in the recent row to `A ×3`.

Expansion is keyboard-only (`Ctrl Alt Shift K` → `POST /api/keyboard/toggle` or
`data/keyboard.json`), so the panel stays click-through. Physical key capture
would need XRecord; it is deliberately not implemented, so the strip is labelled
`RECENT KEYS` and shows driver-synthesised events only. A passive grab over
typing keys was rejected because it would intercept the user's input.

## Threat model

**While armed, an agent can do anything the user could do with the pointer and
keyboard**: click a payment button, accept a dialog, type into a terminal, read
whatever is on screen through the model's own vision if it has it. The driver
itself never reads files, environment secrets or credentials, and typing never
puts text in a process argument list.

Mitigations, in order of strength:

1. **Time-boxed arming** — default 300 s, hard maximum 900 s; expiry is advisory
   but enforced on every sample.
2. **Human chords** — panic locks arming; unlock is a different chord and no MCP
   tool can call it. The model cannot recover from a panic on its own.
3. **Pause** — a human can hold the pointer mid-task without discarding the
   session; actions refuse while paused.
4. **Sentinel** — `touch data/STOP` halts any process within one sample, even if
   the MCP server is wedged; the HUD panic writes it first.
5. **Loopback telemetry** — the API has no auth because it is local-only and
   never exposes motion; it cannot be reached from the tailnet or the internet.
6. **Click-through HUD** — the panel cannot swallow the user's clicks, and the
   key grabs replay so Esc and the chords still reach the page.

Out of scope: a compromised local account (it could edit the state file), screen
capture of secrets, and clickjacking-style attacks where a page moves a button
under an approaching pointer. There is no detection for "the click landed on
something dangerous"; the arming window and the human chords are the control.

## Limits

- One X session on one host; virtual-screen bounds only (no per-monitor policy).
- i3-specific container handling; other WMs fall back to the active window.
- Key grabs can lose to another client (i3 mode bindings); the unaffected chords
  still install and the legend still documents them.
- The keysym map covers the active layout; unsupported scripts fall back to
  `xdotool type --file -` on stdin.
- Telemetry retention: 200 sessions, path points and events pruned with them.
