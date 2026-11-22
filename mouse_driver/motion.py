"""Human motion engine.

The model is deliberately small and testable:

* **Duration** comes from Fitts' law: ``0.12 + 0.22 * log2(d/20 + 1)`` seconds,
  clamped to 0.18–1.60 s. A caller may override it.
* **Path** is a cubic Bezier whose two control points are pushed perpendicular
  to the straight line by a random amount that grows with distance.
* **Speed** follows the minimum-jerk profile ``s = 10t^3 - 15t^4 + 6t^5``, so
  the pointer accelerates out of rest and settles into the target.
* **Tremor** of 1.5–3 px is added perpendicular to travel and fades to zero at
  both ends.
* **Overshoot** happens on ~12 % of moves: 2–4 % past the target, a 40–90 ms
  pause, then a short corrective sub-move.
* **Click approach** slows the final 24 px and returns a settle pause before
  the button goes down.

Point samples are emitted every ``SAMPLE_INTERVAL_S`` (120 Hz). Everything is
pure: no X calls, no sleeping, so the planner is unit-testable and the driver
handles timing.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

SAMPLE_INTERVAL_S = 1.0 / 120.0  # 8.3 ms; ~8 ms per emitted point
MIN_DURATION_S = 0.18
MAX_DURATION_S = 1.60
FITTS_BASE_S = 0.12
FITTS_GAIN = 0.22
FINAL_APPROACH_PX = 24.0
OVERSHOOT_PROBABILITY = 0.12


@dataclass(frozen=True)
class Point:
    """One pointer sample. ``t`` is seconds from the start of the move."""

    x: float
    y: float
    t: float

    def as_list(self) -> list[float]:
        return [round(self.x, 2), round(self.y, 2), round(self.t, 4)]


@dataclass
class MovePlan:
    start: tuple[float, float]
    end: tuple[float, float]
    duration_s: float
    points: list[Point] = field(default_factory=list)
    overshoot_px: float = 0.0
    settle_ms: int = 0
    seed: int | None = None
    curve: str = "human"
    slow_final_px: float = 0.0

    @property
    def distance_px(self) -> float:
        return math.dist(self.start, self.end)

    @property
    def path_length_px(self) -> float:
        total = 0.0
        prev = self.start
        for point in self.points:
            total += math.dist(prev, (point.x, point.y))
            prev = (point.x, point.y)
        return total

    @property
    def overshot(self) -> bool:
        return self.overshoot_px > 0.0

    def summary(self) -> dict:
        return {
            "from": [round(self.start[0], 2), round(self.start[1], 2)],
            "to": [round(self.end[0], 2), round(self.end[1], 2)],
            "points": len(self.points),
            "duration_s": round(self.duration_s, 3),
            "distance_px": round(self.distance_px, 1),
            "path_px": round(self.path_length_px, 1),
            "overshoot_px": round(self.overshoot_px, 1),
            "settle_ms": self.settle_ms,
            "curve": self.curve,
            "seed": self.seed,
        }


def fitts_duration(distance_px: float) -> float:
    """Fitts' law duration in seconds for a straight-line distance."""
    distance = max(0.0, float(distance_px))
    raw = FITTS_BASE_S + FITTS_GAIN * math.log2(distance / 20.0 + 1.0)
    return min(MAX_DURATION_S, max(MIN_DURATION_S, raw))


def min_jerk(t: float) -> float:
    """Normalised minimum-jerk progress, 0 at t=0 and 1 at t=1."""
    t = min(1.0, max(0.0, t))
    return 10.0 * t**3 - 15.0 * t**4 + 6.0 * t**5


def _bezier(
    p0: tuple[float, float],
    c1: tuple[float, float],
    c2: tuple[float, float],
    p3: tuple[float, float],
    u: float,
) -> tuple[float, float]:
    v = 1.0 - u
    a = v * v * v
    b = 3.0 * v * v * u
    c = 3.0 * v * u * u
    d = u * u * u
    return (
        a * p0[0] + b * c1[0] + c * c2[0] + d * p3[0],
        a * p0[1] + b * c1[1] + c * c2[1] + d * p3[1],
    )


class _Tremor:
    """Smooth pseudo-noise: two out-of-phase sinusoids, bounded to amplitude."""

    def __init__(self, rng: random.Random, amplitude_px: float) -> None:
        self.amplitude = amplitude_px
        self.f1 = rng.uniform(6.0, 11.0)
        self.f2 = rng.uniform(15.0, 24.0)
        self.p1 = rng.uniform(0.0, math.tau)
        self.p2 = rng.uniform(0.0, math.tau)

    def value(self, t: float) -> float:
        raw = 0.62 * math.sin(math.tau * self.f1 * t + self.p1) + 0.38 * math.sin(
            math.tau * self.f2 * t + self.p2
        )
        return self.amplitude * raw


def _sample_segment(
    rng: random.Random,
    p0: tuple[float, float],
    p3: tuple[float, float],
    duration_s: float,
    t_offset: float,
    curvature: float,
    tremor_px: float,
    out: list[Point],
) -> None:
    """Append sampled points for one eased Bezier segment."""
    dx, dy = p3[0] - p0[0], p3[1] - p0[1]
    dist = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / dist, dx / dist  # unit normal
    # Curvature grows with distance but stays subtle: at most 16 % of travel.
    reach = min(0.16, curvature * min(1.0, dist / 600.0))
    side = rng.choice((-1.0, 1.0))
    off1 = side * rng.uniform(0.45, 1.0) * reach * dist
    off2 = side * rng.uniform(0.45, 1.0) * reach * dist
    c1 = (p0[0] + dx / 3.0 + nx * off1, p0[1] + dy / 3.0 + ny * off1)
    c2 = (p0[0] + 2.0 * dx / 3.0 + nx * off2, p0[1] + 2.0 * dy / 3.0 + ny * off2)
    tremor = _Tremor(rng, tremor_px) if tremor_px > 0 else None

    steps = max(3, int(round(duration_s / SAMPLE_INTERVAL_S)) + 1)
    for i in range(steps):
        frac = i / (steps - 1)
        u = min_jerk(frac)
        x, y = _bezier(p0, c1, c2, p3, u)
        if tremor is not None and 0.0 < frac < 1.0:
            envelope = math.sin(math.pi * frac) ** 0.8
            nudge = tremor.value(t_offset + frac * duration_s) * envelope
            x += nx * nudge
            y += ny * nudge
        out.append(Point(x, y, t_offset + frac * duration_s))


def plan_move(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    *,
    duration_s: float | None = None,
    curve: str = "human",
    seed: int | None = None,
    click_approach: bool = False,
    slow_final_px: float = 0.0,
) -> MovePlan:
    """Plan a human-shaped move from ``(x0, y0)`` to ``(x1, y1)``.

    ``curve="straight"`` skips curvature and tremor (useful for tests and for
    callers that need a predictable line). Any other value plans a human move.
    """
    rng = random.Random(seed)
    start = (float(x0), float(y0))
    end = (float(x1), float(y1))
    dist = math.dist(start, end)
    human = curve != "straight"

    base_duration = fitts_duration(dist) if duration_s is None else float(duration_s)
    base_duration = min(3.0, max(0.12, base_duration))

    slow_px = float(slow_final_px or (FINAL_APPROACH_PX if click_approach else 0.0))
    if dist <= slow_px * 1.5:
        slow_px = 0.0

    points: list[Point] = [Point(start[0], start[1], 0.0)]
    plan = MovePlan(
        start=start,
        end=end,
        duration_s=base_duration,
        points=points,
        seed=seed,
        curve=curve,
        slow_final_px=slow_px,
    )

    # Split the journey: a long eased approach, then (optionally) the slow last
    # few pixels into the target so the button lands calmly.
    if slow_px:
        approach_t = (end[0] + (start[0] - end[0]) * slow_px / dist,
                      end[1] + (start[1] - end[1]) * slow_px / dist)
        main_duration = base_duration * 0.78
        final_duration = max(0.09, base_duration * 0.22)
    else:
        approach_t = end
        main_duration = base_duration
        final_duration = 0.0

    overshoot_target = approach_t
    if human and dist > 40.0 and rng.random() < OVERSHOOT_PROBABILITY:
        over_px = dist * rng.uniform(0.02, 0.04)
        ux, uy = (approach_t[0] - start[0]) / dist, (approach_t[1] - start[1]) / dist
        overshoot_target = (
            approach_t[0] + ux * over_px + rng.uniform(-2.0, 2.0),
            approach_t[1] + uy * over_px + rng.uniform(-2.0, 2.0),
        )
        plan.overshoot_px = abs(over_px)
        corrective_s = rng.uniform(0.07, 0.12)
        pause_s = rng.uniform(0.040, 0.090)
        main_duration = max(0.10, main_duration - corrective_s - pause_s)
    else:
        corrective_s = 0.0
        pause_s = 0.0

    tremor_px = rng.uniform(1.5, 3.0) if human else 0.0
    _sample_segment(
        rng,
        start,
        overshoot_target,
        main_duration,
        0.0,
        0.9 if human else 0.0,
        tremor_px,
        points,
    )
    t_cursor = main_duration

    if plan.overshoot_px:
        if pause_s > 0:
            points.append(Point(overshoot_target[0], overshoot_target[1], t_cursor + pause_s))
        t_cursor += pause_s
        _sample_segment(
            rng,
            overshoot_target,
            approach_t,
            corrective_s,
            t_cursor,
            0.0,
            tremor_px * 0.35,
            points,
        )
        t_cursor += corrective_s

    if slow_px:
        _sample_segment(
            rng,
            approach_t,
            end,
            final_duration,
            t_cursor,
            0.0,
            tremor_px * 0.2,
            points,
        )
        t_cursor += final_duration
        plan.settle_ms = int(rng.uniform(55, 120)) if human else 40

    # Guarantee the path terminates exactly on the requested pixel.
    if not points or math.dist((points[-1].x, points[-1].y), end) > 1e-9:
        last_t = points[-1].t if points else 0.0
        points.append(Point(end[0], end[1], max(t_cursor, last_t + 0.0005)))

    # Segment joins can repeat a timestamp; keep strictly increasing samples.
    clean = [points[0]]
    for point in points[1:]:
        if point.t - clean[-1].t > 1e-9:
            clean.append(point)
        else:
            clean[-1] = point
    plan.points = clean
    plan.duration_s = round(clean[-1].t, 4)
    return plan


@dataclass(frozen=True)
class ScrollStep:
    ticks: int
    gap_s: float


def plan_scroll(dy: int, dx: int = 0, *, seed: int | None = None) -> list[ScrollStep]:
    """Split a wheel delta into 4–9 decaying events with 15–40 ms gaps."""
    rng = random.Random(seed)
    total = abs(int(dy))
    horizontal = abs(int(dx))
    events = rng.randint(4, 9)
    if horizontal > total:
        total = horizontal

    # Decaying weights, normalised so the steps sum to the requested delta.
    weights = [1.0 / (i + 1.35) ** 0.55 for i in range(events)]
    scale = total / sum(weights) if total else 0.0
    raw = [w * scale for w in weights]
    ticks = [max(1, int(round(v))) for v in raw] if total else [0] * events
    if total:
        # Fix rounding drift on the final event (decay still ends small).
        drift = total - sum(ticks)
        ticks[-1] = max(1, ticks[-1] + drift)
    return [ScrollStep(ticks=ticks[i], gap_s=rng.uniform(0.015, 0.040)) for i in range(events)]
