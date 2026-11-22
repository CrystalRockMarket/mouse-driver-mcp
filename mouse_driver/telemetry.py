"""SQLite telemetry plus the loopback HTTP/SSE API on 127.0.0.1:8340.

The database is the durable record of every move; the HTTP API is what the HUD
and the site read. The server is threaded, binds loopback only, sends no CORS
headers, serves no files and answers nothing outside the documented routes.

Control endpoints are deliberately narrow: ``POST /api/disarm`` and
``POST /api/stop``. There is no remote "move" endpoint — motion is only
reachable through the MCP tools or the CLI, and both obey the arming gate.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import config
from .motion import Point

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    ended_at REAL,
    actor TEXT NOT NULL DEFAULT 'agent',
    pid INTEGER NOT NULL DEFAULT 0,
    steps INTEGER NOT NULL DEFAULT 0,
    distance_px REAL NOT NULL DEFAULT 0,
    max_speed REAL NOT NULL DEFAULT 0,
    last_action TEXT,
    last_target TEXT
);
CREATE TABLE IF NOT EXISTS path_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    t REAL NOT NULL,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS path_points_session ON path_points(session_id, id);
CREATE INDEX IF NOT EXISTS path_points_ts ON path_points(ts);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
"""

MAX_SESSIONS_KEPT = 200


def _iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


class Store:
    """Thread-local SQLite access with WAL and a busy timeout."""

    def __init__(self, db_path: Path | str = config.DB_PATH) -> None:
        config.ensure_dirs()
        self.path = Path(db_path)
        self._local = threading.local()
        self._session_lock = threading.Lock()
        self._session_id: int | None = None
        self._init_db()

    # -- plumbing --------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=3.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=3000")
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        conn = self._conn()
        conn.executescript(SCHEMA)
        # Bounded retention: paths and events for the newest sessions survive.
        conn.execute(
            "DELETE FROM sessions WHERE id NOT IN "
            "(SELECT id FROM sessions ORDER BY id DESC LIMIT ?)",
            (MAX_SESSIONS_KEPT,),
        )

    # -- sessions --------------------------------------------------------
    def start_session(self, actor: str = "agent") -> int:
        conn = self._conn()
        cur = conn.execute(
            "INSERT INTO sessions (started_at, actor, pid) VALUES (?, ?, ?)",
            (time.time(), actor, 0),
        )
        with self._session_lock:
            self._session_id = int(cur.lastrowid or 0)
        return self._session_id or 0

    def ensure_session(self, actor: str = "agent") -> int:
        with self._session_lock:
            if self._session_id:
                return self._session_id
        return self.start_session(actor)

    def current_session_id(self) -> int:
        return self.ensure_session()

    def end_session(self) -> None:
        with self._session_lock:
            session_id = self._session_id
            self._session_id = None
        if not session_id:
            return
        self._conn().execute("UPDATE sessions SET ended_at=? WHERE id=? AND ended_at IS NULL", (time.time(), session_id))

    # -- writes ----------------------------------------------------------
    def record_move(
        self,
        points: list[Point],
        *,
        action: str,
        target: str,
        duration_s: float,
        session_id: int | None = None,
    ) -> dict:
        """Persist one planned move and fold its metrics into the session."""
        session_id = session_id or self.current_session_id()
        if not points:
            return {"steps": 0, "distance_px": 0.0, "max_speed": 0.0}
        # Anchor the final sample to "now" so no timestamp points into the future
        # and the HUD's two-second window matches what the eye saw.
        now = time.time()
        started = now - points[-1].t
        rows = []
        distance = 0.0
        max_speed = 0.0
        prev = points[0]
        for index, point in enumerate(points):
            if index:
                gap = max(1e-4, point.t - prev.t)
                seg = ((point.x - prev.x) ** 2 + (point.y - prev.y) ** 2) ** 0.5
                distance += seg
                max_speed = max(max_speed, seg / gap)
            rows.append((session_id, started + point.t, point.t, int(round(point.x)), int(round(point.y))))
            prev = point
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(
                "INSERT INTO path_points (session_id, ts, t, x, y) VALUES (?, ?, ?, ?, ?)", rows
            )
            conn.execute(
                "UPDATE sessions SET steps = steps + ?, distance_px = distance_px + ?, "
                "max_speed = MAX(max_speed, ?), last_action = ?, last_target = ? WHERE id = ?",
                (len(rows), distance, max_speed, action, target, session_id),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {
            "steps": len(rows),
            "distance_px": round(distance, 1),
            "max_speed": round(max_speed, 1),
        }

    def add_event(self, kind: str, detail: str = "", session_id: int | None = None) -> None:
        self._conn().execute(
            "INSERT INTO events (session_id, ts, kind, detail) VALUES (?, ?, ?, ?)",
            (session_id or self._session_id, time.time(), kind, detail[:300]),
        )

    # -- reads -----------------------------------------------------------
    def _session_row(self, session_id: int | None = None) -> dict | None:
        conn = self._conn()
        if session_id:
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        data = dict(row)
        last_ts = conn.execute(
            "SELECT MAX(ts) FROM path_points WHERE session_id=?", (data["id"],)
        ).fetchone()[0]
        span = max(0.05, (last_ts or data["started_at"]) - data["started_at"])
        return {
            "id": data["id"],
            "started": _iso(data["started_at"]),
            "started_at": data["started_at"],
            "ended_at": data["ended_at"],
            "actor": data["actor"],
            "steps": data["steps"],
            "distance_px": round(data["distance_px"], 1),
            "avg_speed": round(data["distance_px"] / span, 1),
            "max_speed": round(data["max_speed"], 1),
            "last_action": data["last_action"],
            "last_target": data["last_target"],
        }

    def session(self, session_id: int | None = None) -> dict:
        row = self._session_row(session_id)
        if row is None:
            return {
                "id": None,
                "started": None,
                "steps": 0,
                "distance_px": 0.0,
                "avg_speed": 0.0,
                "max_speed": 0.0,
                "last_action": None,
                "last_target": None,
            }
        return row

    def recent_points(self, window_s: float = 2.0, limit: int = 400) -> list[list[int]]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT x, y FROM path_points WHERE ts >= ? ORDER BY id DESC LIMIT ?",
            (time.time() - window_s, limit),
        ).fetchall()
        return [[r["x"], r["y"]] for r in reversed(rows)]

    def events(self, limit: int = 3) -> list[dict]:
        rows = self._conn().execute(
            "SELECT ts, kind, detail FROM events ORDER BY id DESC LIMIT ?", (max(1, limit),)
        ).fetchall()
        return [
            {"t": _iso(r["ts"]), "kind": r["kind"], "detail": r["detail"]} for r in reversed(rows)
        ]

    def sessions(self, limit: int = 20) -> list[dict]:
        limit = max(1, min(100, int(limit)))
        rows = self._conn().execute(
            "SELECT id FROM sessions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._session_row(r["id"]) or {} for r in rows]

    def session_path(self, session_id: int, max_points: int = 2000) -> dict:
        conn = self._conn()
        exists = conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not exists:
            return {"ok": False, "error": f"session {session_id} not found", "points": []}
        total = conn.execute(
            "SELECT COUNT(*) FROM path_points WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        stride = max(1, (int(total) + max_points - 1) // max_points)
        rows = conn.execute(
            "SELECT t, x, y FROM path_points WHERE session_id=? AND (id % ? = 0 OR id IN "
            "(SELECT MAX(id) FROM path_points WHERE session_id=?)) ORDER BY id",
            (session_id, stride, session_id),
        ).fetchall()
        return {
            "ok": True,
            "session_id": session_id,
            "total_points": int(total),
            "stride": stride,
            "points": [[r["x"], r["y"], round(r["t"], 3)] for r in rows],
        }


class _Handler(BaseHTTPRequestHandler):
    server_version = "MouseDriver/1.0"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:  # keep stdout clean for stdio MCP
        return

    # -- helpers ---------------------------------------------------------
    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _bad_route(self) -> None:
        self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

    def _json_body(self) -> dict:
        raw = getattr(self, "_raw_body", b"") or b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode() or "{}")
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _discard_body(self) -> bool:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 1024:
            self._send_json({"ok": False, "error": "body too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return False
        if length:
            self.rfile.read(length)
        return True

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        api: TelemetryAPI = self.server.api  # type: ignore[attr-defined]
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/healthz":
            payload = api.health()
        elif path == "/api/state":
            payload = api.build_state()
        elif path == "/api/stream":
            self._stream()
            return
        elif path == "/api/task":
            payload = api.task_get()
        elif path == "/api/sessions":
            payload = {"ok": True, "sessions": api.store.sessions(20)}
        elif path.startswith("/api/sessions/"):
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[3] == "path" and parts[2].isdigit():
                payload = api.store.session_path(int(parts[2]), 2000)
            elif len(parts) == 3 and parts[2].isdigit():
                payload = {"ok": True, "session": api.store.session(int(parts[2]))}
            else:
                payload = {"ok": False, "error": "not found"}
                self._send_json(payload, HTTPStatus.NOT_FOUND)
                return
        else:
            self._bad_route()
            return
        self._send_json(payload)

    def do_POST(self) -> None:  # noqa: N802
        api: TelemetryAPI = self.server.api  # type: ignore[attr-defined]
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 1024:
            self._send_json({"ok": False, "error": "body too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        self._raw_body = self.rfile.read(length) if length > 0 else b""
        if path == "/api/disarm":
            payload = api.disarm("hud")
        elif path == "/api/stop":
            payload = api.stop("hud stop button")
        elif path == "/api/panic":
            payload = api.panic()
        elif path == "/api/lock":
            payload = api.lock()
        elif path == "/api/unlock":
            payload = api.unlock()
        elif path == "/api/pause":
            payload = api.pause()
        elif path == "/api/resume":
            payload = api.resume()
        elif path == "/api/keyboard/toggle":
            payload = api.toggle_keyboard()
        elif path == "/api/hud/toggle":
            payload = api.toggle_hud()
        elif path == "/api/hud/visible":
            payload = api.set_hud_visibility(self._json_body())
        elif path == "/api/hud/position":
            payload = api.set_hud_position(self._json_body())
        elif path == "/api/hud/reset":
            payload = api.reset_hud_position()
        elif path == "/api/hud/scale":
            payload = api.set_hud_scale(self._json_body())
        elif path == "/api/hud/offset":
            payload = api.set_hud_offset(self._json_body())
        elif path == "/api/task":
            payload = api.task_set(self._json_body())
        elif path == "/api/task/start":
            payload = api.task_start()
        elif path == "/api/task/stop":
            payload = api.task_stop()
        else:
            self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        self._send_json(payload)

    def _stream(self) -> None:
        api: TelemetryAPI = self.server.api  # type: ignore[attr-defined]
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while not api.stopping.is_set():
                payload = json.dumps(api.build_state(), separators=(",", ":"))
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
                api.stopping.wait(0.25)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class TelemetryAPI:
    """Threaded loopback HTTP API around a :class:`Store`."""

    def __init__(
        self,
        store: Store,
        *,
        guard=None,
        driver=None,
        host: str = config.API_HOST,
        port: int = config.API_PORT,
        started_at: float | None = None,
        state_extra: Callable[[], dict] | None = None,
        target_provider: Callable[[], dict | None] | None = None,
        keyboard_provider: Callable[[], dict] | None = None,
        control=None,
    ) -> None:
        self.store = store
        self.guard = guard
        self.driver = driver
        self.host = host
        self.port = int(port)
        self.started_at = started_at or time.time()
        self.state_extra = state_extra
        self.target_provider = target_provider
        self.keyboard_provider = keyboard_provider
        self.control = control
        self.stopping = threading.Event()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self.error: str | None = None

    # -- payloads --------------------------------------------------------
    def health(self) -> dict:
        armed = bool(self.guard.is_armed()) if self.guard else False
        guard_state = self.guard.state() if self.guard else {}
        backend = getattr(self.driver, "backend", "unavailable") if self.driver is not None else "unavailable"
        return {
            "ok": self.driver is not None,
            "armed": armed,
            "paused": bool(guard_state.get("paused")),
            "locked": bool(guard_state.get("locked")),
            "driver": backend,
            "uptime_s": round(time.time() - self.started_at, 1),
        }

    def build_state(self) -> dict:
        armed_info = self.guard.state() if self.guard else {"armed": False, "arm_expires": None}
        cursor = (0, 0)
        if self.driver is not None:
            try:
                cursor = self.driver.pointer()
            except Exception:
                cursor = (0, 0)
        geometry = self.driver.geometry() if self.driver is not None else {
            "w": config.DEFAULT_SCREEN[0],
            "h": config.DEFAULT_SCREEN[1],
            "monitors": config.DEFAULT_SCREEN[2],
        }
        payload = {
            "armed": bool(armed_info.get("armed")),
            "arm_expires": armed_info.get("arm_expires"),
            "cursor": {"x": int(cursor[0]), "y": int(cursor[1])},
            "screen": {"w": geometry["w"], "h": geometry["h"], "monitors": geometry["monitors"]},
            "session": self.store.session(),
            "recent": self.store.recent_points(2.0),
            "events": self.store.events(3),
            "target": self._target(),
            "locked": bool(armed_info.get("locked")),
            "lock_reason": armed_info.get("lock_reason") or "",
            "paused": bool(armed_info.get("paused")),
            "driver": "unavailable" if self.driver is None else "ok",
            **self._keyboard(),
        }
        if self.state_extra is not None:
            try:
                payload.update(self.state_extra() or {})
            except Exception:
                pass
        return payload

    def _keyboard(self) -> dict:
        if self.keyboard_provider is None:
            from .control import read_hud_state

            hud = read_hud_state()
            return {
                "keyboard_expanded": False,
                "keys_recent": [],
                "hud_visible": hud["visible"],
                "hud": hud,
                "task": None,
            }
        try:
            payload = self.keyboard_provider()
            payload.setdefault("hud_visible", True)
            payload.setdefault("hud", {"visible": payload["hud_visible"], "mode": "auto", "x": 0, "y": 0})
            payload.setdefault("task", None)
            return payload
        except Exception:
            from .control import read_hud_state

            hud = read_hud_state()
            return {
                "keyboard_expanded": False,
                "keys_recent": [],
                "hud_visible": hud["visible"],
                "hud": hud,
                "task": None,
            }

    def toggle_hud(self) -> dict:
        if self.control is not None:
            return self.control.toggle_hud_visible()
        from .control import read_hud_state, write_hud_state

        return write_hud_state(visible=not read_hud_state()["visible"])

    def set_hud_visibility(self, body: dict) -> dict:
        if "visible" not in body:
            return {"ok": False, "error": "expected {\"visible\": true|false}"}
        if self.control is not None:
            return self.control.set_hud_visible(bool(body["visible"]))
        from .control import write_hud_state

        return write_hud_state(visible=bool(body["visible"]))

    def set_hud_position(self, body: dict) -> dict:
        if "x" not in body or "y" not in body:
            return {"ok": False, "error": "expected {\"x\": int, \"y\": int, \"mode\": \"manual\"}"}
        mode = "manual" if str(body.get("mode", "manual")) == "manual" else "auto"
        if self.control is not None:
            return self.control.set_hud_position(int(body["x"]), int(body["y"]), mode)
        from .control import write_hud_state

        return write_hud_state(x=int(body["x"]), y=int(body["y"]), mode=mode)

    def set_hud_offset(self, body: dict) -> dict:
        if "offset_x" not in body or "offset_y" not in body:
            return {"ok": False, "error": "expected {\"offset_x\":int,\"offset_y\":int,...}"}
        mode = "custom" if str(body.get("mode", "custom")) == "custom" else "auto"
        x = body.get("x")
        y = body.get("y")
        if self.control is not None:
            return self.control.set_hud_offset(
                int(body["offset_x"]), int(body["offset_y"]), mode,
                int(x) if x is not None else None, int(y) if y is not None else None,
            )
        from .control import write_hud_state

        patch = {"offset_x": int(body["offset_x"]), "offset_y": int(body["offset_y"]), "mode": mode}
        if x is not None and y is not None:
            patch["x"], patch["y"] = int(x), int(y)
        return write_hud_state(**patch)

    def task_get(self) -> dict:
        if self.control is None:
            return {"ok": False, "error": "task state unavailable"}
        return {"ok": True, "task": self.control.task_model(), "status": self.control.task_status()}

    def task_set(self, body: dict) -> dict:
        if self.control is None:
            return {"ok": False, "error": "task state unavailable"}
        try:
            return self.control.task_set(body)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def task_start(self) -> dict:
        if self.control is None:
            return {"ok": False, "error": "task state unavailable"}
        try:
            return self.control.task_start()
        except Exception as exc:
            return {"ok": False, "error": str(exc), "code": getattr(exc, "code", "refused")}

    def task_stop(self) -> dict:
        if self.control is None:
            return {"ok": False, "error": "task state unavailable"}
        return self.control.task_stop()

    def set_hud_scale(self, body: dict) -> dict:
        if "scale" not in body:
            return {"ok": False, "error": "expected {\"scale\": number}"}
        try:
            scale = float(body["scale"])
        except (TypeError, ValueError):
            return {"ok": False, "error": "scale must be a number"}
        if self.control is not None:
            return self.control.set_hud_scale(scale)
        from .control import write_hud_state

        return write_hud_state(scale=scale)

    def reset_hud_position(self) -> dict:
        if self.control is not None:
            return self.control.reset_hud_position()
        from .control import write_hud_state

        return write_hud_state(mode="auto")

    def toggle_keyboard(self) -> dict:
        if self.control is not None:
            return self.control.toggle_keyboard()
        return {"ok": False, "error": "keyboard state unavailable"}

    def _target(self) -> dict | None:
        if self.target_provider is None:
            return None
        try:
            return self.target_provider()
        except Exception:
            return None

    def disarm(self, reason: str) -> dict:
        if self.guard is None:
            return {"ok": False, "error": "no guard"}
        result = self.guard.disarm(reason)
        self.store.add_event("disarm", reason)
        return result

    def pause(self) -> dict:
        if self.control is not None:
            return self.control.pause()
        result = self.guard.pause(by="api")
        self.store.add_event("pause", "api")
        return result

    def resume(self) -> dict:
        if self.control is not None:
            return self.control.resume()
        result = self.guard.resume()
        self.store.add_event("resume", "api")
        return result

    def lock(self) -> dict:
        if self.control is not None:
            return self.control.lock()
        result = self.guard.lock("api lock")
        self.store.add_event("lock", "api")
        return result

    def unlock(self) -> dict:
        if self.control is not None:
            return self.control.unlock()
        result = self.guard.unlock()
        self.store.add_event("unlock", "api")
        return result

    def panic(self) -> dict:
        if self.control is not None:
            return self.control.panic()
        result = self.guard.panic("api panic")
        self.store.add_event("panic", "api")
        return result

    def stop(self, reason: str) -> dict:
        if self.guard is None:
            return {"ok": False, "error": "no guard"}
        result = self.guard.stop(reason)
        self.store.add_event("stop", reason)
        return result

    # -- lifecycle -------------------------------------------------------
    def start(self) -> dict:
        try:
            server = _Server((self.host, self.port), _Handler)
        except OSError as exc:
            self.error = f"{self.host}:{self.port} unavailable: {exc}"
            print(f"mouse-driver telemetry: {self.error}; MCP continues without the API", file=sys.stderr)
            return {"ok": False, "error": self.error}
        server.api = self  # type: ignore[attr-defined]
        self.port = int(server.server_address[1])
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="mouse-telemetry", daemon=True)
        self._thread.start()
        return {"ok": True, "url": f"http://{self.host}:{self.port}"}

    def stop_server(self) -> None:
        self.stopping.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
