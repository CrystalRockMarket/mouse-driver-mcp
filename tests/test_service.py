from __future__ import annotations

import pytest

from mouse_driver.control import MouseService
from mouse_driver.guard import GuardError


@pytest.fixture()
def service():
    svc = MouseService(dry_run=True, start_api=False)
    svc.guard.unlock()
    svc.guard.clear_sentinel()
    svc.guard.disarm("test setup")
    yield svc
    svc.guard.stop("test teardown")
    svc.close()


def test_pointer_actions_refused_when_disarmed(service):
    with pytest.raises(GuardError) as exc:
        service.move(100, 100)
    assert exc.value.code == "not_armed"
    with pytest.raises(GuardError):
        service.click("left")
    with pytest.raises(GuardError):
        service.scroll(3)
    with pytest.raises(GuardError):
        service.type_text("hello")
    with pytest.raises(GuardError):
        service.key("Return")
    with pytest.raises(GuardError):
        service.focus_window("xterm")


def test_move_after_arm_records_path(service):
    service.arm(30)
    result = service.move(700, 500, duration_s=0.35)
    assert result["ok"] is True
    assert result["points"] > 20
    assert result["aborted"] is False
    assert result["cursor"] == {"x": 700, "y": 500}
    assert service.driver.recorded[-1] == (700, 500)
    state = service.api.build_state()
    assert state["session"]["steps"] == result["points"]
    assert state["session"]["last_action"] == "move"
    assert state["session"]["last_target"] == "700,500"


def test_bounds_are_rejected(service):
    service.arm(30)
    with pytest.raises(GuardError) as exc:
        service.move(6000, 200)
    assert exc.value.code == "out_of_bounds"
    with pytest.raises(GuardError):
        service.move(200, -1)


def test_stop_halts_and_blocks(service):
    service.arm(30)
    service.stop()
    assert service.guard.sentinel_present() is True
    with pytest.raises(GuardError) as exc:
        service.move(400, 400)
    assert exc.value.code == "stopped"


def test_click_and_scroll_in_dry_mode(service):
    service.arm(30)
    click = service.click("left", 1)
    assert click["clicked"] is True
    assert 35 <= click["hold_ms"][0] <= 90
    at = service.click("right", 1, 520, 420)
    assert at["clicked"] is True
    assert at["cursor"] == {"x": 520, "y": 420}
    scroll = service.scroll(-6)
    assert 4 <= scroll["events"] <= 9
    assert service.type_text("hello")["chars"] == 5
    assert service.key("Return")["sent"] is True


def test_telemetry_payload_and_replay(service):
    service.arm(30)
    service.move(600, 400, duration_s=0.25)
    payload = service.telemetry(5)
    assert payload["ok"] is True
    assert payload["sessions"]
    session_id = payload["sessions"][0]["id"]
    replay = service.replay_session(session_id, speed=4.0)
    assert replay["ok"] is True
    assert replay["points"] > 0
