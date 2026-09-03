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

**Repeats are pooled, by median.** A run matrix has repeats precisely so that
one unlucky pass does not become a landmark, and leaving them as separate
points defeats that twice over: the running peak becomes the best single
observation of the best rung, and the drop measured against it is inflated by
that rung's own noise. Measured on a connection ramp with three repeats, the
peak rung's three passes spanned 3858-3933/s; using the top of that range as
the reference moved the apparent drop by two points.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from dataclasses import dataclass

from dsel.live.cell import Cell
from dsel.live.schema import (
    AnyRecord,
    EngineRecord,
    LatencyWindowRecord,
    ValidityRecord,
)

DELIVERY_TOLERANCE = 0.03
ERROR_TOLERANCE = 0.001
KNEE_FACTOR = 2.0
COLLAPSE_DROP = 0.05
COLLAPSE_ERRORS = 0.01

DRIVER_BOUND = "INCONCLUSIVE_DRIVER_BOUND"


@dataclass(frozen=True, slots=True)
class Point:
    """One step of a load curve, however it was obtained.

    `x` is the axis being ramped -- the offered rate for a rate ramp, the
    connection count for a connection ramp. `offered_rate_per_s` is carried
    separately and always means the offered rate, because `delivered` has to
    compare achieved against offered whichever axis is moving. On a connection
    ramp the offered rate is held constant and the connections move, which is
    the entire point of that ramp: throughput must not depend on how many
    connections were used to deliver the same load, and where it starts to, the
    connections have become the bottleneck.
    """

    x: float
    achieved_rate_per_s: float
    completed: int
    errors: int
    p99_us: float
    verdict: str = "OK"
    offered_rate_per_s: float = 0.0

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
    """The largest `x` that was actually delivered."""
    delivered = [p.x for p in points if p.delivered]
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
            return point.x
    return None


def collapse(points: Iterable[Point]) -> float | None:
    """Where throughput turned over **and stayed over**.

    The sustained part is not a nicety. The first version returned the first
    point below the running peak by `COLLAPSE_DROP`, and on a real connection
    ramp -- flat within noise from 8 to 256 connections -- a single 6.7% dip
    at one rung was enough to report a collapse that the next three rungs
    plainly contradicted. A collapse that recovers is a measurement blip, and
    calling it a collapse would have a reader cap a pool at a number that
    means nothing.

    An error-rate breach is different and does fire immediately: errors are not
    noise, and a rung that started failing has already answered the question.
    """
    ordered = list(points)
    best = 0.0
    for index, point in enumerate(ordered):
        if point.error_rate > COLLAPSE_ERRORS:
            return point.x
        if best > 0 and point.achieved_rate_per_s < best * (1.0 - COLLAPSE_DROP):
            threshold = best * (1.0 - COLLAPSE_DROP)
            if all(later.achieved_rate_per_s < threshold for later in ordered[index:]):
                return point.x
        best = max(best, point.achieved_rate_per_s)
    return None


RATE_AXIS = "offered_rate_per_s"
CONNECTION_AXIS = "connections"


@dataclass(frozen=True, slots=True)
class Curve:
    """A load curve and its three landmarks, on whichever axis was ramped."""

    points: tuple[Point, ...]
    axis: str = RATE_AXIS

    @property
    def max_sustainable_rate_per_s(self) -> float | None:
        return max_sustainable(self.points)

    @property
    def knee_rate_per_s(self) -> float | None:
        return knee(self.points)

    @property
    def collapse_rate_per_s(self) -> float | None:
        return collapse(self.points)

    def landmarks(self) -> dict[str, float | None | str]:
        return {
            "axis": self.axis,
            "max_sustainable_rate_per_s": self.max_sustainable_rate_per_s,
            "knee_rate_per_s": self.knee_rate_per_s,
            "collapse_rate_per_s": self.collapse_rate_per_s,
        }


@dataclass(frozen=True, slots=True)
class CellSummary:
    """One cell reduced to the numbers a curve needs, keyed by its id."""

    cell_id: str
    cell: Cell
    achieved_rate_per_s: float
    completed: int
    errors: int
    p99_us: float
    verdict: str
    backends: float | None


def summarise_cells(records: Iterable[AnyRecord]) -> list[CellSummary]:
    """Group a metrics stream into cells. The one pass both curves share.

    Grouping is by cell id, never by any one field of it: a connection ramp
    holds the rate fixed and varies `step`, so two cells of the same ramp can
    agree on rate, engine, scenario and repeat and still be different cells.
    """
    windows: dict[str, list[LatencyWindowRecord]] = {}
    verdicts: dict[str, list[str]] = {}
    backends: dict[str, list[float]] = {}
    order: list[str] = []
    for record in records:
        if record.cell is None:
            continue
        if isinstance(record, LatencyWindowRecord):
            if record.cell not in windows:
                windows[record.cell] = []
                order.append(record.cell)
            windows[record.cell].append(record)
        elif isinstance(record, ValidityRecord):
            verdicts.setdefault(record.cell, []).append(record.verdict)
        elif isinstance(record, EngineRecord):
            value = record.metrics.get("backends")
            if isinstance(value, int | float) and not isinstance(value, bool):
                backends.setdefault(record.cell, []).append(float(value))

    summaries: list[CellSummary] = []
    for cell_id in order:
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
        counts = backends.get(cell_id)
        summaries.append(
            CellSummary(
                cell_id=cell_id,
                cell=Cell.parse(cell_id),
                achieved_rate_per_s=achieved,
                completed=completed,
                errors=errors,
                p99_us=weighted / completed if completed else 0.0,
                verdict=(DRIVER_BOUND if DRIVER_BOUND in verdicts.get(cell_id, []) else "OK"),
                # Peak, not mean: connections are opened at the start of a cell
                # and a mean would be dragged down by the ramp-up before they
                # were all up.
                backends=max(counts) if counts else None,
            )
        )
    return summaries


def pool_repeats(points: Iterable[Point]) -> tuple[Point, ...]:
    """One point per axis value, pooling repeats by median.

    Counts and errors are summed -- they are totals -- while rates and
    percentiles are medianed, because they are per-unit-time quantities and
    adding them would report three passes as three times the throughput.
    A verdict is sticky: one driver-bound repeat makes the point driver-bound,
    since the offered load was not delivered in at least one of them.
    """
    grouped: dict[float, list[Point]] = {}
    for point in points:
        grouped.setdefault(point.x, []).append(point)
    pooled: list[Point] = []
    for x in sorted(grouped):
        group = grouped[x]
        pooled.append(
            Point(
                x=x,
                offered_rate_per_s=statistics.median(p.offered_rate_per_s for p in group),
                achieved_rate_per_s=statistics.median(p.achieved_rate_per_s for p in group),
                completed=sum(p.completed for p in group),
                errors=sum(p.errors for p in group),
                p99_us=statistics.median(p.p99_us for p in group),
                verdict=(
                    DRIVER_BOUND if any(p.verdict == DRIVER_BOUND for p in group) else "OK"
                ),
            )
        )
    return tuple(pooled)


def curve_from_records(records: Iterable[AnyRecord]) -> Curve:
    """Rebuild a *rate* curve from a metrics stream alone.

    The offered rate comes from the cell id, which is why the id is parsed
    rather than split: a rate read out of a malformed id would silently
    reorder the curve.
    """
    points = [
        Point(
            x=float(summary.cell.rate),
            offered_rate_per_s=float(summary.cell.rate),
            achieved_rate_per_s=summary.achieved_rate_per_s,
            completed=summary.completed,
            errors=summary.errors,
            p99_us=summary.p99_us,
            verdict=summary.verdict,
        )
        for summary in summarise_cells(records)
    ]
    return Curve(points=pool_repeats(points), axis=RATE_AXIS)


def connection_curve_from_records(records: Iterable[AnyRecord]) -> Curve:
    """Rebuild a *connection* ramp from a metrics stream alone.

    The offered rate is held constant and the connection count is staircased,
    so the axis is the connection count -- and that count is a *measured*
    property, read from the engine's own `backends` reading, not a coordinate
    asserted in the cell id. A ramp that trusted the id would report the
    connections it asked for rather than the ones the engine actually had, and
    the cliff is precisely where those two stop being the same number.
    """
    summaries = summarise_cells(records)
    with_counts = [s for s in summaries if s.backends is not None]
    if not with_counts:
        raise ValueError(
            "no engine records carrying a backend count; a connection ramp "
            "cannot be re-derived without the count the engine actually saw"
        )
    points = [
        Point(
            # Rounded to the rung the run asked for. The engine's own count
            # wobbles by a backend or two -- an autovacuum worker, the sampler
            # itself -- and leaving those as separate axis values would split
            # a rung's repeats across neighbouring points and defeat pooling.
            x=float(_nearest_rung(summary.backends or 0.0)),
            offered_rate_per_s=float(summary.cell.rate),
            achieved_rate_per_s=summary.achieved_rate_per_s,
            completed=summary.completed,
            errors=summary.errors,
            p99_us=summary.p99_us,
            verdict=summary.verdict,
        )
        for summary in with_counts
    ]
    return Curve(points=pool_repeats(points), axis=CONNECTION_AXIS)


def _nearest_rung(backends: float) -> int:
    """Snap a measured backend count to the power of two it belongs to.

    A rung of 64 connections shows up as 65 or 66 backends depending on what
    else the engine happened to be running. Those are the same rung.
    """
    if backends <= 0:
        return 0
    rung = 1
    while rung * 2 <= backends:
        rung *= 2
    return rung if backends - rung <= rung else rung * 2
