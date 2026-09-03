"""S11 acceptance (ramp half): the ramp recovers limits known by construction.

*PLAN.md S11-S12:* "...the ramp recovers a knee and collapse point from a
synthetic target whose limits are known by construction."

The target's service time is `median / (1 - offered/C)`, so both limits are
arithmetic before the ramp runs:

    knee     = C - (C - baseline) / 2     latency doubles against step one
    collapse = W / (median + W/C)         where `min(r, W/s(r))` turns over

A ramp that reported a knee because someone looked at a chart would pass
nothing. These numbers come from `Ramp`'s own rules and are checked against the
closed form, and the answer a ramp can give is quantised to its own steps: the
recovered value must be the first *step* at or past the true one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dsel.driver.ramp import KNEE_FACTOR, RampPlan, run_ramp
from dsel.driver.transport import collapse_rate, deliverable_rate, knee_rate
from tests.support.synthetic import (
    RAMP_CAPACITY_PER_S,
    RAMP_MEDIAN_US,
    RAMP_WORKERS,
    ramp_factory,
)

RATES = (100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0)

pytestmark = pytest.mark.slow


def _first_step_at_or_past(value: float) -> float:
    return next(rate for rate in RATES if rate >= value)


def _first_step_past(value: float) -> float:
    return next(rate for rate in RATES if rate > value)


@pytest.fixture(scope="module")
def ramp(tmp_path_factory: pytest.TempPathFactory):
    plan = RampPlan(
        run_dir=tmp_path_factory.mktemp("s11"),
        engine="synthetic",
        scenario="oltp-read",
        ops=("read",),
        rates_per_s=RATES,
        duration_s=4.0,
        warmup_s=1.0,
        workers=RAMP_WORKERS,
    )
    return run_ramp(plan, ramp_factory)


def test_the_ramp_recovers_the_knee(ramp) -> None:
    expected = knee_rate(RAMP_CAPACITY_PER_S, RATES[0], KNEE_FACTOR)
    assert ramp.knee_rate_per_s == _first_step_past(expected), (
        f"closed-form knee {expected:.0f}/s, ramp said {ramp.knee_rate_per_s}\n{ramp.table()}"
    )


def test_the_ramp_recovers_the_collapse_point(ramp) -> None:
    expected = collapse_rate(RAMP_MEDIAN_US, RAMP_CAPACITY_PER_S, RAMP_WORKERS)
    assert ramp.collapse_rate_per_s == _first_step_at_or_past(expected), (
        f"closed-form collapse {expected:.0f}/s, ramp said "
        f"{ramp.collapse_rate_per_s}\n{ramp.table()}"
    )


def test_max_sustainable_sits_below_the_collapse(ramp) -> None:
    expected = collapse_rate(RAMP_MEDIAN_US, RAMP_CAPACITY_PER_S, RAMP_WORKERS)
    sustainable = ramp.max_sustainable_rate_per_s
    assert sustainable is not None
    assert sustainable < expected, f"{sustainable} is past the collapse\n{ramp.table()}"
    assert sustainable >= 400.0, f"the ramp gave up too early\n{ramp.table()}"


def test_achieved_throughput_follows_the_closed_form(ramp) -> None:
    """Not only the two landmarks: the whole curve, so a ramp that happened to
    land the knee for the wrong reason still fails.

    Up to the collapse the closed form is held to 20%. Past it only the *shape*
    is: `min(r, W/s(r))` says throughput turns over there, and it does, but the
    depth of the far tail depends on a queue term chosen to be steep and
    monotonic rather than to be an M/M/1. Measured on a busy host, 185/s
    against a predicted 240/s at 700 offered -- the turn is real, the depth is
    not a prediction this model is entitled to make.
    """
    collapse = collapse_rate(RAMP_MEDIAN_US, RAMP_CAPACITY_PER_S, RAMP_WORKERS)
    off = []
    peak = 0.0
    for step in ramp.steps:
        expected = deliverable_rate(
            RAMP_MEDIAN_US, RAMP_CAPACITY_PER_S, RAMP_WORKERS, step.offered_rate_per_s
        )
        if step.offered_rate_per_s <= collapse:
            if abs(step.achieved_rate_per_s - expected) / expected > 0.20:
                off.append(
                    f"offered {step.offered_rate_per_s:.0f}: achieved "
                    f"{step.achieved_rate_per_s:.0f}, closed form {expected:.0f}"
                )
            peak = max(peak, step.achieved_rate_per_s)
        elif step.achieved_rate_per_s >= peak:
            off.append(
                f"offered {step.offered_rate_per_s:.0f}: achieved "
                f"{step.achieved_rate_per_s:.0f}, which is not below the pre-collapse "
                f"peak of {peak:.0f}"
            )
    assert not off, "\n".join(off) + "\n" + ramp.table()


def test_the_ramp_marks_the_steps_it_could_not_deliver(ramp) -> None:
    """Past the collapse the driver cannot keep up, and that must show as a
    verdict rather than as a fast-looking result."""
    beyond = [s for s in ramp.steps if s.offered_rate_per_s >= 600.0]
    assert beyond, "the ramp did not go past the collapse"
    assert all(not step.delivered for step in beyond), ramp.table()
    print("\n" + ramp.table())


def test_cells_ran_serialised_and_each_kept_its_own_directory(ramp, tmp_path_factory) -> None:
    """Two rates in flight would contend, and the second would measure the first."""
    assert len(ramp.steps) == len(RATES)
    assert [s.offered_rate_per_s for s in ramp.steps] == list(RATES)


def test_a_ramp_over_an_uncapped_target_finds_no_collapse(tmp_path: Path) -> None:
    """The rules must be able to say "not reached"; a knee that is always found
    is not a measurement."""
    from tests.support.synthetic import synthetic_factory

    plan = RampPlan(
        run_dir=tmp_path,
        engine="synthetic",
        scenario="oltp-read",
        repeat=2,
        ops=("read",),
        rates_per_s=(50.0, 100.0, 150.0),
        duration_s=3.0,
        warmup_s=0.5,
        workers=2,
    )
    result = run_ramp(plan, synthetic_factory)
    assert result.collapse_rate_per_s is None, result.table()
    assert result.knee_rate_per_s is None, result.table()
    assert result.max_sustainable_rate_per_s == 150.0, result.table()
