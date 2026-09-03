"""S10 acceptance: a third party recomputes the percentiles.

*PLAN.md S10:* "Accept, and verify here not at M7: a `.hlog` written from
Python is read by the Java `HistogramLogProcessor` in a pinned JDK container
with p50/p99/p99.9 matching within one bucket. The entire 'third party
recomputes percentiles' claim rests on this."

The bundle carries raw histograms so someone else can recompute rather than
trust. Checking the Python writer with the Python reader would prove nothing --
a shared bug cannot see itself -- so the reference Java implementation reads the
file instead, from a pinned jar in a pinned JDK image, with neither the jar nor
the log bind-mounted.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from dsel.audit.hlog_check import (
    JAR_SHA256,
    HlogCheckError,
    ensure_jar,
    java_percentiles,
    parse_percentile_output,
    within_one_bucket,
)
from dsel.driver.histogram import hlog_name, read_hlog, value_at_count
from dsel.driver.pool import plan_workers, run_pool
from dsel.driver.transport import SyntheticTransport
from dsel.driver.worker import WorkerSpec, run_worker
from tests.conftest import requires_docker

CELL = "uc1/postgres/oltp-mixed/r300/rep1"
POINTS = (50.0, 99.0, 99.9)

SAMPLE_OUTPUT = """#[StartTime: 1788424354.688 (seconds since epoch), Thu Sep 03 08:32 UTC]
       Value     Percentile TotalCount 1/(1-Percentile)

     266.000 0.000000000000         23           1.00
     800.000 0.500000000000      10011           2.00
    2351.000 0.990000000000      19801         100.00
    2397.000 0.999000000000      19981        1000.00
    2403.000 1.000000000000      20000
#[Mean    =      970.942, StdDeviation   =      594.188]
#[Max     =     2403.000, Total count    =        20000]
#[Buckets =           20, SubBuckets     =         2048]
"""


def test_the_processor_output_parses() -> None:
    """Parsed without a container, so a format change is a fast failure."""
    table = parse_percentile_output(SAMPLE_OUTPUT)
    assert table.total_count == 20_000
    assert table.max_value == 2403.0
    assert (table.row_at(50.0).quantile, table.row_at(50.0).value) == (0.5, 800.0)
    assert table.row_at(99.0).count == 19801
    # 99.9 / 100 is 0.9990000000000001; without the epsilon this reads 2403.
    assert table.row_at(99.9).value == 2397.0


def test_empty_output_is_an_error_not_an_empty_table() -> None:
    with pytest.raises(HlogCheckError, match="no percentile rows"):
        parse_percentile_output("#[Max = 0, Total count = 0]\n")


def test_one_bucket_is_relative_not_absolute() -> None:
    """0.1% at 200 us is 0.2 us; the same absolute tolerance at 2 s would be
    meaningless in the other direction."""
    assert within_one_bucket(1000.0, 1001.0)
    assert not within_one_bucket(1000.0, 1050.0)
    assert within_one_bucket(2_000_000.0, 2_002_000.0)
    assert within_one_bucket(0.0, 0.0)


@requires_docker
@pytest.mark.slow
def test_the_pinned_jar_is_what_it_claims_to_be() -> None:
    jar = ensure_jar()
    assert hashlib.sha256(jar.read_bytes()).hexdigest() == JAR_SHA256


@requires_docker
@pytest.mark.slow
def test_java_reads_a_python_written_hlog_and_agrees(tmp_path: Path) -> None:
    """The acceptance. A real driver run, read back by the reference Java
    implementation, agreeing at p50, p99 and p99.9 within one bucket."""
    spec = WorkerSpec(
        worker=0,
        workers=1,
        run_dir=tmp_path,
        cell=CELL,
        ops=("read",),
        rate_per_s=300.0,
        duration_s=6.0,
    )
    transport = SyntheticTransport(median_us=700.0, sigma=0.6, worker=0, seed=spec.seed)
    result = run_worker(spec, transport)
    assert not result.driver_bound, result.verdicts

    hlog = result.hlogs["read/corrected"]
    python = read_hlog(hlog)
    assert python.get_total_count() > 1000, "the log should hold a real sample"

    _, table = java_percentiles(hlog, POINTS, run_id="s10-accept")

    assert table.total_count == python.get_total_count(), (
        f"Java counted {table.total_count} values, Python {python.get_total_count()}"
    )

    # Compared at the cumulative count, not at a nominal percentile. The
    # processor prints the percentile *iterator*, whose steps are forced
    # strictly increasing and land on 0.990234375 rather than 0.99, so a
    # nominal comparison measures the two implementations' percentile
    # conventions rather than their agreement about the data. "The value at
    # which the cumulative count first reaches k" means one thing in both.
    disagreements = []
    reported = []
    for point in POINTS:
        row = table.row_at(point)
        mine = value_at_count(python, row.count)
        reported.append(f"p{point:g} {mine:.0f}/{row.value:.0f}us")
        if not within_one_bucket(mine, row.value):
            disagreements.append(
                f"p{point:g} (count {row.count}): python {mine} vs java {row.value}"
            )
    assert not disagreements, "\n".join(disagreements)

    # Every printed row, not only three: a disagreement anywhere would mean the
    # bundle's raw histogram cannot be recomputed by another reader.
    off = [
        (row.count, row.value, value_at_count(python, row.count))
        for row in table.rows
        if 0 < row.count <= table.total_count
        and not within_one_bucket(row.value, value_at_count(python, row.count))
    ]
    assert not off, f"{len(off)} of {len(table.rows)} rows disagree, first {off[:3]}"

    # And the nominal comparison, recorded rather than asserted: this is the
    # gap the count-based comparison exists to explain.
    nominal = max(
        abs(float(python.get_value_at_percentile(p)) - table.row_at(p).value)
        / table.row_at(p).value
        for p in POINTS
    )
    print(
        f"\n  python vs java at matched counts: {', '.join(reported)}  "
        f"count {table.total_count}, all {len(table.rows)} printed rows agree.\n"
        f"  comparing at nominal percentiles instead would differ by up to "
        f"{nominal * 100:.2f}% -- the iterator's step levels, not the data."
    )


@requires_docker
@pytest.mark.slow
def test_java_agrees_on_the_aggregated_multi_worker_log(tmp_path: Path) -> None:
    """The log the bundle actually carries is the summed one, so that is the
    one a third party has to be able to read."""
    from tests.support.synthetic import synthetic_factory

    specs = plan_workers(tmp_path, CELL, ("read",), rate_per_s=300.0, duration_s=5.0, workers=3)
    result = run_pool(specs, synthetic_factory)

    aggregate = result.hlogs["read/corrected"]
    python = read_hlog(aggregate)
    _, table = java_percentiles(aggregate, POINTS, run_id="s10-aggregate")

    per_worker = sum(
        read_hlog(tmp_path / "histograms" / hlog_name("read", "corrected", w)).get_total_count()
        for w in range(3)
    )
    assert table.total_count == per_worker, "the summed log lost values"
    for point in POINTS:
        row = table.row_at(point)
        mine = value_at_count(python, row.count)
        assert within_one_bucket(mine, row.value), (
            f"p{point:g} (count {row.count}): python {mine} vs java {row.value}"
        )
