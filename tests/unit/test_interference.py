"""Interference sweep analysis (PLAN.md S1 follow-on).

The Docker-dependent parts are exercised in tests/integration. What is checked
here is the arithmetic that turns measurements into a claim -- because that is
where a sweep quietly lies.
"""

from __future__ import annotations

import random

import pytest

from dsel.audit.interference import (
    DEFAULT_LEVELS,
    InterferenceSweep,
    LevelResult,
    _bootstrap_ci,
)


def level(workers: int, retained: float, low: float, high: float) -> LevelResult:
    return LevelResult(
        contention_workers=workers,
        retained_median=retained,
        retained_ci_low=low,
        retained_ci_high=high,
        absolute_median_ops_per_s=retained * 1e6,
        blocks=6,
    )


def make_sweep(levels: list[LevelResult]) -> InterferenceSweep:
    return InterferenceSweep(
        measured_cpuset="2-5",
        contended_cpuset="6-9",
        window_s=2.0,
        blocks=6,
        seed=1,
        levels=levels,
    )


def test_zero_level_is_required_as_the_reference() -> None:
    """Without an uncontended baseline every ratio is meaningless."""
    from dsel.audit.interference import sweep

    with pytest.raises(ValueError, match="must include 0"):
        sweep(image=None, levels=(1, 2, 4))  # type: ignore[arg-type]


def test_default_levels_include_the_baseline() -> None:
    assert 0 in DEFAULT_LEVELS


def test_isolation_holds_when_contention_costs_nothing() -> None:
    s = make_sweep([level(0, 1.0, 0.98, 1.02), level(4, 0.99, 0.96, 1.02)])
    assert s.isolation_holds(0.10) is True


def test_isolation_fails_when_ci_low_drops_below_tolerance() -> None:
    s = make_sweep([level(0, 1.0, 0.98, 1.02), level(4, 0.65, 0.60, 0.70)])
    assert s.isolation_holds(0.10) is False
    assert s.worst_retained.contention_workers == 4


def test_isolation_uses_ci_low_not_the_point_estimate() -> None:
    """A median just inside tolerance with a wide interval must not pass."""
    s = make_sweep([level(0, 1.0, 0.98, 1.02), level(4, 0.92, 0.71, 1.05)])
    assert s.worst_retained.retained_median > 0.90
    assert s.isolation_holds(0.10) is False


def test_worst_retained_picks_the_minimum() -> None:
    s = make_sweep(
        [level(0, 1.0, 0.99, 1.01), level(1, 0.90, 0.88, 0.92), level(4, 0.55, 0.50, 0.60)]
    )
    assert s.worst_retained.contention_workers == 4


def test_bootstrap_ci_brackets_the_median() -> None:
    rng = random.Random(7)
    ratios = [0.62, 0.65, 0.63, 0.66, 0.64, 0.61]
    low, high = _bootstrap_ci(ratios, 2000, rng)
    assert low <= 0.635 <= high
    assert low < high


def test_bootstrap_ci_is_deterministic_for_a_seed() -> None:
    ratios = [0.62, 0.65, 0.63, 0.66, 0.64, 0.61]
    a = _bootstrap_ci(ratios, 2000, random.Random(11))
    b = _bootstrap_ci(ratios, 2000, random.Random(11))
    assert a == b


def test_bootstrap_ci_widens_with_noisier_blocks() -> None:
    tight = _bootstrap_ci([0.64] * 6, 4000, random.Random(3))
    wide = _bootstrap_ci([0.30, 0.95, 0.55, 0.80, 0.40, 0.90], 4000, random.Random(3))
    assert (wide[1] - wide[0]) > (tight[1] - tight[0])


def test_bootstrap_ci_handles_a_single_block() -> None:
    low, high = _bootstrap_ci([0.7], 1000, random.Random(1))
    assert low == high == 0.7
