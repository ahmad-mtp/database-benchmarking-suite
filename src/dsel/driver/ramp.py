"""The rate ramp (PLAN.md S11).

A ramp runs one offered rate after another and reduces each to a step. **It
does not decide what the steps mean.** Knee, collapse and max sustainable rate
are defined once, in `dsel.phenomena.conn_cliff`, and applied here to steps
held in memory and there to records read from a file. That is what makes S15's
criterion reachable: *an independent script re-derives knee and collapse from
`metrics.ndjson` alone*. Two definitions of a knee would drift, and the
re-derivation would prove nothing.

Each step's cell id carries its offered rate, because the cell id is the
run-matrix coordinate and the rate is one of its axes. A ramp whose steps all
shared a cell id would be unreadable from the metrics file afterwards.

**Cells run serialised.** Two rates in flight at once contend, and the second
would measure the first. The ramp runs one step at a time, in order, and the
order is fixed before it starts.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from dsel.driver.pool import DriverResult, TransportFactory, plan_workers, run_pool
from dsel.live.cell import Cell
from dsel.phenomena.conn_cliff import (
    COLLAPSE_DROP,
    COLLAPSE_ERRORS,
    DELIVERY_TOLERANCE,
    ERROR_TOLERANCE,
    KNEE_FACTOR,
    Curve,
    Point,
)

__all__ = [
    "COLLAPSE_DROP",
    "COLLAPSE_ERRORS",
    "DELIVERY_TOLERANCE",
    "ERROR_TOLERANCE",
    "KNEE_FACTOR",
    "Ramp",
    "RampPlan",
    "RampStep",
    "geometric_rates",
    "linear_rates",
    "run_ramp",
    "summarise",
]


@dataclass(frozen=True, slots=True)
class RampStep:
    """One rate, run to completion, with its verdict attached."""

    offered_rate_per_s: float
    achieved_rate_per_s: float
    completed: int
    errors: int
    p50_us: float
    p99_us: float
    verdict: str
    max_worker_cpu: float

    @property
    def error_rate(self) -> float:
        return self.errors / self.completed if self.completed else 0.0

    @property
    def point(self) -> Point:
        """This step as a `phenomena` point, which is where the rules live."""
        return Point(
            offered_rate_per_s=self.offered_rate_per_s,
            achieved_rate_per_s=self.achieved_rate_per_s,
            completed=self.completed,
            errors=self.errors,
            p99_us=self.p99_us,
            verdict=self.verdict,
        )

    @property
    def delivered(self) -> bool:
        """Whether the offered rate was actually put on the wire."""
        return self.point.delivered


@dataclass(frozen=True, slots=True)
class Ramp:
    """The whole ramp, and the three numbers derived from it."""

    steps: tuple[RampStep, ...] = ()
    ops: tuple[str, ...] = ()

    @property
    def curve(self) -> Curve:
        return Curve(points=tuple(step.point for step in self.steps))

    @property
    def baseline_p99_us(self) -> float:
        return self.steps[0].p99_us if self.steps else 0.0

    @property
    def max_sustainable_rate_per_s(self) -> float | None:
        return self.curve.max_sustainable_rate_per_s

    @property
    def knee_rate_per_s(self) -> float | None:
        """The first rate at which latency has clearly left its baseline."""
        return self.curve.knee_rate_per_s

    @property
    def collapse_rate_per_s(self) -> float | None:
        """The first rate at which offering more returned less."""
        return self.curve.collapse_rate_per_s

    def table(self) -> str:
        """The ramp as it should be read: offered against achieved, then latency."""
        lines = [
            f"{'offered':>9} {'achieved':>9} {'p50 us':>9} {'p99 us':>10} "
            f"{'err':>7} {'cpu':>6}  verdict",
            f"{'-' * 9} {'-' * 9} {'-' * 9} {'-' * 10} {'-' * 7} {'-' * 6}  {'-' * 24}",
        ]
        for step in self.steps:
            lines.append(
                f"{step.offered_rate_per_s:>9.0f} {step.achieved_rate_per_s:>9.0f} "
                f"{step.p50_us:>9.0f} {step.p99_us:>10.0f} "
                f"{step.error_rate:>6.2%} {step.max_worker_cpu:>5.0%}  {step.verdict}"
            )
        for label, value in (
            ("max sustainable", self.max_sustainable_rate_per_s),
            ("knee", self.knee_rate_per_s),
            ("collapse", self.collapse_rate_per_s),
        ):
            lines.append(f"{label:>15}: {'not reached' if value is None else f'{value:.0f}/s'}")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class RampPlan:
    """A ramp fixed before it runs. The rate order is not adaptive."""

    run_dir: Path
    ops: tuple[str, ...]
    rates_per_s: tuple[float, ...]
    use_case: str = "uc1"
    engine: str = "postgres"
    scenario: str = "oltp-read"
    repeat: int = 1
    duration_s: float = 5.0
    warmup_s: float = 1.0
    workers: int = 4
    seed: int = 20260903

    def cell_for(self, step: int) -> str:
        """The run-matrix coordinate for one step, rate included.

        The rate has to be in the id: it is an axis of the matrix, and a file
        whose cells did not carry it could not be turned back into a curve.
        """
        return Cell(
            use_case=self.use_case,
            engine=self.engine,
            scenario=self.scenario,
            rate=int(self.rates_per_s[step]),
            repeat=self.repeat,
            step=step,
        ).id


def summarise(plan: RampPlan, offered: float, result: DriverResult) -> RampStep:
    """Reduce one cell's driver result to a ramp step."""
    from dsel.driver.histogram import read_hlog

    p50 = p99 = 0.0
    total = 0
    for op in plan.ops:
        path = result.hlogs.get(f"{op}/corrected")
        if path is None:
            continue
        histogram = read_hlog(path)
        count = histogram.get_total_count()
        if count == 0:
            continue
        # Weighted by count, so a rare operation does not swing the step.
        p50 += float(histogram.get_value_at_percentile(50.0)) * count
        p99 += float(histogram.get_value_at_percentile(99.0)) * count
        total += count
    if total:
        p50 /= total
        p99 /= total
    return RampStep(
        offered_rate_per_s=offered,
        achieved_rate_per_s=result.achieved_rate_per_s,
        completed=result.completed,
        errors=result.errors,
        p50_us=p50,
        p99_us=p99,
        verdict=result.verdict,
        max_worker_cpu=result.max_cpu_fraction,
    )


def run_ramp(
    plan: RampPlan,
    factory: TransportFactory,
    *,
    on_step: Callable[[RampStep], None] | None = None,
) -> Ramp:
    """Run every rate in order, one at a time.

    Serialised deliberately: two cells in flight contend for the same cores and
    the second measures the first.
    """
    steps: list[RampStep] = []
    for index, rate in enumerate(plan.rates_per_s):
        cell_dir = plan.run_dir / f"step{index}"
        specs = plan_workers(
            cell_dir,
            plan.cell_for(index),
            plan.ops,
            rate_per_s=rate,
            duration_s=plan.duration_s,
            workers=plan.workers,
            warmup_s=plan.warmup_s,
            seed=plan.seed,
        )
        result = run_pool(specs, factory)
        step = summarise(plan, rate, result)
        steps.append(step)
        if on_step is not None:
            on_step(step)
    return Ramp(steps=tuple(steps), ops=plan.ops)


def geometric_rates(start: float, stop: float, count: int) -> tuple[float, ...]:
    """Rates spaced geometrically. A knee is easier to bracket in ratios."""
    if count < 2:
        return (start,)
    ratio = (stop / start) ** (1.0 / (count - 1))
    return tuple(round(start * ratio**index) for index in range(count))


def linear_rates(start: float, stop: float, step: float) -> tuple[float, ...]:
    rates: list[float] = []
    current = start
    while current <= stop + 1e-9:
        rates.append(current)
        current += step
    return tuple(rates)


def rates_from(*values: Sequence[float]) -> tuple[float, ...]:
    return tuple(float(v) for group in values for v in group)
