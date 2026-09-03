"""Latency recording and the `.hlog` (PLAN.md S10)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from dsel.driver.histogram import (
    CORRECTED,
    OpRecorder,
    hlog_name,
    new_histogram,
    percentiles,
    read_hlog,
    write_hlog,
)


def test_corrected_latency_carries_the_lateness_uncorrected_does_not() -> None:
    """The difference between the two histograms *is* the omission."""
    recorder = OpRecorder(op="read")
    for _ in range(1000):
        recorder.record(corrected_us=5000.0, uncorrected_us=1000.0)
    assert recorder.corrected.get_value_at_percentile(50) == pytest.approx(5000, rel=0.01)
    assert recorder.uncorrected.get_value_at_percentile(50) == pytest.approx(1000, rel=0.01)
    assert recorder.lag_p99_us == pytest.approx(4000, rel=0.01)


def test_windows_reset_but_totals_do_not() -> None:
    """The window feeds the screen; the totals feed the log."""
    recorder = OpRecorder(op="read")
    for _ in range(10):
        recorder.record(1000.0, 1000.0)
    _, count, _ = recorder.take_window()
    assert count == 10
    for _ in range(5):
        recorder.record(1000.0, 1000.0)
    _, second, _ = recorder.take_window()
    assert second == 5
    assert recorder.count == 15


def test_errors_are_counted_and_still_timed() -> None:
    """A failed operation still took time; dropping it flatters the tail."""
    recorder = OpRecorder(op="write")
    recorder.record(9000.0, 9000.0, ok=False)
    assert recorder.errors == 1
    assert recorder.corrected.get_total_count() == 1


def test_hlog_round_trips_through_the_python_reader(tmp_path: Path) -> None:
    histogram = new_histogram()
    for value in range(1, 5001):
        histogram.record_value(value)
    path = write_hlog(tmp_path / "read.corrected.hlog", histogram, start_time_s=time.time())
    recovered = read_hlog(path)
    assert recovered.get_total_count() == histogram.get_total_count()
    for point in (50.0, 99.0, 99.9):
        assert recovered.get_value_at_percentile(point) == histogram.get_value_at_percentile(
            point
        )


def test_the_log_carries_a_start_time(tmp_path: Path) -> None:
    """`hdrh`'s own writer raises on this line, so it is written directly; the
    Java reader needs it to place intervals on an absolute axis."""
    histogram = new_histogram()
    histogram.record_value(42)
    path = write_hlog(tmp_path / "x.hlog", histogram, start_time_s=1788424354.688)
    text = path.read_text(encoding="utf-8")
    assert "#[StartTime: 1788424354.688 (seconds since epoch)]" in text
    assert "#[Histogram log format version 1.2]" in text
    assert '"StartTimestamp","Interval_Length"' in text


def test_percentiles_of_an_empty_histogram_are_empty_not_zero() -> None:
    """Zero would be a number nobody measured."""
    assert percentiles(new_histogram()) == {}


def test_hlog_names_are_filesystem_safe() -> None:
    assert hlog_name("GET /orders/{id}", CORRECTED) == "GET--orders--id.corrected.hlog"
    assert hlog_name("read", CORRECTED, 3) == "read.corrected.w3.hlog"
