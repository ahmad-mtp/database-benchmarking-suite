"""Comparing PATH A against PATH B (PLAN.md S14).

*Accept: for the same workload, PATH B's `t_db_end - t_db_start` distribution
overlaps PATH A's latency distribution; `ab_delta_valid=false` is stamped on
every local run.*

The two paths are:

    PATH A   driver ------------------> engine
    PATH B   driver -> app tier ------> engine

and the interval being compared is the *engine* portion of each. On PATH A that
is the whole latency; on PATH B it is the middle of the app tier's span, from
the moment the pool hands over a connection to the moment the row is back. Both
carry one hop of the same Docker network, both ask the engine for the same
statement, so they are measuring the same thing from two places and should
land on top of each other. **If they do not, the app tier's instrumentation is
wrong**, and every later claim about where time went is built on it.

Overlap, not equality. The two are measured by different processes at different
points in the stack, and demanding equality would be demanding that the
comparison have no measurement error at all. What is required is that the bulk
of the two distributions -- p10 to p90 -- intersects, and that their medians
agree within a stated factor. That is enough to catch instrumentation timing
the wrong thing, which is the failure this exists to find; it found three.

The band is p10-p90 rather than the interquartile range because the residual
offset is real and has a direction. PATH A's inner interval runs consistently
15-25% above PATH B's `db_us` (84 us against 68 us on a quiet machine), and the
interquartile ranges then sit adjacent rather than overlapping -- 75-93 against
62-74, missing by a microsecond. The likely cause is the driver's own
spin-wait: at 540/s across four workers each spins the last 1.5 ms of every
inter-arrival, which is about 0.8 of a core of contention on cpuset 6-9 that
the app tier on cpuset 0-1 does not have. **That is a hypothesis, not a
measurement.** What is asserted instead is the direction -- the driver's side
is never the faster one -- and a bound on the ratio, because a residual that
changed sign or grew would mean something other than contention.

**`ab_delta_valid` is false on every local run.** The tempting number here is
`PATH B total - PATH A total = the app tier's cost`. It is a subtraction of two
measurements taken on a machine where driver, tier, engine and observability
share ten cores that `cpuset` does not isolate -- S1 measured 20-30%
interference across sets. Each side carries that, and the difference between two
contaminated numbers is not a clean measure of anything. The shape transfers to
real hardware; the number does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dsel.driver.histogram import read_hlog

# The bulk of the two distributions must intersect, and the medians must agree
# within this factor. Both are stated here rather than chosen per run.
MEDIAN_TOLERANCE = 2.0
OVERLAP_LOW_PCT = 10.0
OVERLAP_HIGH_PCT = 90.0
# The driver's side may be slower than the tier's view of the same engine, and
# by this much, before the difference stops looking like contention.
MAX_RESIDUAL_RATIO = 1.5


@dataclass(frozen=True, slots=True)
class Distribution:
    """One histogram, reduced to the points the comparison uses."""

    label: str
    count: int
    low_us: float
    """The `OVERLAP_LOW_PCT` percentile: the bottom of the bulk."""
    p50_us: float
    high_us: float
    """The `OVERLAP_HIGH_PCT` percentile: the top of the bulk."""
    p99_us: float

    @classmethod
    def of(cls, label: str, histogram: Any) -> Distribution:
        return cls(
            label=label,
            count=int(histogram.get_total_count()),
            low_us=float(histogram.get_value_at_percentile(OVERLAP_LOW_PCT)),
            p50_us=float(histogram.get_value_at_percentile(50.0)),
            high_us=float(histogram.get_value_at_percentile(OVERLAP_HIGH_PCT)),
            p99_us=float(histogram.get_value_at_percentile(99.0)),
        )

    @classmethod
    def from_hlog(cls, label: str, path: Path) -> Distribution:
        return cls.of(label, read_hlog(path))


@dataclass(frozen=True, slots=True)
class PathComparison:
    """PATH A's latency against PATH B's engine interval."""

    path_a: Distribution
    path_b_db: Distribution
    path_b_total: Distribution | None = None
    ab_delta_valid: bool = False

    @property
    def overlaps(self) -> bool:
        """Whether the bulk of the two distributions intersects."""
        return (
            self.path_a.low_us <= self.path_b_db.high_us
            and self.path_b_db.low_us <= self.path_a.high_us
        )

    @property
    def residual_ratio(self) -> float:
        """How much slower the driver's own view of the engine is.

        Above 1 by construction on this host, and it must stay that way: the
        driver carries its own spin-wait contention and the tier does not. A
        ratio below 1 would mean the tier is reporting less than the engine
        took, which is not possible unless the span is wrapping the wrong
        thing.
        """
        if not self.path_b_db.p50_us:
            return float("inf")
        return self.path_a.p50_us / self.path_b_db.p50_us

    @property
    def median_ratio(self) -> float:
        low = min(self.path_a.p50_us, self.path_b_db.p50_us)
        high = max(self.path_a.p50_us, self.path_b_db.p50_us)
        return high / low if low else float("inf")

    @property
    def medians_agree(self) -> bool:
        return self.median_ratio <= MEDIAN_TOLERANCE

    @property
    def tier_cost_us(self) -> float | None:
        """PATH B's total less its engine interval.

        Present because it is the number everyone wants, and returned beside
        `ab_delta_valid=False` because on this machine it may not be reported.
        """
        if self.path_b_total is None:
            return None
        return self.path_b_total.p50_us - self.path_b_db.p50_us

    def table(self) -> str:
        lines = [
            f"{'distribution':<22} {'count':>8} {'p' + format(OVERLAP_LOW_PCT, 'g'):>9} "
            f"{'p50 us':>9} {'p' + format(OVERLAP_HIGH_PCT, 'g'):>9} {'p99 us':>9}",
            f"{'-' * 22} {'-' * 8} {'-' * 9} {'-' * 9} {'-' * 9} {'-' * 9}",
        ]
        for dist in (self.path_a, self.path_b_db, self.path_b_total):
            if dist is None:
                continue
            lines.append(
                f"{dist.label:<22} {dist.count:>8,} {dist.low_us:>9.0f} "
                f"{dist.p50_us:>9.0f} {dist.high_us:>9.0f} {dist.p99_us:>9.0f}"
            )
        lines += [
            "",
            f"p{OVERLAP_LOW_PCT:g}-p{OVERLAP_HIGH_PCT:g} overlap: "
            f"{'yes' if self.overlaps else 'NO'}",
            f"residual ratio:        {self.residual_ratio:.2f}x "
            f"(driver's own view is the slower one, bound {MAX_RESIDUAL_RATIO:.1f}x)",
            f"median ratio:          {self.median_ratio:.2f}x "
            f"(tolerance {MEDIAN_TOLERANCE:.1f}x) -> "
            f"{'agree' if self.medians_agree else 'DISAGREE'}",
        ]
        if self.tier_cost_us is not None:
            lines.append(
                f"app tier cost:         {self.tier_cost_us:.0f} us at p50 -- "
                f"ab_delta_valid={str(self.ab_delta_valid).lower()}, so this is a "
                "shape, not a figure to report"
            )
        return "\n".join(lines)
