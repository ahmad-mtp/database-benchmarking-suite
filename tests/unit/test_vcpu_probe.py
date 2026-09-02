"""The vCPU probe's deviation flag (PLAN.md S1 Accept: "the deviation flag is correct").

`analyse` is pure, so correctness is checked against vectors whose right answer
is known by construction -- including the boundary, where a >10% rule and a
>=10% rule disagree.
"""

from __future__ import annotations

import pytest

from dsel.audit.models import VcpuSpeed
from dsel.audit.vcpu_probe import (
    DRIVER_CPUSET,
    ENGINE_CPUSET,
    HETEROGENEITY_THRESHOLD_PCT,
    REASON_HETEROGENEOUS,
    REASON_INDISTINGUISHABLE,
    analyse,
)


def speeds(medians: list[float], spread_pct: float = 0.0) -> list[VcpuSpeed]:
    """Ten vCPUs with the given medians and a controlled within-vCPU spread."""
    out = []
    for cpu, med in enumerate(medians):
        half = med * spread_pct / 200.0
        out.append(
            VcpuSpeed(
                vcpu=cpu,
                median_ips=med,
                min_ips=med - half,
                max_ips=med + half,
                samples=5,
            )
        )
    return out


def test_uniform_vcpus_do_not_flag_heterogeneous() -> None:
    result = analyse(speeds([100.0] * 10))
    assert REASON_HETEROGENEOUS not in result.reasons
    assert result.set_difference_pct == 0.0
    assert result.relative_speed == [1.0] * 10


def test_slow_engine_set_flags_heterogeneous() -> None:
    """Engine set at half speed: a 33% set difference must flag."""
    medians = [100.0] * 10
    for cpu in ENGINE_CPUSET:
        medians[cpu] = 50.0
    result = analyse(speeds(medians))
    assert REASON_HETEROGENEOUS in result.reasons
    assert result.set_difference_pct == pytest.approx(50.0)


def test_slow_driver_set_flags_heterogeneous_too() -> None:
    """The rule is symmetric; which set is slower must not matter."""
    medians = [100.0] * 10
    for cpu in DRIVER_CPUSET:
        medians[cpu] = 50.0
    result = analyse(speeds(medians))
    assert REASON_HETEROGENEOUS in result.reasons
    assert result.set_difference_pct == pytest.approx(50.0)


@pytest.mark.parametrize(
    ("driver_median", "expected_pct", "should_flag"),
    [
        (100.0, 0.0, False),
        (95.0, 5.0, False),
        (90.0, 10.0, False),  # exactly at threshold: rule is ">10", not ">="
        (89.9, 10.1, True),
        (80.0, 20.0, True),
    ],
)
def test_threshold_boundary(
    driver_median: float, expected_pct: float, should_flag: bool
) -> None:
    medians = [100.0] * 10
    for cpu in DRIVER_CPUSET:
        medians[cpu] = driver_median
    result = analyse(speeds(medians))
    assert result.set_difference_pct == pytest.approx(expected_pct, abs=1e-4)
    assert (REASON_HETEROGENEOUS in result.reasons) is should_flag
    assert (result.set_difference_pct > HETEROGENEITY_THRESHOLD_PCT) is should_flag


def test_noise_exceeding_signal_flags_indistinguishable() -> None:
    """The measured case on this host: ~3% span under ~36% within-vCPU noise."""
    medians = [100.0, 99.0, 98.0, 97.5, 98.0, 98.2, 98.4, 99.3, 99.7, 97.9]
    result = analyse(speeds(medians, spread_pct=36.0))
    assert REASON_INDISTINGUISHABLE in result.reasons
    assert REASON_HETEROGENEOUS not in result.reasons
    assert result.between_vcpu_span_pct < result.max_within_vcpu_spread_pct


def test_clean_signal_does_not_flag_indistinguishable() -> None:
    """A real 50% split measured with low noise is resolvable, and must say so."""
    medians = [100.0] * 10
    for cpu in ENGINE_CPUSET:
        medians[cpu] = 50.0
    result = analyse(speeds(medians, spread_pct=2.0))
    assert REASON_INDISTINGUISHABLE not in result.reasons
    assert REASON_HETEROGENEOUS in result.reasons


def test_relative_speed_is_normalised_to_the_fastest() -> None:
    medians = [50.0, 100.0, 75.0] + [100.0] * 7
    result = analyse(speeds(medians))
    assert max(result.relative_speed) == 1.0
    assert result.relative_speed[0] == pytest.approx(0.5)
    assert result.relative_speed[2] == pytest.approx(0.75)


def test_missing_vcpu_is_an_error_not_a_silent_default() -> None:
    with pytest.raises(ValueError, match="not measured"):
        analyse(speeds([100.0] * 6))


def test_empty_measurements_rejected() -> None:
    with pytest.raises(ValueError, match="no vCPU measurements"):
        analyse([])


def test_spread_pct_is_relative_to_median() -> None:
    s = VcpuSpeed(vcpu=0, median_ips=100.0, min_ips=90.0, max_ips=110.0, samples=5)
    assert s.spread_pct == pytest.approx(20.0)
