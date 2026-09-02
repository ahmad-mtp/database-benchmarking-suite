"""Interference sweep against the live daemon (PLAN.md S1 follow-on)."""

from __future__ import annotations

import pytest

from dsel.audit.environment import resolve_image
from dsel.audit.interference import _Contention, _measure_capacity, sweep
from tests.conftest import requires_docker


@pytest.fixture(scope="module")
def pin():
    return resolve_image("python:3.13-slim")


@requires_docker
def test_capacity_probe_scales_with_worker_count(pin) -> None:
    """Four workers on a four-vCPU cpuset must beat one. If not, the probe is
    measuring something other than CPU."""
    one = _measure_capacity(pin, "2-5", workers=1, window_s=1.0)
    four = _measure_capacity(pin, "2-5", workers=4, window_s=1.0)
    assert four > one * 2, f"1 worker={one:,.0f} 4 workers={four:,.0f}"


@requires_docker
def test_contention_context_manager_always_cleans_up(pin) -> None:
    """A leaked spinner would silently poison every later measurement."""
    import subprocess

    with _Contention(pin, "6-9", workers=2) as c:
        name = c._name
        running = subprocess.run(
            ["docker", "ps", "--filter", f"name={name}", "--quiet"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert running, "contention container did not start"
    gone = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={name}", "--quiet"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert not gone, f"contention container {name} leaked"


@requires_docker
def test_contention_cleans_up_on_exception(pin) -> None:
    import subprocess

    name = None
    with pytest.raises(RuntimeError, match="boom"), _Contention(pin, "6-9", workers=1) as c:
        name = c._name
        raise RuntimeError("boom")
    gone = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={name}", "--quiet"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert not gone, f"contention container {name} leaked after an exception"


@requires_docker
def test_zero_level_starts_no_container(pin) -> None:
    import subprocess

    before = subprocess.run(
        ["docker", "ps", "--quiet"], capture_output=True, text=True, check=True
    ).stdout.count("\n")
    with _Contention(pin, "6-9", workers=0):
        during = subprocess.run(
            ["docker", "ps", "--quiet"], capture_output=True, text=True, check=True
        ).stdout.count("\n")
    assert during == before


@requires_docker
@pytest.mark.slow
def test_minimal_sweep_produces_a_baseline_of_one(pin) -> None:
    """Level 0 is measured against itself, so its median retained is 1.0."""
    result = sweep(pin, levels=(0, 4), blocks=2, window_s=1.0)
    baseline = next(level for level in result.levels if level.contention_workers == 0)
    assert baseline.retained_median == 1.0
    assert all(level.blocks == 2 for level in result.levels)
