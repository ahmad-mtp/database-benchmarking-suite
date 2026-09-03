"""The open-loop arrival schedule (PLAN.md S10).

The property that matters is not statistical, it is structural: an arrival's
time must be a function of its index and nothing else. That is what stops a
slow server from being rewarded with fewer requests, which is the mechanism
behind coordinated omission.
"""

from __future__ import annotations

import math
import statistics

import pytest

from dsel.driver.scheduler import ArrivalSchedule, exponential_interval, uniform


def test_the_schedule_is_a_function_of_the_index_alone() -> None:
    """Re-derivable months later, on another machine, one arrival at a time."""
    schedule = ArrivalSchedule(rate_per_s=500.0, worker=1, workers=4, seed=7)
    walked = list(schedule.offsets(200))
    assert walked == list(
        ArrivalSchedule(rate_per_s=500.0, worker=1, workers=4, seed=7).offsets(200)
    )
    for index in (0, 17, 199):
        assert schedule.offset(index) == pytest.approx(walked[index], rel=1e-12)


def test_different_workers_draw_different_schedules() -> None:
    """Otherwise every worker would arrive at the same instant."""
    first = list(ArrivalSchedule(rate_per_s=400.0, worker=0, workers=4).offsets(50))
    second = list(ArrivalSchedule(rate_per_s=400.0, worker=1, workers=4).offsets(50))
    assert first != second


def test_intervals_are_exponential_with_the_right_mean() -> None:
    """A Poisson process, not a uniform one: bursts are the point."""
    schedule = ArrivalSchedule(rate_per_s=1000.0, workers=1, seed=11)
    intervals = [schedule.interval(i) for i in range(20_000)]
    mean = statistics.fmean(intervals)
    assert mean == pytest.approx(0.001, rel=0.03)
    # An exponential has standard deviation equal to its mean; a uniform on
    # [0, 2/rate] would give 0.577 of it, which is what this rules out.
    assert statistics.stdev(intervals) / mean == pytest.approx(1.0, rel=0.05)


def test_rate_is_split_across_workers() -> None:
    schedule = ArrivalSchedule(rate_per_s=800.0, worker=2, workers=4)
    assert schedule.worker_rate_per_s == 200.0
    assert schedule.mean_interval_s == pytest.approx(0.005)


def test_offsets_until_stops_at_the_duration() -> None:
    schedule = ArrivalSchedule(rate_per_s=1000.0, workers=1, seed=3)
    offsets = list(schedule.offsets_until(2.0))
    assert offsets and offsets[-1] <= 2.0
    assert len(offsets) == pytest.approx(2000, rel=0.1)


def test_offsets_increase() -> None:
    schedule = ArrivalSchedule(rate_per_s=250.0, workers=1, seed=5)
    offsets = list(schedule.offsets(1000))
    assert all(b > a for a, b in zip(offsets, offsets[1:], strict=False))


def test_uniform_stays_in_range() -> None:
    values = [uniform(1, 0, i) for i in range(5000)]
    assert all(0.0 <= v < 1.0 for v in values)
    assert statistics.fmean(values) == pytest.approx(0.5, abs=0.02)


def test_exponential_is_defined_at_the_bottom_of_the_range() -> None:
    """u = 0 is reachable; log(1 - u) must not be log(0)."""
    assert exponential_interval(100.0, 0.0) == 0.0
    assert math.isfinite(exponential_interval(100.0, 0.999999))
    with pytest.raises(ValueError, match="rate must be positive"):
        exponential_interval(0.0, 0.5)


def test_a_worker_outside_the_pool_is_refused() -> None:
    with pytest.raises(ValueError, match="outside"):
        ArrivalSchedule(rate_per_s=10.0, worker=4, workers=4)
