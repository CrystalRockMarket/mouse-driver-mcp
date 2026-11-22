from __future__ import annotations

import json

import pytest

from mouse_driver import hud_layout
from mouse_driver.control import HUD_PATH, MouseService


@pytest.fixture()
def service():
    svc = MouseService(dry_run=True, start_api=False)
    svc.guard.unlock()
    svc.guard.clear_sentinel()
    svc.guard.disarm("test setup")
    yield svc
    svc.guard.disarm("teardown")
    svc.close()


def test_scale_clamps_both_ends(service):
    service.set_hud_visible(True)
    big = service.set_hud_scale(5.0)
    assert big["hud"]["scale"] == pytest.approx(1.8)
    small = service.set_hud_scale(0.1)
    assert small["hud"]["scale"] == pytest.approx(0.85)


def test_scale_keeps_mode_visibility_and_offset(service):
    service.set_hud_offset(80, 40, "custom", 1900, 100)
    service.set_hud_visible(True)
    service.set_hud_scale(1.4)
    on_disk = json.loads(HUD_PATH.read_text())
    assert on_disk["mode"] == "custom"
    assert on_disk["visible"] is True
    assert (on_disk["offset_x"], on_disk["offset_y"]) == (80, 40)
    assert on_disk["scale"] == pytest.approx(1.4)
    state = service.api.build_state()
    assert state["hud"]["scale"] == pytest.approx(1.4)
    assert state["hud"]["mode"] == "custom"
    assert state["hud_visible"] is True
    service.set_hud_scale(1.0)


def test_offset_persists_and_reset_returns_auto(service):
    result = service.set_hud_offset(120, 60, "custom", 2000, 200)
    assert result["hud"]["mode"] == "custom"
    assert (result["hud"]["offset_x"], result["hud"]["offset_y"]) == (120, 60)
    assert (result["hud"]["x"], result["hud"]["y"]) == (2000, 200)
    on_disk = json.loads(HUD_PATH.read_text())
    assert on_disk["offset_x"] == 120 and on_disk["offset_y"] == 60
    reset = service.reset_hud_position()
    assert reset["hud"]["mode"] == "auto"
    assert (reset["hud"]["offset_x"], reset["hud"]["offset_y"]) == (12, 12)
    assert service.api.build_state()["hud"]["mode"] == "auto"


def test_follow_geometry_tracks_a_moved_target():
    target = (3, 3, 2554, 1434)
    panel = (430, 184)
    assert hud_layout.panel_position(target, panel, (12, 12)) == (3 + 2554 - 430 - 12, 15)
    moved = (500, 200, 2554, 1434)
    x, y = hud_layout.panel_position(moved, panel, (12, 12))
    assert x == 500 + 2554 - 430 - 12 and y == 212
    assert x > 0


def test_offset_clamps_inside_target():
    assert hud_layout.clamp_offset((0, 0), (1200, 800), (430, 184)) == (12, 12)
    assert hud_layout.clamp_offset((9999, 9999), (1200, 800), (430, 184)) == (758, 604)
    # target smaller than the panel: align top-left, overhang only as needed
    assert hud_layout.panel_position((100, 100, 300, 150), (430, 184), (12, 12)) == (100, 100)


def test_offset_round_trip():
    target = (3, 3, 2554, 1434)
    panel = (430, 184)
    x, y = hud_layout.panel_position(target, panel, (120, 60))
    assert hud_layout.offset_for_position(target, panel, x, y) == (120, 60)


def test_pause_button_mapping():
    assert hud_layout.pause_button(True, False, False) == {
        "glyph": "⏸", "enabled": True, "tone": "idle", "tooltip": "Pause"
    }
    assert hud_layout.pause_button(True, True, False)["glyph"] == "▶"
    assert hud_layout.pause_button(True, True, False)["tone"] == "active"
    assert hud_layout.pause_button(True, True, False)["tooltip"] == "Resume"
    assert hud_layout.pause_button(False, False, False)["enabled"] is False
    assert hud_layout.pause_button(True, False, True)["enabled"] is False
    assert hud_layout.pause_button(False, True, False)["enabled"] is False
