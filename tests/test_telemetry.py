from __future__ import annotations

import json
import time

from mouse_driver.motion import Point


def _points(count=60, step=1 / 120):
    return [Point(100 + i, 100 + i * 0.5, i * step) for i in range(count)]


def test_state_shape(store, guard, driver):
    from mouse_driver.telemetry import TelemetryAPI

    api = TelemetryAPI(store, guard=guard, driver=driver, port=0)
    store.record_move(_points(), action="move", target="160,130", duration_s=0.5)
    store.add_event("move", "160,130")
    state = api.build_state()
    assert set(state) == {
        "armed",
        "arm_expires",
        "cursor",
        "screen",
        "session",
        "recent",
        "events",
        "target",
        "locked",
        "lock_reason",
        "paused",
        "driver",
        "keyboard_expanded",
        "keys_recent",
        "hud_visible",
        "hud",
    }
    assert state["target"] is None
    assert state["hud"]["mode"] in {"auto", "custom"}
    assert 0.85 <= state["hud"]["scale"] <= 1.8
    assert set(state["cursor"]) == {"x", "y"}
    assert set(state["screen"]) == {"w", "h", "monitors"}
    assert state["screen"] == {"w": 5120, "h": 1440, "monitors": 2}
    session = state["session"]
    for key in (
        "id",
        "started",
        "steps",
        "distance_px",
        "avg_speed",
        "max_speed",
        "last_action",
        "last_target",
    ):
        assert key in session
    assert session["steps"] == 60
    assert session["distance_px"] > 0
    assert isinstance(state["recent"], list) and state["recent"]
    assert isinstance(state["recent"][0], list) and len(state["recent"][0]) == 2
    assert state["events"][-1]["kind"] == "move"
    assert set(state["events"][0]) == {"t", "kind", "detail"}
    assert state["armed"] is False and state["arm_expires"] is None


def test_armed_state_reflects_guard(store, guard, driver):
    from mouse_driver.telemetry import TelemetryAPI

    api = TelemetryAPI(store, guard=guard, driver=driver, port=0)
    guard.arm(30)
    assert api.build_state()["armed"] is True
    assert api.build_state()["arm_expires"]
    guard.disarm("test")


def test_sessions_and_path_downsampling(store):
    session_id = store.start_session()
    for _ in range(4):
        store.record_move(_points(300), action="move", target="x", duration_s=2.3, session_id=session_id)
    sessions = store.sessions(20)
    assert sessions and sessions[0]["id"] == session_id
    assert len(store.sessions(20)) <= 20
    path = store.session_path(session_id, max_points=200)
    assert path["ok"] is True
    assert len(path["points"]) <= 200
    assert path["total_points"] == 1200
    missing = store.session_path(99999)
    assert missing["ok"] is False


def test_recent_window_is_two_seconds(store):
    store.record_move(_points(50), action="move", target="x", duration_s=0.4)
    assert store.recent_points(2.0)
    stale = store.recent_points(0.0001)
    assert len(stale) <= 1  # only the sample stamped "now" can match


def test_retention_and_events(store):
    store.add_event("arm", "ttl=300")
    store.add_event("move", "10,10")
    events = store.events(3)
    assert [e["kind"] for e in events] == ["arm", "move"]
    time.sleep(0.01)
    assert store.session()["id"]


def test_hud_visibility_persists_to_disk(tmp_path, monkeypatch):
    import mouse_driver.control as control

    hud = tmp_path / "hud.json"
    monkeypatch.setattr(control, "HUD_PATH", hud)
    monkeypatch.setattr(control, "_HUD_CACHE", {"mtime": -1.0, "state": dict(control.HUD_DEFAULTS)})

    assert control.read_hud_state()["visible"] is True
    result = control.write_hud_state(visible=False)
    assert result["hud"]["visible"] is False
    assert hud.exists()
    # A fresh read (new process) must keep it closed.
    monkeypatch.setattr(control, "_HUD_CACHE", {"mtime": -1.0, "state": dict(control.HUD_DEFAULTS)})
    assert control.read_hud_state()["visible"] is False
    assert json.loads(hud.read_text())["visible"] is False


def test_hud_manual_position_and_reset(tmp_path, monkeypatch):
    import mouse_driver.control as control

    hud = tmp_path / "hud.json"
    monkeypatch.setattr(control, "HUD_PATH", hud)
    monkeypatch.setattr(control, "_HUD_CACHE", {"mtime": -1.0, "state": dict(control.HUD_DEFAULTS)})

    control.write_hud_state(x=2265, y=90, mode="custom", offset_x=40, offset_y=60)
    state = control.read_hud_state()
    assert (state["x"], state["y"], state["mode"]) == (2265, 90, "custom")
    assert (state["offset_x"], state["offset_y"]) == (40, 60)

    control.write_hud_state(mode="auto")
    state = control.read_hud_state()
    assert state["mode"] == "auto"
    assert state["x"] == 2265  # position is remembered for the next manual drag


def test_hud_visible_survives_restart_of_the_reader(tmp_path, monkeypatch):
    import mouse_driver.control as control

    hud = tmp_path / "hud.json"
    monkeypatch.setattr(control, "HUD_PATH", hud)
    monkeypatch.setattr(control, "_HUD_CACHE", {"mtime": -1.0, "state": dict(control.HUD_DEFAULTS)})
    control.write_hud_state(visible=False)

    # Simulate a HUD restart: fresh cache, same file.
    monkeypatch.setattr(control, "_HUD_CACHE", {"mtime": -1.0, "state": dict(control.HUD_DEFAULTS)})
    assert control.read_hud_state()["visible"] is False
    # Reopening is deliberate.
    control.write_hud_state(visible=True)
    assert control.read_hud_state()["visible"] is True


def test_target_file_is_shared_between_instances(tmp_path, monkeypatch):
    import mouse_driver.control as control

    path = tmp_path / "target.json"
    monkeypatch.setattr(control, "TARGET_PATH", path)

    service = object.__new__(control.MouseService)
    service._target_id = 0
    assert service._load_target() == 0

    path.write_text('{"id": 33554435, "updated": 1}\n')
    assert service._load_target() == 33554435

    # Another instance writes a different window; the first one must see it.
    path.write_text('{"id": 27262979, "updated": 2}\n')
    assert service._load_target() == 27262979
