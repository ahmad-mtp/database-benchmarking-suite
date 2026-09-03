"""Per-backend RSS growth (PLAN.md S16-S18b).

*Accept: over a >=1 h soak the per-backend RSS slope has a bootstrap CI
excluding zero.*

The live soak is the acceptance. These tests hold the method to answers that
are known by construction -- including the two ways this analysis can lie to
itself: pooling backends that started at different times, and bootstrapping
samples that are not independent.
"""

from __future__ import annotations

import pytest

from dsel.live.schema import BackendRecord
from dsel.phenomena.backend_growth import (
    MIN_SAMPLES,
    MIN_SPAN_S,
    bootstrap_slope,
    fits_from_records,
    growth_from_records,
    least_squares_slope,
)


def series(
    backend: str, start_s: float, count: int, base: int, per_second: float, step_s: float = 60.0
) -> list[BackendRecord]:
    return [
        BackendRecord(
            t_ms=int((start_s + index * step_s) * 1000),
            w="s",
            seq=index,
            engine="postgres",
            backend_id=backend,
            state="idle",
            vm_rss_bytes=int(base + per_second * index * step_s),
        )
        for index in range(count)
    ]


def test_least_squares_recovers_a_known_slope() -> None:
    points = [(float(t), 100.0 + 3.0 * t) for t in range(50)]
    assert least_squares_slope(points) == pytest.approx(3.0)


def test_a_flat_series_has_no_slope_and_a_single_point_has_none_either() -> None:
    assert least_squares_slope([(float(t), 500.0) for t in range(20)]) == pytest.approx(0.0)
    assert least_squares_slope([(1.0, 5.0)]) == 0.0


def test_each_backend_is_fitted_over_its_own_life() -> None:
    """Not one line through everything.

    Backends start at different times and sit at different points on the curve,
    so a pooled fit reads the spread *between* connections as growth *within*
    one. Here two backends each grow at 100 B/s but start an hour apart and at
    very different sizes: a pooled fit would report a wild slope, and the
    per-backend fits both report 100.
    """
    records = series("a", 0.0, 20, base=10_000_000, per_second=100.0) + series(
        "b", 3600.0, 20, base=80_000_000, per_second=100.0
    )
    fits = {f.backend_id: f for f in fits_from_records(records)}
    assert set(fits) == {"postgres/a", "postgres/b"}
    for fit in fits.values():
        assert fit.slope_bytes_per_s == pytest.approx(100.0)
        assert fit.usable


def test_a_backend_with_too_few_samples_is_excluded_not_fitted() -> None:
    """A slope through three points spanning ninety seconds is not an hourly
    growth rate, and letting it in would widen the interval with noise while
    pretending to be evidence."""
    short = series("brief", 0.0, MIN_SAMPLES - 1, base=1_000_000, per_second=500.0, step_s=30.0)
    fits = fits_from_records(short)
    assert len(fits) == 1
    assert not fits[0].usable
    result = bootstrap_slope(fits)
    assert result.usable_fits == ()
    assert not result.excludes_zero


def test_a_real_growth_gives_an_interval_that_excludes_zero() -> None:
    records: list[BackendRecord] = []
    for index in range(12):
        records += series(
            f"pid-{index}",
            start_s=index * 10.0,
            count=40,
            base=9_000_000 + index * 200_000,
            per_second=120.0 + index,
        )
    result = growth_from_records(records, resamples=2000, seed=1)
    assert len(result.usable_fits) == 12
    assert result.median_slope_bytes_per_s == pytest.approx(125.5, abs=6.0)
    assert result.excludes_zero, result.table()
    assert result.ci_low_bytes_per_s > 0


def test_flat_backends_give_an_interval_that_includes_zero() -> None:
    """The method has to be able to find nothing. One that always excludes
    zero is not a test of anything."""
    records: list[BackendRecord] = []
    for index in range(12):
        records += series(f"pid-{index}", index * 10.0, 40, base=9_000_000, per_second=0.0)
    result = growth_from_records(records, resamples=2000, seed=1)
    assert not result.excludes_zero, result.table()


def test_shrinking_memory_also_counts_as_a_finding() -> None:
    """Either side of zero. A test that only looked for growth would quietly
    pass a leak running backwards."""
    records: list[BackendRecord] = []
    for index in range(12):
        records += series(f"pid-{index}", index * 10.0, 40, base=90_000_000, per_second=-80.0)
    result = growth_from_records(records, resamples=2000, seed=1)
    assert result.excludes_zero
    assert result.ci_high_bytes_per_s < 0


def test_the_bootstrap_resamples_backends_not_samples() -> None:
    """Samples within one backend are serially correlated -- consecutive
    readings of the same process are nearly the same number -- so resampling
    them would manufacture confidence out of autocorrelation. With a single
    backend there is nothing to resample and the interval must collapse onto
    that one backend's slope rather than shrinking towards a false certainty.
    """
    records = series("only", 0.0, 60, base=9_000_000, per_second=140.0)
    result = growth_from_records(records, resamples=500, seed=1)
    assert len(result.usable_fits) == 1
    assert result.ci_low_bytes_per_s == pytest.approx(result.ci_high_bytes_per_s)
    assert result.median_slope_bytes_per_s == pytest.approx(140.0)


def test_the_interval_is_reproducible_from_its_seed() -> None:
    records: list[BackendRecord] = []
    for index in range(8):
        records += series(
            f"pid-{index}", index * 5.0, 30, base=9_000_000, per_second=90.0 + index
        )
    first = growth_from_records(records, resamples=1000, seed=7)
    second = growth_from_records(records, resamples=1000, seed=7)
    assert first.ci_low_bytes_per_s == second.ci_low_bytes_per_s
    assert first.ci_high_bytes_per_s == second.ci_high_bytes_per_s


def test_backends_are_keyed_by_engine_and_pid_together() -> None:
    """A pid is only unique within a container. Splicing two backends' lives
    into one line would produce an implausible slope from two innocent ones."""
    records = [
        *series("42", 0.0, 20, base=9_000_000, per_second=0.0),
        *[
            r.model_copy(update={"engine": "valkey"})
            for r in series("42", 0.0, 20, base=90_000_000, per_second=0.0)
        ],
    ]
    fits = fits_from_records(records)
    assert {f.backend_id for f in fits} == {"postgres/42", "valkey/42"}


def test_min_span_is_stated_in_seconds_not_in_samples() -> None:
    """Eight samples a second apart is eight seconds of evidence, not eight
    samples' worth. Both bars have to be cleared."""
    dense = series("dense", 0.0, MIN_SAMPLES * 2, base=9_000_000, per_second=100.0, step_s=1.0)
    fits = fits_from_records(dense)
    assert fits[0].samples >= MIN_SAMPLES
    assert fits[0].span_s < MIN_SPAN_S
    assert not fits[0].usable
