"""Per-backend memory growth (PLAN.md S16-S18b).

*Accept: over a >=1 h soak the per-backend RSS slope has a bootstrap CI
excluding zero.*

The question is whether a Postgres backend's resident memory grows with the age
of the connection. It matters because it turns a connection count into a memory
limit: if each backend gains a megabyte an hour, a pool of 200 long-lived
connections is a different machine after a day than it was at start-up, and the
capacity number taken on the first afternoon is wrong.

Three things about the method are deliberate.

**The slope is per backend, then pooled.** Fitting one line through every
sample from every backend would mix growth *within* a connection with the
difference *between* connections -- backends started at different times sit at
different points on the curve, and a pooled fit reads that spread as slope.
Each backend gets its own least-squares fit over its own life; the pooled
statistic is the median of those slopes.

**The confidence interval is a bootstrap over backends, not over samples.**
Samples within one backend are serially correlated -- consecutive readings of
the same process are nearly the same number -- so resampling them would
manufacture confidence out of autocorrelation. Backends are the independent
unit, so backends are what gets resampled. The same reasoning as the
interference sweep's block design.

**A backend with too few samples is excluded, not fitted.** A slope through
three points spanning ninety seconds is not an hourly growth rate, and letting
it into the pool would widen the interval with noise while pretending to be
evidence.

**A degenerate interval is reported as degenerate.** Measured on a first soak:
24 connections doing identical work produced 21 identical slopes, so every
bootstrap resample had the same median and the interval came out with zero
width. That is not high confidence -- it is the resampling unit having no
variation, and an interval that excludes zero because it has no width excludes
zero for a reason that has nothing to do with the data. `degenerate` says so,
and a caller that treats `excludes_zero` as evidence without checking it is
reading a coincidence. The fix on the measurement side is heterogeneous
connections, because production connections are not clones either.

Reads `metrics.ndjson` and nothing else.
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Iterable
from dataclasses import dataclass

from dsel.live.schema import AnyRecord, BackendRecord

# A backend needs this many readings, spanning this long, before its slope
# means anything.
MIN_SAMPLES = 8
MIN_SPAN_S = 300.0
BOOTSTRAP_RESAMPLES = 10_000
# Below this many distinct per-backend slopes the bootstrap has nothing to
# resample and its interval is an artefact rather than a measurement.
MIN_DISTINCT_SLOPES = 3
DEFAULT_SEED = 20260903
CI_LOW_PCT = 2.5
CI_HIGH_PCT = 97.5


@dataclass(frozen=True, slots=True)
class BackendFit:
    """One backend's own growth, over its own life."""

    backend_id: str
    samples: int
    span_s: float
    first_rss_bytes: int
    last_rss_bytes: int
    slope_bytes_per_s: float

    @property
    def slope_bytes_per_hour(self) -> float:
        return self.slope_bytes_per_s * 3600.0

    @property
    def usable(self) -> bool:
        return self.samples >= MIN_SAMPLES and self.span_s >= MIN_SPAN_S


def least_squares_slope(points: list[tuple[float, float]]) -> float:
    """Ordinary least squares slope of y on x. Zero when x does not vary."""
    if len(points) < 2:
        return 0.0
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator


def fits_from_records(records: Iterable[AnyRecord]) -> list[BackendFit]:
    """One fit per backend, from its own `backend` records."""
    series: dict[str, list[tuple[float, float]]] = {}
    for record in records:
        if not isinstance(record, BackendRecord) or record.vm_rss_bytes is None:
            continue
        # Keyed by engine *and* pid: a pid is only unique within a container,
        # and a soak that restarts one would otherwise splice two backends'
        # lives into a single implausible line.
        key = f"{record.engine}/{record.backend_id}"
        series.setdefault(key, []).append((record.t_ms / 1000.0, float(record.vm_rss_bytes)))

    fits: list[BackendFit] = []
    for key, points in series.items():
        points.sort()
        span = points[-1][0] - points[0][0]
        fits.append(
            BackendFit(
                backend_id=key,
                samples=len(points),
                span_s=span,
                first_rss_bytes=int(points[0][1]),
                last_rss_bytes=int(points[-1][1]),
                slope_bytes_per_s=least_squares_slope(points),
            )
        )
    fits.sort(key=lambda f: f.backend_id)
    return fits


@dataclass(frozen=True, slots=True)
class GrowthResult:
    """The pooled slope and its bootstrap interval."""

    fits: tuple[BackendFit, ...]
    median_slope_bytes_per_s: float
    ci_low_bytes_per_s: float
    ci_high_bytes_per_s: float
    resamples: int
    seed: int

    @property
    def usable_fits(self) -> tuple[BackendFit, ...]:
        return tuple(f for f in self.fits if f.usable)

    @property
    def distinct_slopes(self) -> int:
        """How many different slopes the resampling unit actually offered."""
        return len({round(f.slope_bytes_per_s, 6) for f in self.usable_fits})

    @property
    def degenerate(self) -> bool:
        """Whether the interval is an artefact of identical inputs.

        With too few distinct slopes the bootstrap cannot express uncertainty:
        every resample returns the same median and the interval collapses to a
        point. It will then "exclude zero" whatever the truth is.
        """
        return self.distinct_slopes < MIN_DISTINCT_SLOPES

    @property
    def excludes_zero(self) -> bool:
        """Whether the interval is entirely on one side of zero.

        Either side counts. Memory that shrinks with connection age is as much
        a finding as memory that grows, and a test that only looked for growth
        would quietly pass a leak running backwards.

        Meaningless when `degenerate`: an interval of zero width excludes
        everything.
        """
        return self.ci_low_bytes_per_s > 0.0 or self.ci_high_bytes_per_s < 0.0

    @property
    def significant(self) -> bool:
        """The claim the acceptance criterion is actually after."""
        return self.excludes_zero and not self.degenerate

    @property
    def median_slope_mib_per_hour(self) -> float:
        return self.median_slope_bytes_per_s * 3600.0 / 1024**2

    def table(self) -> str:
        lines = [
            f"backends fitted: {len(self.usable_fits)} of {len(self.fits)} "
            f"(>= {MIN_SAMPLES} samples over >= {MIN_SPAN_S:.0f}s)",
            f"median slope:    {self.median_slope_bytes_per_s:+,.1f} B/s "
            f"({self.median_slope_mib_per_hour:+.3f} MiB/hour)",
            f"bootstrap {CI_HIGH_PCT - CI_LOW_PCT:.0f}% CI: "
            f"[{self.ci_low_bytes_per_s:+,.1f}, {self.ci_high_bytes_per_s:+,.1f}] B/s "
            f"over {self.resamples:,} resamples of backends, seed {self.seed}",
            f"distinct slopes: {self.distinct_slopes} of {len(self.usable_fits)} backends",
            f"excludes zero:   {'yes' if self.excludes_zero else 'NO'}",
        ]
        if self.degenerate:
            lines.append(
                "DEGENERATE: too few distinct slopes to resample. The interval has "
                "no width and would exclude zero whatever the truth is -- the "
                "connections were doing identical work, so there is no variation "
                "for a bootstrap over backends to express."
            )
        return "\n".join(lines)


def bootstrap_slope(
    fits: Iterable[BackendFit],
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> GrowthResult:
    """Percentile bootstrap of the median slope, resampling *backends*."""
    everything = tuple(fits)
    usable = [f.slope_bytes_per_s for f in everything if f.usable]
    if not usable:
        return GrowthResult(
            fits=everything,
            median_slope_bytes_per_s=0.0,
            ci_low_bytes_per_s=0.0,
            ci_high_bytes_per_s=0.0,
            resamples=0,
            seed=seed,
        )
    rng = random.Random(seed)
    medians: list[float] = []
    size = len(usable)
    for _ in range(resamples):
        sample = [usable[rng.randrange(size)] for _ in range(size)]
        medians.append(statistics.median(sample))
    medians.sort()

    def percentile(pct: float) -> float:
        index = min(len(medians) - 1, max(0, round(pct / 100.0 * (len(medians) - 1))))
        return medians[index]

    return GrowthResult(
        fits=everything,
        median_slope_bytes_per_s=statistics.median(usable),
        ci_low_bytes_per_s=percentile(CI_LOW_PCT),
        ci_high_bytes_per_s=percentile(CI_HIGH_PCT),
        resamples=resamples,
        seed=seed,
    )


def growth_from_records(
    records: Iterable[AnyRecord],
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> GrowthResult:
    """The whole derivation, from a metrics stream alone."""
    return bootstrap_slope(fits_from_records(records), resamples=resamples, seed=seed)
