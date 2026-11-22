from __future__ import annotations

import time

import pytest

from mouse_driver.guard import GuardError


def test_refuses_when_disarmed(guard):
    with pytest.raises(GuardError) as exc:
        guard.require_armed("mouse_move")
    assert exc.value.code == "not_armed"
    assert "not armed" in str(exc.value).lower()
    assert guard.is_armed() is False


def test_arm_disarm_cycle(guard):
    result = guard.arm(5)
    assert result["armed"] is True
    assert guard.is_armed() is True
    guard.require_armed("mouse_move")
    guard.disarm("test")
    assert guard.is_armed() is False
    with pytest.raises(GuardError):
        guard.require_armed("mouse_click")


def test_ttl_is_bounded(guard):
    with pytest.raises(GuardError) as exc:
        guard.arm(901)
    assert exc.value.code == "bad_ttl"
    with pytest.raises(GuardError):
        guard.arm(0)
    assert guard.arm(900)["ttl_s"] == 900


def test_arm_expires(guard):
    guard.arm(0.2)
    time.sleep(0.3)
    assert guard.is_armed() is False
    with pytest.raises(GuardError) as exc:
        guard.require_armed("mouse_move")
    assert exc.value.code == "arm_expired"


def test_stop_writes_sentinel_and_blocks(guard):
    guard.arm(30)
    guard.stop("test stop")
    assert guard.sentinel_present() is True
    assert guard.is_armed() is False
    with pytest.raises(GuardError) as exc:
        guard.require_armed("mouse_move")
    assert exc.value.code == "stopped"
    # Only arm clears the sentinel.
    guard.arm(5)
    assert guard.sentinel_present() is False
    assert guard.is_armed() is True


def test_stop_state_is_shared_across_instances(guard, tmp_path):
    second = type(guard)(
        state_path=guard.state_path, stop_path=guard.stop_path, lock_path=guard.lock_path
    )
    guard.arm(30)
    second.stop("cross process")
    assert guard.is_armed() is False
    assert guard.state()["stop_sentinel"] is True


def test_bounds_reject_and_clamp(guard):
    screen = (5120, 1440)
    assert guard.check_point(0, 0, screen) == (0, 0)
    assert guard.check_point(5119, 1439, screen) == (5119, 1439)
    with pytest.raises(GuardError) as exc:
        guard.check_point(5200, 100, screen)
    assert exc.value.code == "out_of_bounds"
    with pytest.raises(GuardError):
        guard.check_point(-5, 100, screen)
    assert guard.clamp_point(9999, -20, screen) == (5119, 0)


def test_state_shape(guard):
    guard.arm(60)
    state = guard.state()
    for key in ("armed", "expires", "arm_expires", "ttl_remaining_s", "stop_sentinel"):
        assert key in state
    assert 55 <= state["ttl_remaining_s"] <= 60
