"""The multi-process worker pool (PLAN.md S10, D6).

One process per worker, started with the `spawn` context so a worker inherits
nothing but its spec -- no partially-initialised connections, no copied random
state, and the same start-up path on macOS and Linux.

The supervisor does three things the workers cannot do for themselves:

* it holds the run's start instant, so every worker's schedule is anchored to
  the same zero and the arrival processes actually superpose;
* it sums the per-worker histograms into the run-level `.hlog` that the audit
  bundle carries. Summing HdrHistograms is exact -- they are counts per bucket
  -- so the aggregate is not an approximation of the workers, it *is* them;
* it decides the cell's verdict. One driver-bound worker makes the cell
  driver-bound: the offered load was not delivered, and averaging that away
  across the other workers would hide it.
"""

from __future__ import annotations

import multiprocessing as mp
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from dsel.driver.histogram import (
    CORRECTED,
    UNCORRECTED,
    hlog_name,
    new_histogram,
    percentiles,
    read_hlog,
    write_hlog,
)
from dsel.driver.transport import Transport
from dsel.driver.worker import WorkerResult, WorkerSpec, run_worker

TransportFactory = Callable[[WorkerSpec], Transport]


@dataclass(frozen=True, slots=True)
class DriverResult:
    """The cell's load-generation result."""

    workers: tuple[WorkerResult, ...]
    hlogs: dict[str, Path] = field(default_factory=dict)
    summary: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def issued(self) -> int:
        return sum(worker.issued for worker in self.workers)

    @property
    def completed(self) -> int:
        return sum(worker.completed for worker in self.workers)

    @property
    def errors(self) -> int:
        return sum(worker.errors for worker in self.workers)

    @property
    def achieved_rate_per_s(self) -> float:
        return sum(worker.achieved_rate_per_s for worker in self.workers)

    @property
    def max_cpu_fraction(self) -> float:
        return max((worker.cpu_fraction for worker in self.workers), default=0.0)

    @property
    def verdict(self) -> str:
        """One driver-bound worker is enough to make the cell driver-bound."""
        return (
            "INCONCLUSIVE_DRIVER_BOUND"
            if any(worker.driver_bound for worker in self.workers)
            else "OK"
        )


def _entry(spec: WorkerSpec, factory: TransportFactory) -> WorkerResult:
    return run_worker(spec, factory(spec))


def run_pool(
    specs: list[WorkerSpec],
    factory: TransportFactory,
    *,
    ops: tuple[str, ...] | None = None,
) -> DriverResult:
    """Run every worker, then aggregate their logs into the run-level ones."""
    if not specs:
        raise ValueError("no workers")
    run_dir = specs[0].run_dir
    ops = ops or specs[0].ops

    if len(specs) == 1:
        results = [_entry(specs[0], factory)]
    else:
        context = mp.get_context("spawn")
        with context.Pool(processes=len(specs)) as pool:
            results = pool.starmap(_entry, [(spec, factory) for spec in specs])

    histograms = run_dir / "histograms"
    aggregate: dict[str, Path] = {}
    summary: dict[str, dict[str, float]] = {}
    for op in ops:
        for kind in (CORRECTED, UNCORRECTED):
            total = new_histogram()
            found = False
            for spec in specs:
                path = histograms / hlog_name(op, kind, spec.worker)
                if path.is_file():
                    total.add(read_hlog(path))
                    found = True
            if not found:
                continue
            merged = write_hlog(
                histograms / hlog_name(op, kind),
                total,
                start_time_s=0.0,
                interval_end_s=specs[0].duration_s,
            )
            aggregate[f"{op}/{kind}"] = merged
            if kind == CORRECTED:
                summary[op] = percentiles(total)

    return DriverResult(workers=tuple(results), hlogs=aggregate, summary=summary)


def plan_workers(
    run_dir: Path,
    cell: str,
    ops: tuple[str, ...],
    rate_per_s: float,
    duration_s: float,
    *,
    workers: int = 4,
    warmup_s: float = 0.0,
    seed: int = 20260903,
    window_s: float = 1.0,
) -> list[WorkerSpec]:
    """The worker specs for one cell. Fixed before anything starts."""
    return [
        WorkerSpec(
            worker=index,
            workers=workers,
            run_dir=run_dir,
            cell=cell,
            ops=ops,
            rate_per_s=rate_per_s,
            duration_s=duration_s,
            warmup_s=warmup_s,
            seed=seed,
            window_s=window_s,
        )
        for index in range(workers)
    ]
