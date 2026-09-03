"""The rate ramp (PLAN.md S11).

A ramp answers three questions about an engine at a fixed envelope: what rate
it sustains, where latency starts to leave, and where throughput turns over.
Each is defined here as an arithmetic rule over the ramp's own steps, because a
knee identified by eye is not a measurement and cannot be re-derived from the
bundle.

* **Max sustainable rate** -- the highest offered rate the target actually
  delivered: achieved within `DELIVERY_TOLERANCE` of offered, errors under
  `ERROR_TOLERANCE`, and no driver-bound verdict. A step the driver could not
  deliver says nothing about the target, so it cannot be the answer.
* **Knee** -- the lowest offered rate whose p99 exceeds `KNEE_FACTOR` times the
  baseline p99, where the baseline is the *first* step. Latency, not
  throughput: throughput is still fine at the knee, which is the point.
* **Collapse** -- the lowest offered rate whose achieved throughput has fallen
  more than `COLLAPSE_DROP` below the best achieved so far, or whose error rate
  exceeds `COLLAPSE_ERRORS`. Offering more and getting less is the definition;
  a plateau is saturation, not collapse.

**Cells run serialised.** Two rates in flight at once contend, and the second
would measure the first. The ramp runs one step at a time, in order, and the
order is fixed before it starts.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from dsel.driver.pool import DriverResult, TransportFactory, plan_workers, run_pool

DELIVERY_TOLERANCE = 0.03
ERROR_TOLERANCE = 0.001
KNEE_FACTOR = 2.0
COLLAPSE_DROP = 0.05
COLLAPSE_ERRORS = 0.01


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
    def delivered(self) -> bool:
        """Whether the offered rate was actually put on the wire."""
        if self.offered_rate_per_s <= 0:
            return False
        shortfall = 1.0 - self.achieved_rate_per_s / self.offered_rate_per_s
        return (
            shortfall <= DELIVERY_TOLERANCE
            and self.error_rate <= ERROR_TOLERANCE
            and self.verdict == "OK"
        )


@dataclass(frozen=True, slots=True)
class Ramp:
    """The whole ramp, and the three numbers derived from it."""

    steps: tuple[RampStep, ...] = ()
    ops: tuple[str, ...] = ()
    knee_factor: float = KNEE_FACTOR

    @property
    def baseline_p99_us(self) -> float:
        return self.steps[0].p99_us if self.steps else 0.0

    @property
    def max_sustainable_rate_per_s(self) -> float | None:
        delivered = [step for step in self.steps if step.delivered]
        return max((step.offered_rate_per_s for step in delivered), default=None)

    @property
    def knee_rate_per_s(self) -> float | None:
        """The first rate at which latency has clearly left its baseline."""
        baseline = self.baseline_p99_us
        if baseline <= 0:
            return None
        for step in self.steps[1:]:
            if step.p99_us > baseline * self.knee_factor:
                return step.offered_rate_per_s
        return None

    @property
    def collapse_rate_per_s(self) -> float | None:
        """The first rate at which offering more returned less."""
        best = 0.0
        for step in self.steps:
            if step.error_rate > COLLAPSE_ERRORS:
                return step.offered_rate_per_s
            if best > 0 and step.achieved_rate_per_s < best * (1.0 - COLLAPSE_DROP):
                return step.offered_rate_per_s
            best = max(best, step.achieved_rate_per_s)
        return None

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
    cell_prefix: str
    ops: tuple[str, ...]
    rates_per_s: tuple[float, ...]
    duration_s: float = 5.0
    warmup_s: float = 1.0
    workers: int = 4
    seed: int = 20260903

    def cell_for(self, step: int) -> str:
        return f"{self.cell_prefix}/step{step}"


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
