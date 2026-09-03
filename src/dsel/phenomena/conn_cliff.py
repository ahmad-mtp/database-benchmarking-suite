"""Deriving a load curve's landmarks (PLAN.md S15, S16).

**This is where knee and collapse are defined, and nowhere else.** The driver
runs steps; it does not decide what they mean. That separation is the whole
point of S15's acceptance criterion -- *an independent script re-derives knee
and collapse from `metrics.ndjson` alone* -- and it is only achievable if the
rules can be applied to records read from a file, with no run in memory and no
container anywhere.

Nothing here may import Docker, `subprocess`, or an engine client. A derivation
that needs the system it is describing cannot be re-run against an audit
bundle, and a number nobody can re-derive is not evidence.

The three landmarks:

* **Max sustainable rate** -- the highest offered rate actually delivered:
  achieved within `DELIVERY_TOLERANCE` of offered, errors under
  `ERROR_TOLERANCE`, verdict `OK`. A step the driver could not deliver says
  nothing about the engine, so it cannot be the answer.
* **Knee** -- the lowest offered rate whose p99 exceeds `KNEE_FACTOR` times the
  baseline's. Latency, not throughput: at the knee throughput is still fine,
  which is exactly why the knee is the interesting number.
* **Collapse** -- the lowest offered rate whose achieved throughput has fallen
  more than `COLLAPSE_DROP` below the best so far, or whose error rate exceeds
  `COLLAPSE_ERRORS`. Offering more and getting less. A plateau is saturation,
  not collapse.

Latency here is the *within-window estimate* from `latency_window` records,
which is what a metrics file carries. That is sound for landmarks because they
are ordinal -- a knee is a place, not a figure -- and the authoritative
percentiles stay in the `.hlog`. A landmark derived from these estimates must
never be quoted as a latency.

Per cell the statistic is the count-weighted *mean* of the window estimates.
The maximum was tried first and is unusable: it is one sample of a tail, it
moves with whatever else the machine is doing, and on a busy host it inflated
the baseline cell enough to move the knee a whole step.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from dsel.live.cell import Cell, CellError
from dsel.live.schema import AnyRecord, LatencyWindowRecord, ValidityRecord

DELIVERY_TOLERANCE = 0.03
ERROR_TOLERANCE = 0.001
KNEE_FACTOR = 2.0
COLLAPSE_DROP = 0.05
COLLAPSE_ERRORS = 0.01

DRIVER_BOUND = "INCONCLUSIVE_DRIVER_BOUND"


@dataclass(frozen=True, slots=True)
class Point:
    """One step of a load curve, however it was obtained."""

    offered_rate_per_s: float
    achieved_rate_per_s: float
    completed: int
    errors: int
    p99_us: float
    verdict: str = "OK"

    @property
    def error_rate(self) -> float:
        return self.errors / self.completed if self.completed else 0.0

    @property
    def delivered(self) -> bool:
        if self.offered_rate_per_s <= 0:
            return False
        shortfall = 1.0 - self.achieved_rate_per_s / self.offered_rate_per_s
        return (
            shortfall <= DELIVERY_TOLERANCE
            and self.error_rate <= ERROR_TOLERANCE
            and self.verdict == "OK"
        )


def max_sustainable(points: Iterable[Point]) -> float | None:
    delivered = [p.offered_rate_per_s for p in points if p.delivered]
    return max(delivered) if delivered else None


def knee(points: Iterable[Point], factor: float = KNEE_FACTOR) -> float | None:
    ordered = list(points)
    if not ordered:
        return None
    baseline = ordered[0].p99_us
    if baseline <= 0:
        return None
    for point in ordered[1:]:
        if point.p99_us > baseline * factor:
            return point.offered_rate_per_s
    return None


def collapse(points: Iterable[Point]) -> float | None:
    best = 0.0
    for point in points:
        if point.error_rate > COLLAPSE_ERRORS:
            return point.offered_rate_per_s
        if best > 0 and point.achieved_rate_per_s < best * (1.0 - COLLAPSE_DROP):
            return point.offered_rate_per_s
        best = max(best, point.achieved_rate_per_s)
    return None


@dataclass(frozen=True, slots=True)
class Curve:
    """A load curve and its three landmarks."""

    points: tuple[Point, ...]

    @property
    def max_sustainable_rate_per_s(self) -> float | None:
        return max_sustainable(self.points)

    @property
    def knee_rate_per_s(self) -> float | None:
        return knee(self.points)

    @property
    def collapse_rate_per_s(self) -> float | None:
        return collapse(self.points)

    def landmarks(self) -> dict[str, float | None]:
        return {
            "max_sustainable_rate_per_s": self.max_sustainable_rate_per_s,
            "knee_rate_per_s": self.knee_rate_per_s,
            "collapse_rate_per_s": self.collapse_rate_per_s,
        }


def curve_from_records(records: Iterable[AnyRecord]) -> Curve:
    """Rebuild a load curve from a metrics stream alone.

    The offered rate comes from the cell id, which is why the id is parsed
    rather than split: a rate read out of a malformed id would silently
    reorder the curve. The achieved rate is the completed count over the span
    the windows actually cover -- not over a nominal duration nobody recorded.
    """
    windows: dict[str, list[LatencyWindowRecord]] = {}
    verdicts: dict[str, list[str]] = {}
    order: list[str] = []
    for record in records:
        if isinstance(record, LatencyWindowRecord) and record.cell:
            if record.cell not in windows:
                windows[record.cell] = []
                order.append(record.cell)
            windows[record.cell].append(record)
        elif isinstance(record, ValidityRecord) and record.cell:
            verdicts.setdefault(record.cell, []).append(record.verdict)

    points: list[Point] = []
    for cell_id in order:
        try:
            cell = Cell.parse(cell_id)
        except CellError:
            # A record whose cell id is not a run-matrix coordinate cannot be
            # placed on a curve. Skipping it silently would drop a step and
            # move the knee, so it is refused.
            raise

        group = windows[cell_id]
        completed = sum(w.count for w in group)
        errors = sum(w.errors for w in group)
        # Achieved rate is the sum of the per-writer rates, which is the same
        # definition the driver uses in memory. Pooling all the counts over a
        # single wall-clock span would be a different quantity, and the two
        # would then disagree for a reason that had nothing to do with the run.
        # Each writer's windows are consecutive and carry the width they
        # actually covered, so its span is their total.
        by_writer: dict[str, tuple[int, float]] = {}
        for window in group:
            count, span = by_writer.get(window.w, (0, 0.0))
            by_writer[window.w] = (count + window.count, span + window.window_ms / 1000.0)
        achieved = sum(count / span for count, span in by_writer.values() if span > 0)
        # Count-weighted mean of the window estimates, not their maximum. The
        # maximum is a single sample of a tail and moves with whatever else the
        # machine was doing: on a busy host it inflated the *baseline* cell's
        # figure enough to raise the doubling threshold and push the knee a
        # whole step, from 400/s to 500/s, on the same run the driver called
        # 400/s in memory. A knee that moves with the neighbours is not a knee.
        weighted = sum((w.p99_us or 0.0) * w.count for w in group)
        p99 = weighted / completed if completed else 0.0
        verdict = DRIVER_BOUND if DRIVER_BOUND in verdicts.get(cell_id, []) else "OK"
        points.append(
            Point(
                offered_rate_per_s=float(cell.rate),
                achieved_rate_per_s=achieved,
                completed=completed,
                errors=errors,
                p99_us=p99,
                verdict=verdict,
            )
        )
    points.sort(key=lambda p: p.offered_rate_per_s)
    return Curve(points=tuple(points))
