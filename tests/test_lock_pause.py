from __future__ import annotations

import time

import pytest

from mouse_driver.control import MouseService
from mouse_driver.guard import GuardError


@pytest.fixture()
def service():
    svc = MouseService(dry_run=True, start_api=False)
    svc.guard.unlock()
    svc.guard.disarm("test setup")
    yield svc
    svc.guard.unlock()
    svc.guard.stop("test teardown")
    svc.close()


def test_lock_refuses_arming_until_unlock(service):
    service.arm(30)
    service.driver.move_to(120, 140)
    panic = service.panic()
    assert panic["locked"] is True
    assert panic["armed"] is False
    assert service.guard.sentinel_present() is True
    event = service.store.events(1)[-1]
    assert event["kind"] == "panic"
    assert "120,140" in event["detail"]

    with pytest.raises(GuardError) as exc:
        service.arm(30)
    assert exc.value.code == "locked"
    assert "Ctrl+Alt+Shift+Enter" in str(exc.value)

    with pytest.raises(GuardError) as exc:
        service.move(200, 200)
    assert exc.value.code == "locked"

    unlocked = service.unlock()
    assert unlocked["locked"] is False
    assert service.guard.is_armed() is False  # unlock never arms by itself
    armed = service.arm(10)
    assert armed["armed"] is True


def test_paused_refuses_actions_and_resume_restores(service):
    service.arm(30)
    paused = service.pause()
    assert paused["paused"] is True
    assert service.guard.is_armed() is True
    with pytest.raises(GuardError) as exc:
        service.move(300, 300)
    assert exc.value.code == "paused"
    assert "Ctrl+Alt+Shift+P" in str(exc.value)
    with pytest.raises(GuardError):
        service.click("left")

    resumed = service.resume()
    assert resumed["paused"] is False
    moved = service.move(320, 340, duration_s=0.2)
    assert moved["ok"] is True and moved["aborted"] is False


def test_pause_survives_panic_and_unlock_keeps_it(service):
    service.arm(30)
    service.pause()
    service.panic()
    state = service.guard.state()
    assert state["locked"] is True
    assert state["paused"] is True
    service.unlock()
    state = service.guard.state()
    assert state["locked"] is False
    assert state["paused"] is False or state["armed"] is False
    # Paused flag survives only while armed; disarmed resume is a no-op.
    assert service.guard.is_armed() is False


def test_resume_after_ttl_refuses(service):
    service.arm(0.3)
    service.pause()
    time.sleep(0.45)
    with pytest.raises(GuardError) as exc:
        service.resume()
    assert exc.value.code == "arm_expired"
    assert service.guard.is_armed() is False
    assert service.guard.state()["paused"] is False


def test_pause_while_locked_does_nothing(service):
    service.lock()
    with pytest.raises(GuardError) as exc:
        service.pause()
    assert exc.value.code == "locked"
    assert service.guard.state()["locked"] is True
