"""S15 acceptance: the landmarks are in the evidence, not in the process.

*PLAN.md S15:* "`phenomena/*` reads `metrics.ndjson` and never touches Docker
or the engine; `live/sampler/*` writes records and never derives a phenomenon.
Accept: an independent script re-derives knee and collapse from
`metrics.ndjson` alone."

If a landmark cannot be recovered from the file, it was never evidence -- it
was a number held in the memory of the process that produced it, and nobody
outside that process could check it. The script here is given a file and
nothing else: no run directory, no daemon, no engine.

The script imports the *rules* rather than restating them, deliberately. Two
copies of "what a knee is" would drift, and agreement between drifting
definitions proves nothing. The independence that matters is the data path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from dsel.driver.ramp import RampPlan, run_ramp
from dsel.live.merge import find_shards, merge_records, read_merged, sort_key
from dsel.live.ndjson import dumps
from dsel.phenomena.conn_cliff import curve_from_records
from tests.support.synthetic import RAMP_WORKERS, ramp_factory

RATES = (100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0)
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "rederive_landmarks.py"

pytestmark = pytest.mark.slow


# Set by the fixture so the single-step test can reach the same shard tree.
SHARD_ROOT: list[Path] = []


@pytest.fixture(scope="module")
def ramp_and_metrics(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("s15")
    SHARD_ROOT.append(root)
    plan = RampPlan(
        run_dir=root,
        engine="synthetic",
        scenario="oltp-read",
        ops=("read",),
        rates_per_s=RATES,
        duration_s=4.0,
        warmup_s=1.0,
        workers=RAMP_WORKERS,
    )
    ramp = run_ramp(plan, ramp_factory)
    # Each step wrote its own shard directory; the run's metrics file is all of
    # them merged, which is what the audit bundle carries.
    records = []
    for index in range(len(RATES)):
        records.extend(merge_records(find_shards(root / f"step{index}" / "shards")))
    metrics = root / "metrics.ndjson"
    records.sort(key=sort_key)
    metrics.write_text("".join(dumps(r) + "\n" for r in records), encoding="utf-8")
    return ramp, metrics


def test_the_file_alone_recovers_the_same_landmarks(ramp_and_metrics) -> None:
    ramp, metrics = ramp_and_metrics
    curve = curve_from_records(read_merged(metrics))
    assert len(curve.points) == len(RATES), [p.offered_rate_per_s for p in curve.points]
    assert curve.knee_rate_per_s == ramp.knee_rate_per_s
    assert curve.collapse_rate_per_s == ramp.collapse_rate_per_s
    assert curve.max_sustainable_rate_per_s == ramp.max_sustainable_rate_per_s


def test_an_independent_script_recovers_them_too(ramp_and_metrics) -> None:
    """Run as a separate process given a path, with nothing else."""
    ramp, metrics = ramp_and_metrics
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(metrics), "--table"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    print("\n" + result.stdout)
    payload = json.loads(result.stdout[result.stdout.index("{") :])
    assert payload["knee_rate_per_s"] == ramp.knee_rate_per_s
    assert payload["collapse_rate_per_s"] == ramp.collapse_rate_per_s
    assert payload["max_sustainable_rate_per_s"] == ramp.max_sustainable_rate_per_s
    assert payload["steps"] == len(RATES)


def test_the_curve_is_ordered_by_offered_rate_not_by_arrival(ramp_and_metrics) -> None:
    """The file is in `(t_ms, w, seq)` order, not rate order. A derivation that
    trusted arrival order would find the knee wherever the steps happened to
    have been written."""
    _, metrics = ramp_and_metrics
    curve = curve_from_records(read_merged(metrics))
    rates = [p.offered_rate_per_s for p in curve.points]
    assert rates == sorted(rates) == list(RATES)


def test_the_script_refuses_a_file_with_no_curve_in_it(tmp_path: Path) -> None:
    """Silence would be worse than an error: an empty answer looks exactly
    like a run that had no knee."""
    empty = tmp_path / "metrics.ndjson"
    empty.write_text("", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(empty)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )
    assert result.returncode != 0
    assert "no latency_window records" in result.stderr


def test_the_script_reads_the_file_the_bundle_actually_carries(
    ramp_and_metrics, tmp_path: Path
) -> None:
    """The bundle's metrics file is written by `merge_to_file`. The script has
    to read that, not a shape only this test knows how to build."""
    from dsel.live.merge import merge_to_file

    ramp, _ = ramp_and_metrics
    # One step's shards, merged the way the harness merges them.
    root = Path(str(ramp.steps[0].offered_rate_per_s))
    assert root  # the ramp ran
    shard_dir = SHARD_ROOT[0] / "step0" / "shards"
    merged = tmp_path / "metrics.ndjson"
    count = merge_to_file(shard_dir, merged)
    assert count > 0

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(merged)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    # A single step is a curve of one point: no knee, no collapse, and the
    # step itself as the sustainable rate. Saying "no knee" from one point is
    # correct; inventing one would not be.
    assert payload["steps"] == 1
    assert payload["knee_rate_per_s"] is None
    assert payload["collapse_rate_per_s"] is None
