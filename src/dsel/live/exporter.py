"""Prometheus exporter over the metrics stream (PLAN.md S8b).

D3: Prometheus gets its data by *tailing the metrics stream*, through a custom
collector. One write path, one source of truth; the exporter is a consumer of
`metrics.ndjson` exactly like the TUI, never a second sampling path. No
pushgateway, no remote-write, and nothing here touches Docker.

Two constraints shape everything below.

**Latency here is for watching, never for reporting.** The `.hlog` is
authoritative. The series exported for latency are within-window estimates and
are named and annotated so they cannot be mistaken for the reported figure --
`..._estimate_microseconds`, with `dsbench_latency_estimate_only` published
alongside so a dashboard can assert it.

**Cardinality is a budget, not a hope.** PLAN.md fixes it at <=500 active
series and <=5000 distinct series per run. The series that would blow it is the
per-backend view: a connection ramp to 500 connections would publish 500 series
per metric, and with repeats and steps as labels it would be worse again. So

* backends are **aggregated** before publishing -- counts by state and by wait
  event, never one series per backend id;
* `step` and `repeat` never become labels. They join from `dsbench_cell_info`,
  which is one series per cell;
* engine internals are published from an allowlist, not from whatever keys the
  engine happened to return.

Admission is enforced rather than assumed: `SeriesBudget` refuses a new series
past the cap and counts the refusal in `dsbench_series_refused_total`. A run
whose exporter refused anything has *not* met the budget -- the accepted count
staying under 500 is only meaningful next to a refusal count of zero, so both
are published.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from prometheus_client import REGISTRY, CollectorRegistry, start_http_server
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from dsel.live.cell import Cell, CellError
from dsel.live.state import ScreenState, apply
from dsel.live.tail import LiveTailer

PREFIX = "dsbench"

# PLAN.md's series budget.
MAX_ACTIVE_SERIES = 500
MAX_SERIES_PER_RUN = 5000

VERDICT_CODES = {"OK": 1.0, "FLAG": 2.0, "INVALID": 3.0, "INCONCLUSIVE_DRIVER_BOUND": 4.0}

PHASES: tuple[str, ...] = (
    "gate",
    "provision",
    "init",
    "load",
    "warmup",
    "measure",
    "collect",
    "teardown",
)

# Engine internals worth a series each. An allowlist rather than "whatever the
# engine returned": `pg_stat_*` alone would put hundreds of keys on the wire
# and the budget would be spent on fields nobody plotted.
ENGINE_METRIC_ALLOWLIST: tuple[str, ...] = (
    "backends",
    "backends_active",
    "backends_idle_in_transaction",
    "commits_total",
    "rollbacks_total",
    "blocks_read_total",
    "blocks_hit_total",
    "cache_hit_ratio",
    "tup_returned_total",
    "tup_fetched_total",
    "deadlocks_total",
    "temp_bytes_total",
    "checkpoints_timed_total",
    "checkpoints_requested_total",
    "wal_bytes_total",
    "locks_waiting",
    "xact_age_max_s",
    "replication_lag_bytes",
    "connections_rejected_total",
    "autovacuum_workers",
)


class SeriesBudget:
    """Admission control over label sets, with the refusals counted.

    The cap is not a target to sail close to. It exists because Prometheus
    degrades quietly under cardinality -- the scrape gets slower, the query
    gets slower, and nothing announces it. Refusing at the door and publishing
    the refusal count turns a silent degradation into a number on a dashboard.
    """

    def __init__(
        self, max_active: int = MAX_ACTIVE_SERIES, max_per_run: int = MAX_SERIES_PER_RUN
    ) -> None:
        self.max_active = max_active
        self.max_per_run = max_per_run
        self._active: set[tuple[str, tuple[str, ...]]] = set()
        self._ever: set[tuple[str, tuple[str, ...]]] = set()
        self.refused = 0

    def begin_scrape(self) -> None:
        """Active series are counted per scrape; the run-wide set is not reset."""
        self._active = set()

    def admit(self, name: str, label_values: Sequence[str]) -> bool:
        key = (name, tuple(label_values))
        if key in self._active:
            return True
        if len(self._active) >= self.max_active or len(self._ever) >= self.max_per_run:
            self.refused += 1
            return False
        self._active.add(key)
        self._ever.add(key)
        return True

    @property
    def active(self) -> int:
        return len(self._active)

    @property
    def distinct(self) -> int:
        return len(self._ever)


@dataclass
class _Family:
    """One metric family being assembled under the budget."""

    name: str
    documentation: str
    labels: tuple[str, ...] = ()
    samples: list[tuple[tuple[str, ...], float]] = field(default_factory=list)

    def to_metric(self) -> GaugeMetricFamily:
        family = GaugeMetricFamily(
            f"{PREFIX}_{self.name}", self.documentation, labels=list(self.labels)
        )
        for values, value in self.samples:
            family.add_metric(list(values), value)
        return family


class StateExporter:
    """Turns a `ScreenState` into metric families, under a series budget.

    Kept separate from the collector so the mapping is testable without an HTTP
    server, a registry, or a run.
    """

    def __init__(self, budget: SeriesBudget | None = None) -> None:
        self.budget = budget or SeriesBudget()

    def families(self, state: ScreenState) -> list[Metric]:
        self.budget.begin_scrape()
        out: list[_Family] = []

        def emit(family: _Family, label_values: Sequence[str], value: float | None) -> None:
            if value is None:
                return
            if not self.budget.admit(family.name, label_values):
                return
            family.samples.append((tuple(label_values), float(value)))

        # --- identity and the join series ---------------------------------
        if state.cell is not None:
            try:
                cell = Cell.parse(state.cell)
            except CellError:
                cell = None
            if cell is not None:
                labels = cell.info_labels()
                info = _Family(
                    "cell_info",
                    "Run-matrix cell. step and repeat join from here, never as "
                    "labels on every series.",
                    tuple(labels),
                )
                emit(info, tuple(labels.values()), 1.0)
                out.append(info)

        progress = _Family("records_total", "Records folded into this view.")
        emit(progress, (), state.records_seen)
        out.append(progress)

        elapsed = _Family("elapsed_seconds", "Span of the record stream seen so far.")
        emit(elapsed, (), state.elapsed_ms / 1000.0)
        out.append(elapsed)

        # --- phase --------------------------------------------------------
        phase_active = _Family("phase_active", "1 while a phase is running.", ("phase",))
        phase_done = _Family("phase_completed", "1 once a phase has ended.", ("phase",))
        for phase in PHASES:
            emit(phase_active, (phase,), 1.0 if state.phase == phase else 0.0)
            emit(phase_done, (phase,), 1.0 if phase in state.completed_phases else 0.0)
        out += [phase_active, phase_done]

        boundary = _Family(
            "measure_boundary_timestamp_seconds",
            "Warmup end and measure start, for the dashboard boundary marker.",
            ("boundary",),
        )
        if state.warmup_ended_t_ms is not None:
            emit(boundary, ("warmup_end",), state.warmup_ended_t_ms / 1000.0)
        if state.measure_began_t_ms is not None:
            emit(boundary, ("measure_begin",), state.measure_began_t_ms / 1000.0)
        out.append(boundary)

        # --- operations ---------------------------------------------------
        ops_total = _Family("ops_total", "Operations completed.", ("op",))
        op_errors = _Family("op_errors_total", "Operations that errored.", ("op",))
        op_rate = _Family("op_rate_per_second", "Latest window's rate.", ("op",))
        op_latency = _Family(
            "op_latency_estimate_microseconds",
            "WITHIN-WINDOW ESTIMATE, not the reported figure. The .hlog is "
            "authoritative; a scrape bucket must never become a reported percentile.",
            ("op", "quantile"),
        )
        for op in sorted(state.ops):
            record = state.ops[op]
            emit(ops_total, (op,), record.count)
            emit(op_errors, (op,), record.errors)
            emit(op_rate, (op,), record.rate_per_s)
            for quantile, value in (
                ("p50", record.p50_us),
                ("p99", record.p99_us),
                ("max", record.max_us),
            ):
                emit(op_latency, (op, quantile), value)
        out += [ops_total, op_errors, op_rate, op_latency]

        estimate_only = _Family(
            "latency_estimate_only",
            "Always 1. Every latency series here is a within-window estimate.",
        )
        emit(estimate_only, (), 1.0)
        out.append(estimate_only)

        # --- containers ---------------------------------------------------
        cpu_usage = _Family(
            "container_cpu_usage_microseconds_total",
            "cgroup cpu.stat usage_usec.",
            ("container",),
        )
        cpu_throttled = _Family(
            "container_cpu_throttled_microseconds_total",
            "cgroup cpu.stat throttled_usec.",
            ("container",),
        )
        cpu_periods = _Family(
            "container_cpu_throttled_periods_total",
            "cgroup cpu.stat nr_throttled.",
            ("container",),
        )
        mem_current = _Family(
            "container_memory_bytes", "cgroup memory.current.", ("container",)
        )
        mem_max = _Family("container_memory_limit_bytes", "cgroup memory.max.", ("container",))
        oom = _Family(
            "container_oom_events_total", "cgroup memory.events oom_kill.", ("container",)
        )
        pids = _Family("container_pids", "cgroup pids.current.", ("container",))
        for name in sorted(state.containers):
            c = state.containers[name]
            emit(cpu_usage, (name,), c.cpu_usage_usec)
            emit(cpu_throttled, (name,), c.cpu_throttled_usec)
            emit(cpu_periods, (name,), None)
            emit(mem_current, (name,), c.memory_current)
            emit(mem_max, (name,), c.memory_max)
            emit(oom, (name,), None)
            emit(pids, (name,), c.pids_current)
        out += [cpu_usage, cpu_throttled, cpu_periods, mem_current, mem_max, oom, pids]

        # --- pools --------------------------------------------------------
        pool_size = _Family("pool_size", "Configured pool size.", ("pool",))
        pool_in_use = _Family("pool_in_use", "Connections checked out.", ("pool",))
        pool_idle = _Family("pool_idle", "Connections idle in the pool.", ("pool",))
        pool_waiting = _Family("pool_waiting", "Callers waiting to acquire.", ("pool",))
        pool_timeouts = _Family("pool_timeouts_total", "Acquire timeouts.", ("pool",))
        pool_wait = _Family(
            "pool_acquire_wait_estimate_microseconds",
            "WITHIN-WINDOW ESTIMATE of acquire wait p99, not a reported figure.",
            ("pool",),
        )
        for name in sorted(state.pools):
            p = state.pools[name]
            emit(pool_size, (name,), p.size)
            emit(pool_in_use, (name,), p.in_use)
            emit(pool_idle, (name,), p.idle)
            emit(pool_waiting, (name,), p.waiting)
            emit(pool_timeouts, (name,), p.timeouts)
            emit(pool_wait, (name,), p.acquire_wait_us_p99)
        out += [pool_size, pool_in_use, pool_idle, pool_waiting, pool_timeouts, pool_wait]

        # --- app tier -----------------------------------------------------
        app_count = _Family("app_requests_total", "App-tier requests.", ("endpoint",))
        app_errors = _Family("app_errors_total", "App-tier errors.", ("endpoint",))
        app_cpu = _Family("app_cpu_percent", "App-tier CPU. Gated at 70%.", ("endpoint",))
        app_span = _Family(
            "app_span_estimate_microseconds",
            "WITHIN-WINDOW ESTIMATE of app-tier span timings, not a reported figure.",
            ("endpoint", "span"),
        )
        for name in sorted(state.app_endpoints):
            a = state.app_endpoints[name]
            emit(app_count, (name,), a.count)
            emit(app_errors, (name,), a.errors)
            emit(app_cpu, (name,), a.cpu_pct)
            for span, value in (
                ("recv_to_db_start", a.app_recv_to_db_start_us),
                ("db", a.db_us),
                ("db_end_to_send", a.db_end_to_send_us),
            ):
                emit(app_span, (name, span), value)
        out += [app_count, app_errors, app_cpu, app_span]

        # --- engine internals, from the allowlist -------------------------
        engine_metric = _Family(
            "engine_metric",
            "Engine internals, from a fixed allowlist so the budget is not spent "
            "on keys nobody plotted.",
            ("engine", "metric"),
        )
        for engine_name in sorted(state.engines):
            metrics = state.engines[engine_name].metrics
            for metric_name in ENGINE_METRIC_ALLOWLIST:
                raw = metrics.get(metric_name)
                if isinstance(raw, int | float) and not isinstance(raw, bool):
                    emit(engine_metric, (engine_name, metric_name), float(raw))
        out.append(engine_metric)

        # --- backends, aggregated ------------------------------------------
        by_state: dict[tuple[str, str], int] = {}
        by_wait: dict[tuple[str, str], int] = {}
        rss_sum: dict[str, float] = {}
        rss_count: dict[str, int] = {}
        for (engine_name, _), backend in state.backends.items():
            by_state[(engine_name, backend.state or "unknown")] = (
                by_state.get((engine_name, backend.state or "unknown"), 0) + 1
            )
            wait_key = (engine_name, backend.wait_event_type or "none")
            by_wait[wait_key] = by_wait.get(wait_key, 0) + 1
            if backend.vm_rss_bytes is not None:
                rss_sum[engine_name] = rss_sum.get(engine_name, 0.0) + backend.vm_rss_bytes
                rss_count[engine_name] = rss_count.get(engine_name, 0) + 1

        backends_by_state = _Family(
            "backends",
            "Backends counted by state. Aggregated deliberately: one series per "
            "backend id would put a 500-connection ramp straight through the budget.",
            ("engine", "state"),
        )
        backends_by_wait = _Family(
            "backends_waiting",
            "Backends counted by wait event type.",
            ("engine", "wait_event_type"),
        )
        backend_rss_sum = _Family("backend_rss_bytes_sum", "Summed backend RSS.", ("engine",))
        backend_rss_count = _Family(
            "backend_rss_bytes_count", "Backends contributing to the RSS sum.", ("engine",)
        )
        for (engine_name, backend_state), count in sorted(by_state.items()):
            emit(backends_by_state, (engine_name, backend_state), count)
        for (engine_name, wait), count in sorted(by_wait.items()):
            emit(backends_by_wait, (engine_name, wait), count)
        for engine_name in sorted(rss_sum):
            emit(backend_rss_sum, (engine_name,), rss_sum[engine_name])
            emit(backend_rss_count, (engine_name,), rss_count[engine_name])
        out += [backends_by_state, backends_by_wait, backend_rss_sum, backend_rss_count]

        # --- sockets --------------------------------------------------------
        sockets = _Family(
            "net_sockets", "Socket counts by scope and state.", ("scope", "state")
        )
        overflows = _Family("net_listen_overflows_total", "Accept-queue overflows.", ("scope",))
        drops = _Family("net_listen_drops_total", "Listen drops.", ("scope",))
        ephemeral = _Family("net_ephemeral_ports", "Ephemeral port use.", ("scope", "kind"))
        for scope in sorted(state.net):
            n = state.net[scope]
            for socket_state, value in (
                ("established", n.established),
                ("time_wait", n.time_wait),
                ("syn_recv", n.syn_recv),
            ):
                emit(sockets, (scope, socket_state), value)
            emit(overflows, (scope,), n.listen_overflows)
            emit(drops, (scope,), n.listen_drops)
            emit(ephemeral, (scope, "used"), n.ephemeral_used)
            emit(ephemeral, (scope, "available"), n.ephemeral_available)
        out += [sockets, overflows, drops, ephemeral]

        # --- validity -------------------------------------------------------
        verdict = _Family(
            "validity_verdict",
            "1=OK 2=FLAG 3=INVALID 4=INCONCLUSIVE_DRIVER_BOUND.",
            ("gate",),
        )
        observed = _Family("validity_observed", "What the gate measured.", ("gate",))
        limit = _Family("validity_limit", "The gate's limit.", ("gate",))
        for gate in sorted(state.validity):
            fired = state.validity[gate]
            emit(verdict, (gate,), VERDICT_CODES[fired.verdict])
            if isinstance(fired.observed, int | float):
                emit(observed, (gate,), float(fired.observed))
            if isinstance(fired.limit, int | float):
                emit(limit, (gate,), float(fired.limit))
        worst = _Family("validity_worst", "The most serious verdict seen this run.")
        emit(worst, (), VERDICT_CODES[state.worst_verdict])
        out += [verdict, observed, limit, worst]

        # --- the budget's own meter ------------------------------------------
        # Emitted last and outside admission control: a budget that could refuse
        # to report its own breach would be useless.
        meters = [
            _Family("series_active", "Series published in this scrape."),
            _Family("series_distinct_total", "Distinct series published this run."),
            _Family(
                "series_refused_total",
                "Series refused by the budget. Non-zero means the run exceeded it: "
                "an active count under the cap only means something beside a zero here.",
            ),
            _Family("series_budget_active_limit", "The active-series cap."),
            _Family("series_budget_run_limit", "The per-run distinct-series cap."),
        ]
        for meter, value in zip(
            meters,
            (
                self.budget.active,
                self.budget.distinct,
                self.budget.refused,
                self.budget.max_active,
                self.budget.max_per_run,
            ),
            strict=True,
        ):
            meter.samples.append(((), float(value)))
        out += meters

        return [family.to_metric() for family in out if family.samples]


class RunCollector(Collector):
    """A `prometheus_client` collector that folds the run's records on scrape.

    Registered against a run directory. Each scrape drains whatever the tailer
    has released since the last one and folds it into the same `ScreenState`
    the TUI uses, so the dashboard and the terminal cannot disagree.
    """

    def __init__(self, run_dir: Path, budget: SeriesBudget | None = None) -> None:
        self.run_dir = run_dir
        self.state = ScreenState()
        self.exporter = StateExporter(budget)
        self._tailer = LiveTailer(run_dir / "shards")
        self._lock = threading.Lock()

    def ingest(self, *, final: bool = False) -> int:
        """Fold newly released records. Returns how many were applied."""
        with self._lock:
            records = self._tailer.poll()
            if final:
                records = records + self._tailer.close()
            for record in records:
                self.state = apply(self.state, record)
            return len(records)

    def collect(self) -> Iterator[Metric]:
        self.ingest()
        with self._lock:
            state = self.state
        yield from self.exporter.families(state)


def serve(
    run_dir: Path,
    port: int,
    registry: CollectorRegistry | None = None,
) -> tuple[RunCollector, threading.Thread]:
    """Expose a run on `port`. Returns the collector and the server thread."""
    registry = registry if registry is not None else REGISTRY
    collector = RunCollector(run_dir)
    registry.register(collector)
    _, thread = start_http_server(port, registry=registry)
    return collector, thread


def wait_for_scrape(port: int, timeout_s: float = 10.0) -> None:
    """Block until the exporter answers, so callers do not race the server."""
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=1) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    raise TimeoutError(f"exporter on :{port} did not answer within {timeout_s}s")
