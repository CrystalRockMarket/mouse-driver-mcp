from __future__ import annotations

import http.client
import json
import socket
import time
import urllib.request

import pytest

from mouse_driver.motion import Point
from mouse_driver.telemetry import TelemetryAPI


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def api(store, guard, driver):
    server = TelemetryAPI(store, guard=guard, driver=driver, port=_free_port())
    assert server.start()["ok"] is True
    store.record_move([Point(10 + i, 20 + i, i / 120) for i in range(80)], action="move", target="x", duration_s=0.6)
    yield server
    server.stop_server()


def _get(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers.get("Access-Control-Allow-Origin") is None
        return json.loads(response.read().decode())


def test_healthz_and_state(api):
    health = _get(api.port, "/healthz")
    assert health["ok"] is True and health["driver"] == "dry"
    assert "uptime_s" in health and "armed" in health
    state = _get(api.port, "/api/state")
    assert state["session"]["steps"] == 80
    assert state["recent"] and len(state["recent"][0]) == 2
    sessions = _get(api.port, "/api/sessions")
    assert sessions["ok"] is True and sessions["sessions"]
    path = _get(api.port, f"/api/sessions/{sessions['sessions'][0]['id']}/path")
    assert path["ok"] is True and path["points"]


def test_unknown_routes_404(api):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(api.port, "/api/nope")
    assert exc.value.code == 404
    with pytest.raises(urllib.error.HTTPError):
        _get(api.port, "/../etc/passwd")


def test_control_endpoints(api):
    api.guard.arm(30)
    req = urllib.request.Request(f"http://127.0.0.1:{api.port}/api/disarm", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=3) as response:
        payload = json.loads(response.read().decode())
    assert payload["armed"] is False
    req = urllib.request.Request(f"http://127.0.0.1:{api.port}/api/stop", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=3) as response:
        payload = json.loads(response.read().decode())
    assert payload["ok"] is True
    assert api.guard.sentinel_present() is True


def test_sse_stream_delivers_state(api):
    conn = http.client.HTTPConnection("127.0.0.1", api.port, timeout=3)
    conn.request("GET", "/api/stream")
    response = conn.getresponse()
    assert response.status == 200
    assert response.getheader("Content-Type").startswith("text/event-stream")
    line = response.readline().decode()
    assert line.startswith("data: ")
    payload = json.loads(line[6:])
    assert "session" in payload and "recent" in payload
    started = time.monotonic()
    response.readline()  # blank separator
    assert time.monotonic() - started < 2.0
    conn.close()
