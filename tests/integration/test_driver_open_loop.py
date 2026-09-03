"""The driver is open loop, and says so when it cannot be (PLAN.md S10).

Coordinated omission is not an error that shows up in a log. It shows up as a
good p99, obtained by asking the target for less work than the spec said. These
tests are the two halves of not doing that:

* while the driver can keep up, the offered rate is delivered and latency is
  measured from the *scheduled* start, so any lateness is inside the number;
* once it cannot keep up, the run is marked `INCONCLUSIVE_DRIVER_BOUND` rather
  than reported.

The transport is synthetic on purpose: its service-time distribution is known
in closed form, so a disagreement can only be the driver's or the histogram's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dsel.driver.pool import plan_workers, run_pool
from dsel.driver.transport import SyntheticTransport
from dsel.driver.worker import WorkerSpec, run_worker
from dsel.live.merge import find_shards, merge_records
from dsel.live.schema import LatencyWindowRecord, ValidityRecord

pytestmark = pytest.mark.slow

CELL = "uc1/postgres/oltp-mixed/r200/rep1"


def test_the_offered_rate_is_delivered_and_lateness_is_inside_the_latency(
    tmp_path: Path,
) -> None:
    spec = WorkerSpec(
        worker=0,
        workers=1,
        run_dir=tmp_path,
        cell=CELL,
        ops=("read",),
        rate_per_s=200.0,
        duration_s=4.0,
    )
    transport = SyntheticTransport(median_us=600.0, sigma=0.4, worker=0, seed=spec.seed)
    result = run_worker(spec, transport)

    assert result.issued == pytest.approx(800, rel=0.1), "the offered rate was not delivered"
    assert result.achieved_rate_per_s == pytest.approx(200.0, rel=0.1)
    assert result.verdicts, "both gates must be reported, fired or not"
    assert not result.driver_bound, result.verdicts

    # The uncorrected log is what a closed-loop driver would have reported: the
    # service time alone. The corrected one adds the driver's own lateness at
    # every percentile, which is the thing coordinated omission discards.
    from dsel.driver.histogram import read_hlog

    corrected = read_hlog(result.hlogs["read/corrected"])
    uncorrected = read_hlog(result.hlogs["read/uncorrected"])
    assert uncorrected.get_value_at_percentile(50) == pytest.approx(600.0, rel=0.15), (
        "the synthetic target's median is known in closed form"
    )
    for point in (50.0, 90.0, 99.0, 99.9):
        assert corrected.get_value_at_percentile(point) >= uncorrected.get_value_at_percentile(
            point
        ), f"corrected latency fell below uncorrected at p{point:g}"
    assert corrected.get_value_at_percentile(99) > uncorrected.get_value_at_percentile(99), (
        "the tail is where omission hides; the two logs must differ there"
    )


def test_a_driver_that_cannot_keep_up_is_inconclusive_not_fast(tmp_path: Path) -> None:
    """A worker holds one request at a time, so an offered rate above
    1/service_time is undeliverable. The wrong answer is a flattering p99; the
    right one is a verdict."""
    spec = WorkerSpec(
        worker=0,
        workers=1,
        run_dir=tmp_path,
        cell=CELL,
        ops=("read",),
        rate_per_s=400.0,
        duration_s=3.0,
    )
    # 8 ms of service against a 2.5 ms mean inter-arrival: it cannot be done.
    transport = SyntheticTransport(median_us=8000.0, sigma=0.05, worker=0, seed=spec.seed)
    result = run_worker(spec, transport)

    assert result.driver_bound, result.verdicts
    assert result.achieved_rate_per_s < 200.0, "it should not have kept up"
    # And the latency it reports is honest about it rather than flattering.
    assert result.summary["read"]["p50"] > 100_000.0, result.summary


def test_the_pool_writes_one_shard_per_worker_and_merges_cleanly(tmp_path: Path) -> None:
    from tests.support.synthetic import synthetic_factory

    specs = plan_workers(
        tmp_path, CELL, ("read", "write"), rate_per_s=200.0, duration_s=4.0, workers=3
    )
    result = run_pool(specs, synthetic_factory)

    shards = find_shards(tmp_path / "shards")
    assert len(shards) == 3, [p.name for p in shards]
    records = list(merge_records(shards))
    assert records, "the merge must not be empty"

    windows = [r for r in records if isinstance(r, LatencyWindowRecord)]
    gates = [r for r in records if isinstance(r, ValidityRecord)]
    assert {r.op for r in windows} == {"read", "write"}
    assert all(r.estimate_only is True for r in windows)
    assert len(gates) == 9, "three gates per worker, reported whether or not they fired"
    assert {r.gate.split("[")[0] for r in gates} == {
        "driver_worker_cpu",
        "driver_schedule_lag",
        "driver_lag_share",
    }
    assert {r.verdict for r in gates} <= {"OK", "INCONCLUSIVE_DRIVER_BOUND"}

    assert result.completed > 0
    assert result.achieved_rate_per_s == pytest.approx(200.0, rel=0.15)


def test_aggregating_worker_histograms_is_exact(tmp_path: Path) -> None:
    """HdrHistograms are counts per bucket, so summing them is not an
    approximation of the workers -- it is the workers."""
    from dsel.driver.histogram import hlog_name, read_hlog
    from tests.support.synthetic import synthetic_factory

    specs = plan_workers(tmp_path, CELL, ("read",), rate_per_s=200.0, duration_s=3.0, workers=2)
    result = run_pool(specs, synthetic_factory)

    per_worker = sum(
        read_hlog(tmp_path / "histograms" / hlog_name("read", "corrected", w)).get_total_count()
        for w in (0, 1)
    )
    aggregate = read_hlog(result.hlogs["read/corrected"])
    assert aggregate.get_total_count() == per_worker
