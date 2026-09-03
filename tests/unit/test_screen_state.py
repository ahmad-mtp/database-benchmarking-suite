"""The screen-state reducer (PLAN.md S8a).

S8a's criterion is an equality between two sessions, which only means anything
if the state is a function of the record stream alone. These tests pin that
down: no wall clock, no hidden accumulation, and gates that stay fired.
"""

from __future__ import annotations

import json

from dsel.live.schema import (
    AnyRecord,
    ContainerRecord,
    LatencyWindowRecord,
    PhaseRecord,
    ValidityRecord,
)
from dsel.live.state import ScreenState, apply, reduce_all, snapshot


def _phase(t_ms: int, seq: int, phase: str, event: str) -> PhaseRecord:
    return PhaseRecord(t_ms=t_ms, w="g", seq=seq, cell="c", phase=phase, event=event)  # type: ignore[arg-type]


def _stream() -> list[AnyRecord]:
    return [
        _phase(1000, 0, "warmup", "begin"),
        LatencyWindowRecord(
            t_ms=1100,
            w="d",
            seq=0,
            cell="c",
            window_ms=100,
            op="read",
            count=10,
            rate_per_s=100.0,
            p50_us=300.0,
            p99_us=900.0,
            max_us=1500.0,
        ),
        _phase(2000, 1, "warmup", "end"),
        _phase(2001, 2, "measure", "begin"),
        LatencyWindowRecord(
            t_ms=2100,
            w="d",
            seq=1,
            cell="c",
            window_ms=100,
            op="read",
            count=12,
            errors=1,
            rate_per_s=120.0,
            p50_us=310.0,
            p99_us=950.0,
        ),
        ContainerRecord(
            t_ms=2200,
            w="s",
            seq=0,
            cell="c",
            container="dsel-engine",
            memory_current=1024,
            memory_max=4096,
            pids_current=30,
        ),
        ValidityRecord(t_ms=2300, w="g", seq=3, cell="c", gate="driver_cpu", verdict="FLAG"),
        _phase(3000, 4, "measure", "end"),
    ]


def test_reduction_is_pure() -> None:
    """Folding the same records twice lands in exactly the same place."""
    assert reduce_all(_stream()) == reduce_all(_stream())


def test_boundary_is_recorded_from_the_stream() -> None:
    state = reduce_all(_stream())
    assert state.warmup_ended_t_ms == 2000
    assert state.measure_began_t_ms == 2001


def test_counts_accumulate_but_gauges_do_not() -> None:
    state = reduce_all(_stream())
    op = state.ops["read"]
    assert op.count == 22, "counts add across windows"
    assert op.errors == 1
    assert op.rate_per_s == 120.0, "rate is the latest window, not a sum"
    assert op.max_us == 1500.0, "a window without a max keeps the last one seen"


def test_a_fired_gate_is_never_erased() -> None:
    """A later OK must not clear a FLAG, or the screen would hide a bad run."""
    state = reduce_all(_stream())
    assert state.worst_verdict == "FLAG"
    state = apply(
        state, ValidityRecord(t_ms=4000, w="g", seq=5, gate="driver_cpu", verdict="OK")
    )
    assert state.worst_verdict == "FLAG"
    assert state.validity["driver_cpu"].verdict == "FLAG"


def test_snapshot_does_not_leak_insertion_order() -> None:
    """The S8a comparison diffs these dumps, so they must depend on the state
    alone. Two sessions that met the same ops in a different order hold the
    same state; a dump that echoed dict insertion order would report a
    difference that was never on either screen."""

    def window(t_ms: int, seq: int, op: str) -> LatencyWindowRecord:
        return LatencyWindowRecord(
            t_ms=t_ms,
            w="d",
            seq=seq,
            cell="c",
            window_ms=100,
            op=op,
            count=5,
            rate_per_s=50.0,
            p50_us=100.0,
        )

    forward = reduce_all([window(1000, 0, "read"), window(1000, 1, "write")])
    backward = reduce_all([window(1000, 0, "write"), window(1000, 1, "read")])
    assert list(forward.ops) != list(backward.ops), "the dicts differ in order"
    dump = json.dumps(snapshot(forward), sort_keys=True)
    assert dump == json.dumps(snapshot(backward), sort_keys=True)


def test_snapshot_carries_the_boundary() -> None:
    dump = json.loads(json.dumps(snapshot(reduce_all(_stream())), sort_keys=True))
    assert dump["warmup_ended_t_ms"] == 2000
    assert dump["measure_began_t_ms"] == 2001


def test_empty_state_renders_as_nothing_seen() -> None:
    state = ScreenState()
    assert state.elapsed_ms == 0
    assert state.worst_verdict == "OK"
    assert not state.in_measurement_window
