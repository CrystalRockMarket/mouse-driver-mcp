"""Pure geometry and state helpers for the HUD (no GTK, easy to test).

The panel is always anchored to the target window by a per-target offset:

* ``offset_x`` is the distance from the target's right edge to the panel's right
  edge (12 px = 12 px inside the window), ``offset_y`` from the top edge.
* The offset is clamped so the panel stays inside the target; when the target is
  smaller than the panel the panel aligns to the target's top-left and may
  overhang only as far as it must.
"""
from __future__ import annotations

DEFAULT_INSET = 12


def clamp_offset(offset: tuple[float, float], target_size: tuple[int, int],
                 panel_size: tuple[int, int]) -> tuple[int, int]:
    width, height = panel_size
    target_w, target_h = target_size
    max_x = target_w - width - DEFAULT_INSET
    max_y = target_h - height - DEFAULT_INSET
    offset_x = max(DEFAULT_INSET, min(int(round(offset[0])), max(DEFAULT_INSET, max_x)))
    offset_y = max(DEFAULT_INSET, min(int(round(offset[1])), max(DEFAULT_INSET, max_y)))
    return offset_x, offset_y


def panel_position(target: tuple[int, int, int, int], panel_size: tuple[int, int],
                   offset: tuple[float, float]) -> tuple[int, int]:
    """Screen position for the panel, inside the target rect where possible."""
    tx, ty, tw, th = (int(v) for v in target)
    width, height = panel_size
    offset_x, offset_y = clamp_offset(offset, (tw, th), (width, height))
    x = tx + tw - width - offset_x
    y = ty + offset_y
    if width > tw:
        x = tx
    if height > th:
        y = ty
    return int(x), int(y)


def offset_for_position(target: tuple[int, int, int, int], panel_size: tuple[int, int],
                        x: int, y: int) -> tuple[int, int]:
    """Convert a dropped top-left back into a clamped in-target offset."""
    tx, ty, tw, th = (int(v) for v in target)
    width, height = panel_size
    offset = (tx + tw - int(x) - width, int(y) - ty)
    return clamp_offset(offset, (tw, th), (width, height))


def pause_button(armed: bool, paused: bool, locked: bool) -> dict:
    """Glyph, tone and clickability of the header pause button."""
    if locked or not armed:
        return {"glyph": "\u23f8", "enabled": False, "tone": "off", "tooltip": "Pause unavailable"}
    if paused:
        return {"glyph": "\u25b6", "enabled": True, "tone": "active", "tooltip": "Resume"}
    return {"glyph": "\u23f8", "enabled": True, "tone": "idle", "tooltip": "Pause"}
