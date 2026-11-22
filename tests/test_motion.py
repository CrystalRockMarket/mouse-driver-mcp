from __future__ import annotations

import math

import pytest

from mouse_driver import motion


def test_fitts_duration_bounds():
    assert motion.fitts_duration(0) == pytest.approx(motion.MIN_DURATION_S, abs=0.02)
    assert motion.fitts_duration(20) < motion.fitts_duration(1400)
    assert motion.fitts_duration(1e6) == pytest.approx(motion.MAX_DURATION_S)


def test_long_move_has_dense_points_and_settles():
    plan = motion.plan_move(300, 300, 1700, 900, seed=7)  # 1442 px
    assert len(plan.points) > 40
    assert plan.duration_s >= motion.MIN_DURATION_S
    assert (plan.points[-1].x, plan.points[-1].y) == (1700.0, 900.0)
    # Ordered in time, sampled near 8 ms.
    gaps = [b.t - a.t for a, b in zip(plan.points, plan.points[1:])]
    assert min(gaps) >= motion.SAMPLE_INTERVAL_S * 0.9
    assert max(gaps) < 0.12  # the largest gap is an overshoot corrective pause


def test_progress_is_monotonic_ish():
    plan = motion.plan_move(200, 200, 1600, 1000, seed=11)
    distances = [math.dist((p.x, p.y), plan.end) for p in plan.points]
    regressions = [a - b for a, b in zip(distances, distances[1:]) if b > a]
    assert not regressions or max(regressions) <= 6.0
    assert distances[-1] == 0.0
    assert distances[0] == pytest.approx(plan.distance_px)


def test_human_path_curves_away_from_straight_line():
    plan = motion.plan_move(0, 0, 1400, 0, seed=3)
    max_deviation = max(abs(p.y) for p in plan.points)
    assert max_deviation > 3.0  # curvature and/or tremor
    straight = motion.plan_move(0, 0, 1400, 0, seed=3, curve="straight")
    assert all(abs(p.y) < 0.01 for p in straight.points)


def test_overshoot_happens_across_seeds_and_stays_small():
    hits = [s for s in range(40) if motion.plan_move(100, 100, 1000, 800, seed=s).overshot]
    assert 1 <= len(hits) <= 20, hits
    for seed in hits[:5]:
        plan = motion.plan_move(100, 100, 1000, 800, seed=seed)
        assert 0.015 * plan.distance_px <= plan.overshoot_px <= 0.06 * plan.distance_px
        assert plan.settle_ms == 0  # no click approach on a plain move


def test_overshoot_has_pause_then_correction():
    seed = next(s for s in range(40) if motion.plan_move(100, 100, 1000, 800, seed=s).overshot)
    plan = motion.plan_move(100, 100, 1000, 800, seed=seed)
    gaps = [b.t - a.t for a, b in zip(plan.points, plan.points[1:])]
    assert max(gaps) >= 0.040  # the corrective pause
    assert plan.points[-1].x == 1000.0 and plan.points[-1].y == 800.0


def test_click_approach_slows_the_last_pixels():
    plan = motion.plan_move(100, 100, 1000, 700, click_approach=True, seed=5)
    assert plan.slow_final_px == pytest.approx(motion.FINAL_APPROACH_PX)
    assert 55 <= plan.settle_ms <= 120
    tail = plan.points[-6:]
    speeds = [
        math.dist((a.x, a.y), (b.x, b.y)) / max(1e-4, b.t - a.t) for a, b in zip(tail, tail[1:])
    ]
    peak = max(
        math.dist((a.x, a.y), (b.x, b.y)) / max(1e-4, b.t - a.t)
        for a, b in zip(plan.points[:20], plan.points[1:21])
    )
    assert min(speeds) < peak * 0.5


def test_explicit_duration_is_respected():
    plan = motion.plan_move(0, 0, 500, 0, duration_s=0.5, seed=1)
    assert plan.duration_s == pytest.approx(0.5, abs=0.02)


def test_scroll_plan_decays_and_sums():
    steps = motion.plan_scroll(12, seed=4)
    assert 4 <= len(steps) <= 9
    ticks = [s.ticks for s in steps]
    assert ticks[0] >= ticks[-1]
    assert sum(ticks) == 12
    assert all(0.015 <= s.gap_s <= 0.040 for s in steps)
    assert all(t >= 1 for t in ticks)


def test_zero_distance_move_is_safe():
    plan = motion.plan_move(500, 500, 500, 500, seed=2)
    assert plan.points[-1].x == 500.0
    assert plan.duration_s >= motion.MIN_DURATION_S
