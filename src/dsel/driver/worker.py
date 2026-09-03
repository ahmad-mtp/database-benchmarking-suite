"""One driver worker (PLAN.md S10).

A worker owns one arrival schedule, one transport, one shard of
`metrics.ndjson` and one pair of histograms per operation. It never
coordinates with another worker: shards are merged afterwards by
`(t_ms, w, seq)`, and the arrival schedules are independent Poisson processes
whose superposition is the offered rate.

**D6: the driver is multi-process from the start.** A saturated single Python
process reads about 25% of a 4-core quota, so a gate written against the
container's CPU silently never fires. The gate that matters is therefore
per-worker and measured from inside the worker -- `getrusage` on itself,
against wall clock, as a fraction of *one* core.

Two more gates are implementation-independent, and both are about **schedule
lag** -- the gap between when an arrival was due and when it was issued.

*Falling behind.* If a worker cannot start requests at their scheduled time,
the load it offered was not the load the spec asked for, whatever the CPU says.
Lag p99 above twice the mean inter-arrival means systematically a whole request
behind, not merely jittering.

*Drowning the signal.* A worker can keep up on average and still contribute
most of the latency it reports. Corrected latency is measured from the
scheduled start, so the driver's own lateness is inside every number; when the
median lag exceeds the median service time -- the uncorrected p50, which is
what the target actually took -- the figure describes the driver rather than
the engine. Measured before `driver/clock.py` existed, a target with a 500 us
service time reported a 3.7 ms p50, all of it the host's sleep overshoot. That
is the failure this gate names.

Both gates mark the cell rather than annotate it. `INCONCLUSIVE_DRIVER_BOUND`
is a distinct verdict from `INVALID`: the run is not wrong, it is unanswerable
at this offered rate on this machine.

**One request in flight per worker.** A worker issues, waits, and moves to the
next scheduled arrival, so a pool of `n` workers cannot deliver more than
`n / service_time` however high the offered rate is set. That ceiling is not
hidden: past it the worker falls behind its schedule, the lateness lands in
every latency it records, and the lag gate fires. The failure mode of a
closed-loop driver -- a good-looking p99 obtained by quietly asking for less --
is the one thing this cannot do.
"""

from __future__ import annotations

import os
import resource
import time
from dataclasses import dataclass, field
from pathlib import Path

from dsel.driver.clock import wait_until
from dsel.driver.histogram import (
    CORRECTED,
    INNER,
    UNCORRECTED,
    OpRecorder,
    hlog_name,
    percentiles,
    write_hlog,
)
from dsel.driver.scheduler import ArrivalSchedule
from dsel.driver.transport import Transport, TransportError
from dsel.live.ndjson import ShardWriter, now_ms
from dsel.live.schema import LatencyWindowRecord, PhaseRecord, ValidityRecord

# The gates. PLAN.md fixes the CPU one; the lag one is stated here because
# nothing upstream defines it.
WORKER_CPU_LIMIT = 0.70
LAG_INTERVALS_LIMIT = 2.0
LAG_FLOOR_US = 1000.0
# Above this share the reported latency is mostly the driver's own lateness.
LAG_SHARE_LIMIT = 0.5

GATE_WORKER_CPU = "driver_worker_cpu"
GATE_SCHEDULE_LAG = "driver_schedule_lag"
GATE_LAG_SHARE = "driver_lag_share"

DEFAULT_WINDOW_S = 1.0


@dataclass(frozen=True, slots=True)
class WorkerSpec:
    """Everything one worker needs, and nothing it shares."""

    worker: int
    workers: int
    run_dir: Path
    cell: str
    ops: tuple[str, ...]
    rate_per_s: float
    duration_s: float
    warmup_s: float = 0.0
    seed: int = 20260903
    window_s: float = DEFAULT_WINDOW_S

    @property
    def schedule(self) -> ArrivalSchedule:
        return ArrivalSchedule(
            rate_per_s=self.rate_per_s,
            worker=self.worker,
            workers=self.workers,
            seed=self.seed,
        )


@dataclass(slots=True)
class WorkerResult:
    """What a worker hands back. Histograms travel as encoded log files."""

    worker: int
    issued: int = 0
    completed: int = 0
    errors: int = 0
    cpu_fraction: float = 0.0
    lag_p99_us: float = 0.0
    lag_p50_us: float = 0.0
    service_p50_us: float = 0.0
    mean_interval_us: float = 0.0
    achieved_rate_per_s: float = 0.0
    hlogs: dict[str, Path] = field(default_factory=dict)
    summary: dict[str, dict[str, float]] = field(default_factory=dict)
    verdicts: dict[str, str] = field(default_factory=dict)

    @property
    def lag_share(self) -> float:
        """How much of the reported latency the driver contributed itself."""
        total = self.lag_p50_us + self.service_p50_us
        return self.lag_p50_us / total if total > 0 else 0.0

    @property
    def driver_bound(self) -> bool:
        return any(v == "INCONCLUSIVE_DRIVER_BOUND" for v in self.verdicts.values())


def _cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def run_worker(spec: WorkerSpec, transport: Transport) -> WorkerResult:
    """Drive one worker's schedule to completion.

    The schedule is walked forward and never adjusted. If a request is issued
    late, the lateness is carried into the latency that request records --
    which is the whole point of an open loop.
    """
    writer = ShardWriter(spec.run_dir / "shards", f"driver-{spec.worker}")
    recorders = {op: OpRecorder(op=op) for op in spec.ops}
    schedule = spec.schedule
    mean_interval_us = schedule.mean_interval_s * 1_000_000.0
    lag_limit_us = max(LAG_FLOOR_US, LAG_INTERVALS_LIMIT * mean_interval_us)

    issued = errors = 0
    measure_from = spec.warmup_s
    # The measurement window opens when the warmup closes, not when the phase
    # starts. Left at the phase start, the first window spans the warmup and
    # charges its seconds to the handful of arrivals that happened to fall
    # after it -- measured, a 1150 ms first window holding one operation,
    # which put the rate re-derived from the file 25% under the truth.
    next_window = spec.warmup_s + spec.window_s

    # Connecting is `init`, not `measure`. Opening the transport inside the
    # window puts connection setup into the denominator of the achieved rate
    # and into the first latencies -- measured against pgbench, that alone
    # accounted for a 1.6% rate shortfall the driver did not have.
    transport.open()
    cpu_before = _cpu_seconds()
    phase_start = time.perf_counter()
    window_started = phase_start + spec.warmup_s
    wall_start = time.time()
    try:
        if spec.worker == 0:
            writer.write(
                PhaseRecord(
                    t_ms=now_ms(), w="", seq=0, cell=spec.cell, phase="measure", event="begin"
                )
            )
        for index, offset in enumerate(schedule.offsets_until(spec.duration_s)):
            scheduled = phase_start + offset
            now = time.perf_counter()
            if now < scheduled:
                wait_until(scheduled)
            actual = time.perf_counter()
            op = spec.ops[index % len(spec.ops)]
            ok = True
            try:
                transport.execute(op, index)
            except TransportError:
                ok = False
                errors += 1
            done = time.perf_counter()
            issued += 1
            # Warmup arrivals are driven but not recorded: the point of a
            # warmup is that its numbers do not enter the result.
            if offset >= measure_from:
                recorders[op].record(
                    corrected_us=(done - scheduled) * 1_000_000.0,
                    uncorrected_us=(done - actual) * 1_000_000.0,
                    ok=ok,
                    inner_us=getattr(transport, "last_inner_us", None),
                )
            if done - phase_start >= next_window:
                now_perf = time.perf_counter()
                _emit_windows(writer, spec, recorders, now_perf - window_started)
                window_started = now_perf
                next_window += spec.window_s
    finally:
        transport.close()

    finished = time.perf_counter()
    elapsed = finished - phase_start
    # The achieved rate is over the *measured* span, not the whole phase.
    # Counting warmup arrivals against a denominator that includes the warmup
    # nearly cancels, but not exactly, and the two definitions then disagree
    # between the run's own answer and the one re-derived from the file (S15).
    # One definition, two data paths.
    measured_span = max(1e-9, finished - (phase_start + spec.warmup_s))
    cpu_fraction = (_cpu_seconds() - cpu_before) / elapsed if elapsed > 0 else 0.0
    # The final window is a partial one. Emitting it at the nominal width would
    # claim a second of coverage for what may be a tenth of one, and any rate
    # derived from the file afterwards -- the re-derived curve, the Prometheus
    # series -- would come out low in proportion. Measured, a 4 s ramp step
    # with a 1 s window read 25% under its true rate.
    _emit_windows(writer, spec, recorders, finished - window_started)

    result = WorkerResult(
        worker=spec.worker,
        issued=issued,
        completed=sum(r.count for r in recorders.values()),
        errors=errors,
        cpu_fraction=cpu_fraction,
        lag_p99_us=max((r.lag_p99_us for r in recorders.values()), default=0.0),
        lag_p50_us=max((r.lag_p50_us for r in recorders.values()), default=0.0),
        service_p50_us=max((r.service_p50_us for r in recorders.values()), default=0.0),
        mean_interval_us=mean_interval_us,
        achieved_rate_per_s=sum(r.count for r in recorders.values()) / measured_span,
    )

    histograms = spec.run_dir / "histograms"
    for op, recorder in recorders.items():
        if recorder.count == 0:
            continue
        for kind, histogram in (
            (CORRECTED, recorder.corrected),
            (UNCORRECTED, recorder.uncorrected),
            (INNER, recorder.inner),
        ):
            if histogram.get_total_count() == 0:
                continue
            path = write_hlog(
                histograms / hlog_name(op, kind, spec.worker),
                histogram,
                start_time_s=wall_start,
                interval_end_s=elapsed,
            )
            result.hlogs[f"{op}/{kind}"] = path
        result.summary[op] = percentiles(recorder.corrected)

    _emit_gates(writer, spec, result, lag_limit_us)
    if spec.worker == 0:
        writer.write(
            PhaseRecord(
                t_ms=now_ms(), w="", seq=0, cell=spec.cell, phase="measure", event="end"
            )
        )
    writer.close()
    return result


def _emit_windows(
    writer: ShardWriter, spec: WorkerSpec, recorders: dict[str, OpRecorder], window_s: float
) -> None:
    """Publish within-window estimates. For watching, never for reporting.

    `window_s` is the width the window actually covered, not the width it was
    aimed at. The boundary advances whether or not anything was recorded, so a
    warmup window that emitted nothing does not leave its seconds inside the
    next window's width.
    """
    if window_s <= 0:
        return
    for op, recorder in recorders.items():
        histogram, count, errors = recorder.take_window()
        if count == 0:
            continue
        writer.write(
            LatencyWindowRecord(
                t_ms=now_ms(),
                w="",
                seq=0,
                cell=spec.cell,
                window_ms=int(window_s * 1000),
                op=op,
                count=count,
                errors=errors,
                rate_per_s=count / window_s,
                p50_us=float(histogram.get_value_at_percentile(50.0)),
                p90_us=float(histogram.get_value_at_percentile(90.0)),
                p99_us=float(histogram.get_value_at_percentile(99.0)),
                max_us=float(histogram.get_max_value()),
            )
        )


def _emit_gates(
    writer: ShardWriter, spec: WorkerSpec, result: WorkerResult, lag_limit_us: float
) -> None:
    """Emit both driver gates, whether or not they fired."""
    cpu_verdict = (
        "INCONCLUSIVE_DRIVER_BOUND" if result.cpu_fraction > WORKER_CPU_LIMIT else "OK"
    )
    lag_verdict = "INCONCLUSIVE_DRIVER_BOUND" if result.lag_p99_us > lag_limit_us else "OK"
    share = result.lag_share
    share_verdict = "INCONCLUSIVE_DRIVER_BOUND" if share > LAG_SHARE_LIMIT else "OK"
    result.verdicts = {
        f"{GATE_WORKER_CPU}[{spec.worker}]": cpu_verdict,
        f"{GATE_SCHEDULE_LAG}[{spec.worker}]": lag_verdict,
        f"{GATE_LAG_SHARE}[{spec.worker}]": share_verdict,
    }
    writer.write(
        ValidityRecord(
            t_ms=now_ms(),
            w="",
            seq=0,
            cell=spec.cell,
            gate=f"{GATE_WORKER_CPU}[{spec.worker}]",
            verdict=cpu_verdict,  # type: ignore[arg-type]
            observed=round(result.cpu_fraction, 4),
            limit=WORKER_CPU_LIMIT,
            detail=(
                f"pid {os.getpid()} used {result.cpu_fraction:.1%} of one core; "
                "measured per worker because a whole-container gate cannot fire "
                "on a single saturated Python process (D6)"
            ),
        )
    )
    writer.write(
        ValidityRecord(
            t_ms=now_ms(),
            w="",
            seq=0,
            cell=spec.cell,
            gate=f"{GATE_SCHEDULE_LAG}[{spec.worker}]",
            verdict=lag_verdict,  # type: ignore[arg-type]
            observed=round(result.lag_p99_us, 1),
            limit=round(lag_limit_us, 1),
            detail=(
                f"p99 lag against a {result.mean_interval_us:.0f} us mean "
                "inter-arrival; a worker that cannot start on time did not offer "
                "the rate the spec asked for"
            ),
        )
    )
    writer.write(
        ValidityRecord(
            t_ms=now_ms(),
            w="",
            seq=0,
            cell=spec.cell,
            gate=f"{GATE_LAG_SHARE}[{spec.worker}]",
            verdict=share_verdict,  # type: ignore[arg-type]
            observed=round(share, 4),
            limit=LAG_SHARE_LIMIT,
            detail=(
                f"median lag {result.lag_p50_us:.0f} us against a median service "
                f"time of {result.service_p50_us:.0f} us; past half, the reported "
                "latency describes the driver rather than the engine"
            ),
        )
    )
