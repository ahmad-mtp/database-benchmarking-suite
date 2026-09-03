"""The connection ramp (PLAN.md S16-S18a).

*Hold rate, staircase connections.* The rate ramp asks how much load an engine
can take; this asks how much **concurrency** it can take while the load stays
the same. They are different questions with different answers, and the second
is the one that catches the failure people actually hit in production: the
offered load never changed, someone raised a pool size, and throughput fell.

The whole design follows from one requirement -- **the offered load must be
identical at every step**. If the rate moved with the connection count, a fall
in throughput could be either, and the ramp would prove nothing. So:

* the arrival schedule is generated once, from `ArrivalSchedule`, and *reused
  verbatim* at every step. Not the same rate -- the same arrivals;
* the connections are all opened before the phase starts, so a step measures
  a steady state and not a connection storm;
* an arrival goes to whichever connection is free, and if none is free it
  *waits*, and the waiting lands in its latency. That is what makes the cliff
  visible rather than absorbed.

**The rung order is randomised, and that is not a nicety.** Run in ascending
order the ramp reported a clean 15% fall from 8 connections to 32 in five
consecutive runs -- and then a sixth run, on a machine that had been under
continuous load for forty minutes, came out flat at a level 15% below all of
them. The "effect" was the first rung of each ramp running on a cooler
machine than the last. Ascending order makes rung position a proxy for elapsed
time, and on this host elapsed time is a proxy for temperature. Shuffling the
order per repeat is the same block design the interference sweep uses, for the
same reason: drift must not be able to align itself with the axis.

Unlike the rate driver this runs one process holding many connections, because
the axis is connections and one OS process per connection would put the cliff
at the process table rather than at the engine. The measurement window, the
histograms and the records are the same as everywhere else.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dsel.driver.clock import wait_until
from dsel.driver.histogram import (
    CORRECTED,
    INNER,
    UNCORRECTED,
    OpRecorder,
    hlog_name,
    write_hlog,
)
from dsel.driver.scheduler import ArrivalSchedule
from dsel.driver.transport import PGBENCH_ACCOUNTS_PER_SCALE, PGBENCH_SELECT, _uniform
from dsel.live.cell import Cell
from dsel.live.ndjson import ShardWriter, now_ms
from dsel.live.schema import LatencyWindowRecord, PhaseRecord, PoolRecord

DEFAULT_WINDOW_S = 1.0


@dataclass(frozen=True, slots=True)
class ConnectionStep:
    """One rung of the staircase, as the driver saw it."""

    connections: int
    offered_rate_per_s: float
    issued: int
    completed: int
    errors: int
    achieved_rate_per_s: float
    p50_us: float
    p99_us: float
    max_wait_us: float
    cell: str


@dataclass(frozen=True, slots=True)
class ConnectionRampPlan:
    """Fixed before anything starts, including the arrival schedule."""

    run_dir: Path
    dsn: str
    connection_counts: tuple[int, ...]
    rate_per_s: float
    duration_s: float = 10.0
    warmup_s: float = 2.0
    scale: int = 5
    seed: int = 20260903
    use_case: str = "uc1"
    engine: str = "postgres"
    scenario: str = "conn-ramp"
    repeat: int = 1
    window_s: float = DEFAULT_WINDOW_S
    statement: str = PGBENCH_SELECT
    """The statement each arrival issues. Dear enough that the *engine* is the
    bottleneck, or the cliff measured is the driver's own."""
    writer_id: str = "conn-driver-0"
    """Distinct per repeat. Two passes sharing a shard would restart `seq` and
    the merge would refuse the file -- correctly, since a shard is one writer's
    forward-only record."""
    shuffle_rungs: bool = True
    """Randomise the order the rungs are run in.

    On by default because ascending order is actively misleading here: it makes
    rung position a proxy for elapsed time, and elapsed time on this host is a
    proxy for temperature. Turn it off only to reproduce that failure.
    """

    def rung_order(self) -> list[int]:
        """The indices of `connection_counts`, in the order they will run.

        Derived from the seed and the repeat, so the order is different for
        each repeat, identical on a rerun, and recorded in the bundle by
        construction -- an order nobody can reconstruct is an order nobody can
        check for accidental alignment.
        """
        order = list(range(len(self.connection_counts)))
        if not self.shuffle_rungs:
            return order
        random.Random(self.seed + self.repeat).shuffle(order)
        return order

    def cell_for(self, step: int) -> str:
        return Cell(
            use_case=self.use_case,
            engine=self.engine,
            scenario=self.scenario,
            # The rate is the same at every step *by design*; `step` is what
            # separates the cells, and the connection count is recovered from
            # what the engine reported rather than from this id.
            rate=int(self.rate_per_s),
            repeat=self.repeat,
            step=step,
        ).id

    @property
    def schedule(self) -> ArrivalSchedule:
        """One schedule, reused at every step.

        Not "the same rate at every step" -- the same *arrivals*. Two Poisson
        realisations of the same rate differ by 1/sqrt(N), which at these run
        lengths is a percent or two, and a percent or two is the size of the
        effect being looked for.
        """
        return ArrivalSchedule(rate_per_s=self.rate_per_s, worker=0, workers=1, seed=self.seed)


async def _run_step(
    plan: ConnectionRampPlan, connections: int, step: int, writer: ShardWriter
) -> ConnectionStep:
    import asyncpg

    cell = plan.cell_for(step)
    rows = plan.scale * PGBENCH_ACCOUNTS_PER_SCALE
    recorder = OpRecorder(op="select_account")

    # Every connection is open before the phase starts: a step measures a
    # steady state, not a connection storm.
    pool: list[Any] = [await asyncpg.connect(plan.dsn) for _ in range(connections)]
    free: asyncio.Queue[Any] = asyncio.Queue()
    for connection in pool:
        free.put_nowait(connection)

    writer.write(
        PhaseRecord(t_ms=now_ms(), w="", seq=0, cell=cell, phase="measure", event="begin")
    )
    issued = errors = 0
    max_wait_us = 0.0
    phase_start = time.perf_counter()
    window_started = phase_start + plan.warmup_s
    next_window = plan.warmup_s + plan.window_s
    in_flight: set[asyncio.Task[None]] = set()

    async def issue(index: int, scheduled: float) -> None:
        nonlocal issued, errors, max_wait_us
        # Waiting for a connection is part of the latency, deliberately. It is
        # the mechanism the cliff is made of; absorbing it would hide it.
        acquired_at = time.perf_counter()
        connection = await free.get()
        wait_us = (time.perf_counter() - acquired_at) * 1_000_000.0
        max_wait_us = max(max_wait_us, wait_us)
        aid = 1 + int(_uniform(plan.seed, 0, index, "aid") * rows)
        started = time.perf_counter()
        ok = True
        try:
            await connection.fetchval(plan.statement, aid)
        except Exception:
            ok = False
            errors += 1
        finally:
            free.put_nowait(connection)
        done = time.perf_counter()
        issued += 1
        if scheduled - phase_start >= plan.warmup_s:
            recorder.record(
                corrected_us=(done - scheduled) * 1_000_000.0,
                uncorrected_us=(done - acquired_at) * 1_000_000.0,
                ok=ok,
                inner_us=(done - started) * 1_000_000.0,
            )

    def flush(width_s: float) -> None:
        if width_s <= 0:
            return
        histogram, count, window_errors = recorder.take_window()
        if count == 0:
            return
        writer.write(
            LatencyWindowRecord(
                t_ms=now_ms(),
                w="",
                seq=0,
                cell=cell,
                window_ms=int(width_s * 1000),
                op="select_account",
                count=count,
                errors=window_errors,
                rate_per_s=count / width_s,
                p50_us=float(histogram.get_value_at_percentile(50.0)),
                p90_us=float(histogram.get_value_at_percentile(90.0)),
                p99_us=float(histogram.get_value_at_percentile(99.0)),
                max_us=float(histogram.get_max_value()),
            )
        )
        writer.write(
            PoolRecord(
                t_ms=now_ms(),
                w="",
                seq=0,
                cell=cell,
                pool="driver->postgres",
                size=connections,
                in_use=connections - free.qsize(),
                idle=free.qsize(),
                waiting=len(in_flight) - (connections - free.qsize()),
                acquire_wait_us_p99=max_wait_us,
            )
        )

    try:
        for index, offset in enumerate(plan.schedule.offsets_until(plan.duration_s)):
            scheduled = phase_start + offset
            wait_until(scheduled)
            task = asyncio.create_task(issue(index, scheduled))
            in_flight.add(task)
            task.add_done_callback(in_flight.discard)
            # Yield so the loop can actually progress the tasks; without this
            # the arrivals would all be created and none of them run.
            await asyncio.sleep(0)
            if time.perf_counter() - phase_start >= next_window:
                now_perf = time.perf_counter()
                flush(now_perf - window_started)
                window_started = now_perf
                next_window += plan.window_s
        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)
    finally:
        finished = time.perf_counter()
        flush(finished - window_started)
        writer.write(
            PhaseRecord(t_ms=now_ms(), w="", seq=0, cell=cell, phase="measure", event="end")
        )
        for connection in pool:
            await connection.close()

    histograms = plan.run_dir / f"step{step}" / "histograms"
    for kind, histogram in (
        (CORRECTED, recorder.corrected),
        (UNCORRECTED, recorder.uncorrected),
        (INNER, recorder.inner),
    ):
        if histogram.get_total_count():
            write_hlog(
                histograms / hlog_name("select_account", kind),
                histogram,
                start_time_s=time.time(),
                interval_end_s=finished - phase_start,
            )

    measured = max(1e-9, finished - (phase_start + plan.warmup_s))
    return ConnectionStep(
        connections=connections,
        offered_rate_per_s=plan.rate_per_s,
        issued=issued,
        completed=recorder.count,
        errors=recorder.errors,
        achieved_rate_per_s=recorder.count / measured,
        p50_us=float(recorder.corrected.get_value_at_percentile(50.0)),
        p99_us=float(recorder.corrected.get_value_at_percentile(99.0)),
        max_wait_us=max_wait_us,
        cell=cell,
    )


def run_connection_ramp(
    plan: ConnectionRampPlan,
    *,
    before_step: Callable[[int, int, str], None] | None = None,
    after_step: Callable[[ConnectionStep], None] | None = None,
) -> list[ConnectionStep]:
    """Run every rung in order, one at a time.

    Serialised for the same reason the rate ramp is: two steps in flight would
    contend, and the second would be measuring the first.

    The callbacks exist so a sampler can be started and stopped *around* each
    rung. Sampling across the gaps would attribute one rung's backends to the
    next, and the backend count is the axis.
    """
    writer = ShardWriter(plan.run_dir / "shards", plan.writer_id)
    steps: list[ConnectionStep] = []
    try:
        for index in plan.rung_order():
            count = plan.connection_counts[index]
            if before_step is not None:
                before_step(index, count, plan.cell_for(index))
            step = asyncio.run(_run_step(plan, count, index, writer))
            steps.append(step)
            if after_step is not None:
                after_step(step)
    finally:
        writer.close()
    # Returned in rung order rather than run order: the reader wants the curve,
    # and the order it was measured in is in the records.
    steps.sort(key=lambda s: s.connections)
    return steps


def table(steps: list[ConnectionStep]) -> str:
    lines = [
        f"{'conns':>6} {'offered':>8} {'achieved':>9} {'p50 us':>9} {'p99 us':>10} "
        f"{'max wait us':>12} {'errors':>7}",
        f"{'-' * 6} {'-' * 8} {'-' * 9} {'-' * 9} {'-' * 10} {'-' * 12} {'-' * 7}",
    ]
    for step in steps:
        lines.append(
            f"{step.connections:>6} {step.offered_rate_per_s:>8.0f} "
            f"{step.achieved_rate_per_s:>9.0f} {step.p50_us:>9.0f} {step.p99_us:>10.0f} "
            f"{step.max_wait_us:>12.0f} {step.errors:>7}"
        )
    return "\n".join(lines)
