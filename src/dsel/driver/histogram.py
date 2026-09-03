"""Latency recording and `.hlog` output (PLAN.md S10).

**The `.hlog` is the authoritative latency record.** Everything else -- the
TUI's numbers, the Prometheus series, the window summaries in
`metrics.ndjson` -- is a within-window estimate for watching. The bundle
carries raw histograms, never just summaries, because a summary cannot be
re-percentiled and cannot be checked by anyone else.

Two histograms are kept per operation, and both are written:

* **corrected** -- latency from the *scheduled* start. This is the reported
  one. In a genuinely open loop this measurement *is* the coordinated-omission
  correction: a worker that starts a request late has already added that delay
  to the number it records.
* **uncorrected** -- latency from the moment the request was actually issued,
  which is what a closed-loop driver would have reported.

Keeping both is what makes the correction checkable rather than asserted. The
gap between them is the driver's own lag, and if it is large the run is
driver-bound and says so. `hdrh`'s `record_corrected_value` is deliberately
*not* used: applying it on top of a scheduled-start measurement would count the
same delay twice.

The two go to separate files rather than one tagged log, because `hdrh`'s
writer does not emit the tag field and a log whose tags are silently dropped is
worse than two plainly-named files.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from hdrh.histogram import HdrHistogram
from hdrh.log import HistogramLogWriter

# 1 microsecond to 10 minutes, 3 significant figures: 0.1% quantisation error,
# which is far below any difference this harness would act on.
LOWEST_TRACKABLE_US = 1
HIGHEST_TRACKABLE_US = 600_000_000
SIGNIFICANT_FIGURES = 3

CORRECTED = "corrected"
UNCORRECTED = "uncorrected"


def new_histogram() -> HdrHistogram:
    return HdrHistogram(LOWEST_TRACKABLE_US, HIGHEST_TRACKABLE_US, SIGNIFICANT_FIGURES)


@dataclass(slots=True)
class OpRecorder:
    """Both histograms for one operation, plus the counters around them."""

    op: str
    corrected: HdrHistogram = field(default_factory=new_histogram)
    uncorrected: HdrHistogram = field(default_factory=new_histogram)
    lag: HdrHistogram = field(default_factory=new_histogram)
    window: HdrHistogram = field(default_factory=new_histogram)
    count: int = 0
    errors: int = 0
    window_count: int = 0
    window_errors: int = 0

    def record(self, corrected_us: float, uncorrected_us: float, ok: bool = True) -> None:
        """Record one completed operation.

        The lag is the difference, and it is recorded rather than derived so
        the driver-bound gate reads a real distribution instead of a mean.
        """
        corrected = max(1, int(corrected_us))
        uncorrected = max(1, int(uncorrected_us))
        self.corrected.record_value(corrected)
        self.uncorrected.record_value(uncorrected)
        self.lag.record_value(max(1, corrected - uncorrected))
        self.window.record_value(corrected)
        self.count += 1
        self.window_count += 1
        if not ok:
            self.errors += 1
            self.window_errors += 1

    def take_window(self) -> tuple[HdrHistogram, int, int]:
        """Hand back the window histogram and start a fresh one."""
        finished, count, errors = self.window, self.window_count, self.window_errors
        self.window, self.window_count, self.window_errors = new_histogram(), 0, 0
        return finished, count, errors

    @property
    def lag_p99_us(self) -> float:
        if self.lag.get_total_count() == 0:
            return 0.0
        return float(self.lag.get_value_at_percentile(99.0))


def percentiles(
    histogram: HdrHistogram, points: tuple[float, ...] = (50.0, 99.0, 99.9)
) -> dict[str, float]:
    """The percentiles a summary quotes. Never a substitute for the log."""
    if histogram.get_total_count() == 0:
        return {}
    return {f"p{point:g}": float(histogram.get_value_at_percentile(point)) for point in points}


def _write_header(handle: TextIO, writer: HistogramLogWriter, start_time_s: float) -> None:
    """Emit the log preamble.

    `hdrh`'s own `output_start_time` raises (`datetime.iso_format` does not
    exist), so the line is written directly. It matters: the Java reader uses
    `#[StartTime: ...]` to place interval timestamps on an absolute axis, and a
    log without it is read relative to zero.
    """
    writer.output_log_format_version()
    handle.write(f"#[StartTime: {start_time_s:.3f} (seconds since epoch)]\n")
    writer.output_legend()


def write_hlog(
    path: Path,
    histogram: HdrHistogram,
    *,
    start_time_s: float,
    interval_start_s: float = 0.0,
    interval_end_s: float | None = None,
) -> Path:
    """Write one histogram as a v1.2 histogram log.

    One interval per file for a completed phase. Interval logs come later, when
    a phase is long enough for the shape within it to matter.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    end = interval_end_s if interval_end_s is not None else time.time() - start_time_s
    with path.open("w", encoding="utf-8") as handle:
        writer = HistogramLogWriter(handle)
        _write_header(handle, writer, start_time_s)
        writer.output_interval_histogram(
            histogram, start_time_stamp_sec=interval_start_s, end_time_stamp_sec=end
        )
    return path


def hlog_name(op: str, kind: str, worker: int | None = None) -> str:
    """`<op>.<kind>[.w<n>].hlog`, with the operation made filename-safe."""
    safe = "".join(character if character.isalnum() else "-" for character in op).strip("-")
    suffix = "" if worker is None else f".w{worker}"
    return f"{safe}.{kind}{suffix}.hlog"


def read_hlog(path: Path) -> HdrHistogram:
    """Read a log back, accumulating every interval it holds."""
    from hdrh.log import HistogramLogReader

    total = new_histogram()
    reader = HistogramLogReader(str(path), new_histogram())
    while True:
        interval = reader.get_next_interval_histogram()
        if interval is None:
            break
        total.add(interval)
    return total


def value_at_count(histogram: HdrHistogram, count: int) -> float:
    """The value at which the cumulative count first reaches `count`.

    This is the one statement about a histogram that two implementations
    cannot disagree about. `get_value_at_percentile` involves a rounding rule
    and, on the Java side, an iterator whose steps are forced strictly
    increasing -- so comparing implementations by percentile compares their
    conventions as much as their data. Comparing by count compares the data.
    """
    for item in histogram.get_recorded_iterator():
        if item.total_count_to_this_value >= count:
            return float(histogram.get_highest_equivalent_value(item.value_iterated_to))
    return float(histogram.get_max_value())
