"""S16-S18b acceptance: per-backend RSS growth over a long soak.

*PLAN.md:* "over a >=1 h soak the per-backend RSS slope has a bootstrap CI
excluding zero."

An hour is not arbitrary and this cannot be shortened into the ordinary test
run. A backend's RSS moves in page-sized steps as its caches fill, so over ten
minutes the slope is mostly the start-up transient and the interval swamps it;
the growth only separates from that over a much longer window. The soak is
therefore produced deliberately --

    python scripts/soak_backend_growth.py --minutes 65 --out runs

-- and this test analyses whatever soak it is pointed at, so the hour is spent
once rather than on every suite run. Set `DSEL_SOAK_METRICS` to the resulting
`metrics.ndjson`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dsel.live.merge import read_merged
from dsel.phenomena.backend_growth import MIN_SPAN_S, growth_from_records

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "analyse_backend_growth.py"
SOAK_HOUR_S = 3600.0

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def soak_metrics() -> Path:
    raw = os.environ.get("DSEL_SOAK_METRICS")
    if not raw:
        pytest.skip(
            "set DSEL_SOAK_METRICS to a soak's metrics.ndjson; produce one with "
            "`python scripts/soak_backend_growth.py --minutes 65`"
        )
    path = Path(raw)
    if not path.exists():
        pytest.fail(f"DSEL_SOAK_METRICS={path} does not exist")
    return path


def test_the_soak_is_long_enough_to_ask_the_question(soak_metrics: Path) -> None:
    """A short soak measures the start-up transient and calls it growth."""
    result = growth_from_records(read_merged(soak_metrics), resamples=1)
    assert result.fits, "no backend records carrying VmRSS"
    span = max(f.span_s for f in result.fits)
    assert span >= SOAK_HOUR_S * 0.95, (
        f"the longest backend was observed for {span / 60:.1f} minutes; PLAN.md "
        "asks for an hour, and under it the interval is dominated by the "
        "start-up transient"
    )


def test_enough_backends_were_fitted_to_bootstrap_over(soak_metrics: Path) -> None:
    """Backends are the independent unit, so they are what gets resampled.
    A handful of them is a handful of independent observations, whatever the
    sample count says."""
    result = growth_from_records(read_merged(soak_metrics), resamples=1)
    assert len(result.usable_fits) >= 8, (
        f"{len(result.usable_fits)} backends cleared the bar "
        f"(>= {MIN_SPAN_S:.0f}s); the interval would be meaningless"
    )


def test_the_slope_interval_excludes_zero(soak_metrics: Path) -> None:
    """The acceptance -- and not by accident.

    A first soak drove 24 identical connections and produced 21 identical
    slopes: every resample had the same median, the interval had zero width,
    and it "excluded zero" for a reason that had nothing to do with memory.
    The connections have to differ, as production connections do.
    """
    result = growth_from_records(read_merged(soak_metrics))
    print("\n" + result.table())
    assert not result.degenerate, result.table()
    assert result.excludes_zero, result.table()
    assert result.significant


def test_an_independent_script_reaches_the_same_interval(soak_metrics: Path) -> None:
    """From the file alone, in a separate process, as with the landmarks."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(soak_metrics), "--table"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout[result.stdout.index("{") :])
    in_process = growth_from_records(read_merged(soak_metrics))
    assert payload["excludes_zero"] is in_process.excludes_zero
    assert payload["degenerate"] is in_process.degenerate
    assert payload["ci_low_bytes_per_s"] == pytest.approx(in_process.ci_low_bytes_per_s)
    assert payload["ci_high_bytes_per_s"] == pytest.approx(in_process.ci_high_bytes_per_s)
