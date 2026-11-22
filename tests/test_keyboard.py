from __future__ import annotations

import pytest

from mouse_driver.control import MouseService


@pytest.fixture()
def service():
    svc = MouseService(dry_run=True, start_api=False)
    svc.guard.unlock()
    svc.guard.clear_sentinel()
    svc.guard.disarm("test setup")
    yield svc
    svc.guard.disarm("teardown")
    svc.close()


def test_key_records_combo_parts(service):
    service.arm(30)
    service.key("ctrl+shift+t")
    keys = service.keys_recent()
    assert keys[-1]["key"] == "Ctrl+Shift+T"
    assert keys[-1]["source"] == "driver"
    assert {"key", "code", "t", "source"} == set(keys[-1])


def test_type_records_each_character(service):
    service.arm(30)
    service.type_text("Hi 2")
    labels = [event["key"] for event in service.keys_recent()[-4:]]
    assert labels == ["H", "I", "Space", "2"]


def test_keyboard_toggle_and_state(service):
    before = service.api.build_state()["keyboard_expanded"]
    toggled = service.toggle_keyboard()
    assert toggled["keyboard_expanded"] is (not before)
    state = service.api.build_state()
    assert state["keyboard_expanded"] is (not before)
    assert isinstance(state["keys_recent"], list)
    service.toggle_keyboard()
    assert service.api.build_state()["keyboard_expanded"] is before


def test_actions_refused_when_paused(service):
    service.arm(30)
    service.pause()
    service.keys_recent()  # buffer stays readable
    with pytest.raises(Exception):
        service.key("ctrl+t")
