from __future__ import annotations

import pytest

from mouse_driver import task as task_mod


def test_validation_defaults_and_bounds():
    model = task_mod.validate_task(
        {
            "name": "demo",
            "steps": [
                {"kind": "key", "value": "c"},
                {"kind": "wait", "seconds": 10},
                {"kind": "key", "value": "Return"},
            ],
            "repeat": {"mode": "infinite"},
            "interval_s": 0,
        }
    )
    assert model["repeat"]["mode"] == "infinite"
    assert model["steps"][1] == {"kind": "wait", "seconds": 10.0}

    with pytest.raises(task_mod.TaskError):
        task_mod.validate_task({"steps": []})
    with pytest.raises(task_mod.TaskError):
        task_mod.validate_task({"steps": [{"kind": "wait", "seconds": 0.05}]})
    with pytest.raises(task_mod.TaskError):
        task_mod.validate_task({"steps": [{"kind": "key", "value": ""}]})
    with pytest.raises(task_mod.TaskError):
        task_mod.validate_task({"steps": [{"kind": "key", "value": "c"}], "repeat": {"mode": "count", "count": 0}})
    with pytest.raises(task_mod.TaskError):
        task_mod.validate_task({"steps": [{"kind": "key", "value": "c"}], "interval_s": 4000})


class FakeGuard:
    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.last = "run"

    def action_state(self):
        if self.sequence:
            self.last = self.sequence.pop(0)
        return self.last


class FakeService:
    def __init__(self, sequence=()):
        self.guard = FakeGuard(sequence)
        self.events = []

    def key(self, value):
        self.events.append(("key", value))

    def type_text(self, value):
        self.events.append(("type", value))


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _model(**overrides):
    data = {
        "steps": [{"kind": "key", "value": "c"}, {"kind": "wait", "seconds": 0.3}, {"kind": "key", "value": "Return"}],
        "repeat": {"mode": "count", "count": 2},
        "interval_s": 0.2,
    }
    data.update(overrides)
    return task_mod.validate_task(data)


def test_count_mode_runs_exact_cycles():
    service = FakeService()
    clock = FakeClock()
    runner = task_mod.TaskRunner(service, _model(), clock=clock, sleeper=clock.sleep)
    runner.run()
    status = runner.snapshot()
    assert status["cycles_done"] == 2
    assert status["running"] is False
    assert [e for e in service.events if e == ("key", "c")] == [("key", "c"), ("key", "c")]
    assert status["last_result"] == "finished"


def test_duration_mode_stops_at_deadline():
    clock = FakeClock()
    service = FakeService()
    runner = task_mod.TaskRunner(
        service,
        _model(repeat={"mode": "duration", "minutes": 1}, interval_s=0),
        clock=clock,
        sleeper=clock.sleep,
    )
    runner.run()
    assert runner.snapshot()["cycles_done"] >= 1
    assert runner.snapshot()["last_result"] == "duration cap reached"


def test_pause_freezes_then_resumes():
    clock = FakeClock()
    service = FakeService(sequence=["run", "pause", "pause", "run"])
    runner = task_mod.TaskRunner(
        service, _model(repeat={"mode": "count", "count": 1}, interval_s=0), clock=clock, sleeper=clock.sleep
    )
    runner.run()
    assert runner.snapshot()["cycles_done"] == 1
    assert [e for e in service.events if e[0] == "key"] == [("key", "c"), ("key", "Return")]


def test_stops_when_guard_goes_stop():
    clock = FakeClock()
    service = FakeService(sequence=["run", "stop"])
    runner = task_mod.TaskRunner(
        service, _model(repeat={"mode": "infinite"}, interval_s=0), clock=clock, sleeper=clock.sleep
    )
    runner.run()
    assert runner.snapshot()["running"] is False
    assert runner.snapshot()["last_result"].startswith("stopped")
    assert service.events == []


def test_hard_cycle_cap_is_recorded():
    clock = FakeClock()
    service = FakeService()
    model = _model(repeat={"mode": "infinite"}, interval_s=0, steps=[{"kind": "key", "value": "c"}])
    runner = task_mod.TaskRunner(service, model, clock=clock, sleeper=clock.sleep)
    runner.status["cycles_done"] = task_mod.MAX_CYCLES
    runner.run()
    assert runner.snapshot()["last_result"] == "cycle cap reached"
