"""What the app tier reports about itself (PLAN.md S13).

The tier writes into the same `metrics.ndjson` shard set as everything else --
one shard per worker process, merged afterwards by `(t_ms, w, seq)`. It is a
producer on the one write path, not a second telemetry system.

**The CPU figure comes from the cgroup, not from the process.** D6 established
this one tier down: a single saturated Python process reads about 25% of a
4-core quota, so a gate written against one process silently never fires. The
app tier has the same shape -- several uvicorn workers under one quota -- so
the number that matters is the *cgroup's* CPU time against its own quota, which
is what `app_tier_cpu_pct` is gated on.

Spans are aggregated per window rather than emitted per request. A record per
request at 2000/s would put more load on the metrics path than on the engine,
and the histogram in the driver is the authoritative latency record anyway.
"""

from __future__ import annotations

import os
import statistics
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dsel.app.spans import Span
from dsel.driver.histogram import hlog_name, new_histogram, write_hlog
from dsel.live.ndjson import ShardWriter, now_ms
from dsel.live.schema import AppRecord, PoolRecord, ValidityRecord
from dsel.metrics.validity import app_cpu_gate

CGROUP_CPU_STAT = Path("/sys/fs/cgroup/cpu.stat")
CGROUP_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")

DEFAULT_WINDOW_S = 1.0


def read_cpu_usage_usec() -> int | None:
    """The cgroup's cumulative CPU time. `None` outside a cgroup v2 container."""
    try:
        for line in CGROUP_CPU_STAT.read_text().splitlines():
            key, _, value = line.partition(" ")
            if key == "usage_usec":
                return int(value)
    except OSError:
        return None
    return None


def read_cpu_quota() -> float | None:
    """Cores of quota, from `cpu.max`. `None` when unlimited or unreadable.

    An unlimited container has no denominator, so there is no percentage to
    gate on -- and that must read as "unknown", never as "0%", or the gate
    would pass every time by never having a number.
    """
    try:
        raw = CGROUP_CPU_MAX.read_text().split()
    except OSError:
        return None
    if len(raw) != 2 or raw[0] == "max":
        return None
    return int(raw[0]) / int(raw[1])


@dataclass(slots=True)
class CpuMeter:
    """Cgroup CPU as a percentage of the container's own quota."""

    quota_cores: float | None = field(default_factory=read_cpu_quota)
    _last_usec: int | None = None
    _last_monotonic: float = 0.0

    def sample(self) -> float | None:
        """Percent of quota used since the previous sample."""
        usage = read_cpu_usage_usec()
        now = time.monotonic()
        if usage is None or self.quota_cores is None:
            return None
        if self._last_usec is None:
            self._last_usec, self._last_monotonic = usage, now
            return None
        elapsed = now - self._last_monotonic
        used = (usage - self._last_usec) / 1_000_000.0
        self._last_usec, self._last_monotonic = usage, now
        if elapsed <= 0:
            return None
        return used / (elapsed * self.quota_cores) * 100.0


@dataclass(slots=True)
class EndpointWindow:
    """Spans for one endpoint within the current window."""

    endpoint: str
    count: int = 0
    errors: int = 0
    recv_to_db: list[float] = field(default_factory=list)
    db: list[float] = field(default_factory=list)
    db_to_send: list[float] = field(default_factory=list)

    def add(self, span: Span) -> None:
        self.count += 1
        if not span.ok:
            self.errors += 1
        self.recv_to_db.append(span.recv_to_db_start_us)
        self.db.append(span.db_us)
        self.db_to_send.append(span.db_end_to_send_us)

    def mean(self, values: list[float]) -> float | None:
        return statistics.fmean(values) if values else None


class AppMetrics:
    """One worker's metrics writer, sampling and flushing on a background thread."""

    def __init__(
        self,
        run_dir: Path,
        cell: str,
        window_s: float = DEFAULT_WINDOW_S,
        pool: object = None,
    ) -> None:
        self.cell = cell
        self.window_s = window_s
        self.pool = pool
        self.cpu = CpuMeter()
        self._writer = ShardWriter(run_dir / "shards", f"app-{os.getpid()}")
        self._windows: dict[str, EndpointWindow] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_cpu_pct: float | None = None
        self.fired_gates: set[str] = set()
        self._histograms: dict[tuple[str, str], Any] = {}
        self._started_at = time.time()
        self._hlog_dir = run_dir / "histograms"

    def record(self, span: Span) -> None:
        with self._lock:
            window = self._windows.get(span.endpoint)
            if window is None:
                window = EndpointWindow(endpoint=span.endpoint)
                self._windows[span.endpoint] = window
            window.add(span)
            self._histogram(span.endpoint, "db").record_value(max(1, int(span.db_us)))
            self._histogram(span.endpoint, "total").record_value(max(1, int(span.total_us)))
            self._histogram(span.endpoint, "tier").record_value(
                max(1, int(span.tier_overhead_us))
            )

    def _histogram(self, endpoint: str, kind: str) -> Any:
        key = (endpoint, kind)
        histogram = self._histograms.get(key)
        if histogram is None:
            histogram = new_histogram()
            self._histograms[key] = histogram
        return histogram

    def write_hlogs(self, directory: Path | None = None) -> dict[str, Path]:
        """Write the tier's raw histograms.

        S14 compares PATH B's `t_db_end - t_db_start` distribution against
        PATH A's latency distribution, and a distribution comparison needs a
        distribution. The per-window means in `AppRecord` are for watching;
        this is the record that can be re-percentiled by someone else, exactly
        as the driver's `.hlog` is.

        Rewritten on every flush rather than only at shutdown. The tier is a
        long-lived container that outlives the run being measured, so a file
        written at shutdown does not exist when the results are collected --
        and a container that is killed would leave nothing at all.
        """
        directory = directory if directory is not None else self._hlog_dir
        written: dict[str, Path] = {}
        with self._lock:
            snapshot = dict(self._histograms)
        for (endpoint, kind), histogram in sorted(snapshot.items()):
            if histogram.get_total_count() == 0:
                continue
            path = write_hlog(
                directory / hlog_name(f"{endpoint}-{kind}", "app"),
                histogram,
                start_time_s=self._started_at,
                interval_end_s=max(0.0, time.time() - self._started_at),
            )
            written[f"{endpoint}/{kind}"] = path
        return written

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="dsel-app-metrics", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self.flush()
        self._writer.close()

    def _loop(self) -> None:
        while not self._stop.wait(self.window_s):
            self.flush()

    def _emit_gate(self, cpu_pct: float) -> None:
        """Stamp the app-tier CPU gate into the run's own metrics stream.

        The tier reports its own verdict rather than leaving it to be derived
        afterwards: a cell is invalidated by something that happened during it,
        and the record has to be in the stream the audit bundle hashes.
        """
        gate = app_cpu_gate(cpu_pct)
        self._writer.write(
            ValidityRecord(
                t_ms=now_ms(),
                w="",
                seq=0,
                cell=self.cell,
                gate=gate.gate,
                verdict=gate.verdict,
                observed=gate.observed,
                limit=gate.limit,
                detail=(f"{gate.detail} [{gate.reason}]" if gate.reason else gate.detail),
            )
        )
        if gate.fired:
            self.fired_gates.add(gate.reason or gate.gate)

    def flush(self) -> None:
        """Emit one `app` record per endpoint, plus the pool's state."""
        cpu_pct = self.cpu.sample()
        if cpu_pct is not None:
            self.last_cpu_pct = cpu_pct
            self._emit_gate(cpu_pct)
        with self._lock:
            windows = self._windows
            self._windows = {}
        self.write_hlogs()
        for endpoint in sorted(windows):
            window = windows[endpoint]
            if window.count == 0:
                continue
            self._writer.write(
                AppRecord(
                    t_ms=now_ms(),
                    w="",
                    seq=0,
                    cell=self.cell,
                    endpoint=endpoint,
                    count=window.count,
                    errors=window.errors,
                    app_recv_to_db_start_us=window.mean(window.recv_to_db),
                    db_us=window.mean(window.db),
                    db_end_to_send_us=window.mean(window.db_to_send),
                    cpu_pct=cpu_pct,
                )
            )
        if self.pool is not None:
            from dsel.app.pools import pool_state

            state = pool_state(self.pool)
            self._writer.write(
                PoolRecord(
                    t_ms=now_ms(),
                    w="",
                    seq=0,
                    cell=self.cell,
                    pool="app->postgres",
                    size=state["size"],
                    in_use=state["in_use"],
                    idle=state["idle"],
                    waiting=state["waiting"],
                )
            )
