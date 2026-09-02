"""Screen state as a pure reduction over records (PLAN.md S8a).

S8a's acceptance criterion is that `dsel watch --replay <run-id>` reaches the
same final screen state as the live session did. That is only checkable if the
state is a *function of the record stream* and nothing else -- no wall-clock
reads, no "time since last update", no reaching for Docker. Live and replay
then differ only in where records come from, and the criterion becomes an
equality assertion rather than two people looking at two screens.

The renderer is kept separate and holds no state of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from dsel.live.schema import (
    AnyRecord,
    AppRecord,
    ContainerRecord,
    LatencyWindowRecord,
    PhaseRecord,
    PoolRecord,
    ValidityRecord,
)

# The boundary S8a requires to be visibly marked in both live and replay.
MEASURE_PHASES = ("warmup", "measure")


@dataclass(frozen=True, slots=True)
class OpState:
    """Latest window for one operation. Estimates only, never reported."""

    op: str
    count: int = 0
    errors: int = 0
    rate_per_s: float = 0.0
    p50_us: float | None = None
    p99_us: float | None = None
    max_us: float | None = None


@dataclass(frozen=True, slots=True)
class ContainerState:
    """Latest resource reading for one container."""

    container: str
    cpu_usage_usec: int | None = None
    cpu_throttled_usec: int | None = None
    memory_current: int | None = None
    memory_max: int | None = None
    pids_current: int | None = None

    @property
    def memory_pct(self) -> float | None:
        if not self.memory_current or not self.memory_max:
            return None
        return self.memory_current / self.memory_max * 100.0


@dataclass(frozen=True, slots=True)
class ScreenState:
    """Everything the watcher shows, derived only from the record stream."""

    records_seen: int = 0
    first_t_ms: int | None = None
    last_t_ms: int | None = None
    cell: str | None = None
    phase: str | None = None
    phase_began_t_ms: int | None = None
    completed_phases: tuple[str, ...] = ()
    # The warmup -> measure boundary, marked in both live and replay.
    warmup_ended_t_ms: int | None = None
    measure_began_t_ms: int | None = None
    ops: dict[str, OpState] = field(default_factory=dict)
    containers: dict[str, ContainerState] = field(default_factory=dict)
    pools: dict[str, PoolRecord] = field(default_factory=dict)
    app_endpoints: dict[str, AppRecord] = field(default_factory=dict)
    validity: dict[str, ValidityRecord] = field(default_factory=dict)

    @property
    def elapsed_ms(self) -> int:
        if self.first_t_ms is None or self.last_t_ms is None:
            return 0
        return self.last_t_ms - self.first_t_ms

    @property
    def in_measurement_window(self) -> bool:
        return self.phase in MEASURE_PHASES

    @property
    def worst_verdict(self) -> str:
        """The most serious verdict seen. Drives the banner colour."""
        for verdict in ("INVALID", "INCONCLUSIVE_DRIVER_BOUND", "FLAG"):
            if any(v.verdict == verdict for v in self.validity.values()):
                return verdict
        return "OK"

    @property
    def total_rate(self) -> float:
        return sum(op.rate_per_s for op in self.ops.values())

    @property
    def total_errors(self) -> int:
        return sum(op.errors for op in self.ops.values())


def apply(state: ScreenState, record: AnyRecord) -> ScreenState:
    """Fold one record into the state. Pure: same input, same output."""
    updates: dict[str, object] = {
        "records_seen": state.records_seen + 1,
        "first_t_ms": state.first_t_ms if state.first_t_ms is not None else record.t_ms,
        "last_t_ms": record.t_ms
        if state.last_t_ms is None
        else max(state.last_t_ms, record.t_ms),
    }
    if record.cell is not None:
        updates["cell"] = record.cell

    if isinstance(record, PhaseRecord):
        if record.event == "begin":
            updates["phase"] = record.phase
            updates["phase_began_t_ms"] = record.t_ms
            if record.phase == "measure":
                updates["measure_began_t_ms"] = record.t_ms
        else:
            updates["completed_phases"] = (*state.completed_phases, record.phase)
            if record.phase == "warmup":
                updates["warmup_ended_t_ms"] = record.t_ms
            if state.phase == record.phase:
                updates["phase"] = None

    elif isinstance(record, LatencyWindowRecord):
        ops = dict(state.ops)
        previous = ops.get(record.op, OpState(op=record.op))
        ops[record.op] = OpState(
            op=record.op,
            count=previous.count + record.count,
            errors=previous.errors + record.errors,
            rate_per_s=record.rate_per_s,
            p50_us=record.p50_us,
            p99_us=record.p99_us,
            max_us=record.max_us if record.max_us is not None else previous.max_us,
        )
        updates["ops"] = ops

    elif isinstance(record, ContainerRecord):
        containers = dict(state.containers)
        containers[record.container] = ContainerState(
            container=record.container,
            cpu_usage_usec=record.cpu_usage_usec,
            cpu_throttled_usec=record.cpu_throttled_usec,
            memory_current=record.memory_current,
            memory_max=record.memory_max,
            pids_current=record.pids_current,
        )
        updates["containers"] = containers

    elif isinstance(record, PoolRecord):
        pools = dict(state.pools)
        pools[record.pool] = record
        updates["pools"] = pools

    elif isinstance(record, AppRecord):
        endpoints = dict(state.app_endpoints)
        endpoints[record.endpoint] = record
        updates["app_endpoints"] = endpoints

    elif isinstance(record, ValidityRecord):
        validity = dict(state.validity)
        # A gate that has fired stays fired: a later OK must not erase an
        # INVALID, or the screen would quietly clear a failed run.
        existing = validity.get(record.gate)
        if existing is None or record.verdict != "OK":
            validity[record.gate] = record
        updates["validity"] = validity

    return replace(state, **updates)  # type: ignore[arg-type]


def reduce_all(records: object) -> ScreenState:
    """Fold an entire stream. Live and replay both end here."""
    state = ScreenState()
    for record in records:  # type: ignore[attr-defined]
        state = apply(state, record)
    return state
