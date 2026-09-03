"""Per-backend sampler for Postgres (PLAN.md S16-S18).

One record per backend per tick: what it is doing, what it is waiting on, how
old it is, and how much memory it is holding. This is the per-connection view
the cliff work needs, and it is the *only* place the connection phenomena get
their raw material.

**Ground truth for memory is `VmRSS`.** PLAN.md is explicit and the reason is
version portability: `/proc/<pid>/status` exists on every Postgres that has
ever run in a container, while `pg_get_process_memory_contexts()` is
UNVERIFIED on PG18 and must not be depended on.
`pg_log_backend_memory_contexts()` (PG14+) is the fallback for a *breakdown*,
which is a different question from *how much*.

**Backpressure is a first-class behaviour, not an optimisation.** Sampling
`pg_stat_activity` at 1 Hz becomes the load it is measuring once there are
enough backends to walk: the view takes a snapshot of every backend on every
call. Above `BACKPRESSURE_BACKENDS` the cadence drops to
`BACKPRESSURE_INTERVAL_S`, and the change is *recorded* rather than silent --
a sampler that quietly slows down leaves a gap that looks like a quiet period.

This module writes records and derives nothing. The slope, the confidence
interval and the attribution all live under `phenomena/`, computed from the
file afterwards (S15's rule).
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass

from dsel.live.ndjson import ShardWriter, now_ms
from dsel.live.schema import BackendRecord, EngineRecord, ValidityRecord

DEFAULT_INTERVAL_S = 1.0
# Above this many backends the view itself becomes the load. PLAN.md's risk 7.
BACKPRESSURE_BACKENDS = 256
BACKPRESSURE_INTERVAL_S = 10.0

GATE_SAMPLER_BACKPRESSURE = "backend_sampler_backpressure"

# One statement, one snapshot. Two queries would sample two different moments
# and attribute one backend's state to another's age.
ACTIVITY_SQL = """
SELECT pid, coalesce(state, ''), coalesce(wait_event_type, ''),
       coalesce(wait_event, ''),
       extract(epoch FROM (now() - backend_start)),
       coalesce(extract(epoch FROM (now() - query_start)), -1)
FROM pg_stat_activity
WHERE backend_type = 'client backend'
"""

# VmRSS for every backend, read in the same exec so the memory reading and the
# activity snapshot are not seconds apart at high connection counts.
RSS_SCRIPT = r"""
for d in /proc/[0-9]*; do
  pid=${d#/proc/}
  rss=$(awk '/^VmRSS:/{print $2}' "$d/status" 2>/dev/null)
  [ -n "$rss" ] && echo "$pid $rss"
done
"""


class BackendSamplerError(RuntimeError):
    """The sampler could not read the engine."""


@dataclass(frozen=True, slots=True)
class Backend:
    """One backend as the engine described it, plus its RSS."""

    pid: int
    state: str
    wait_event_type: str
    wait_event: str
    age_s: float
    query_age_s: float
    vm_rss_bytes: int | None


def _exec(container: str, args: list[str], timeout: float = 30.0) -> str:
    result = subprocess.run(
        ["docker", "exec", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise BackendSamplerError(f"exec failed: {result.stderr.strip()}")
    return result.stdout


def read_rss(container: str) -> dict[int, int]:
    """`pid -> VmRSS bytes` for every process in the engine container.

    Read from `/proc` rather than asked of Postgres. It works on every version,
    it needs no extension, and it cannot be affected by the engine being busy
    -- which matters, because the moment worth measuring is exactly when it is.
    """
    output = subprocess.run(
        ["docker", "exec", "-i", container, "sh", "-s"],
        input=RSS_SCRIPT,
        capture_output=True,
        text=True,
        timeout=60.0,
        check=False,
    )
    if output.returncode != 0:
        raise BackendSamplerError(f"rss read failed: {output.stderr.strip()}")
    rss: dict[int, int] = {}
    for line in output.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            rss[int(parts[0])] = int(parts[1]) * 1024
    return rss


def read_backends(
    container: str, user: str = "postgres", password: str = "dsel"
) -> list[Backend]:
    """Every client backend, with its RSS attached."""
    raw = _exec(
        container,
        [
            "--env",
            f"PGPASSWORD={password}",
            container,
            "psql",
            "-U",
            user,
            "-tAX",
            "-F",
            "|",
            "-c",
            ACTIVITY_SQL,
        ],
    )
    rss = read_rss(container)
    backends: list[Backend] = []
    for line in raw.splitlines():
        fields = line.split("|")
        if len(fields) != 6 or not fields[0].isdigit():
            continue
        pid = int(fields[0])
        backends.append(
            Backend(
                pid=pid,
                state=fields[1],
                wait_event_type=fields[2],
                wait_event=fields[3],
                age_s=float(fields[4]),
                query_age_s=float(fields[5]),
                vm_rss_bytes=rss.get(pid),
            )
        )
    return backends


def to_records(
    backends: list[Backend], engine: str, cell: str | None
) -> list[BackendRecord | EngineRecord]:
    """Map a snapshot onto records. No interpretation, no aggregation."""
    t_ms = now_ms()
    out: list[BackendRecord | EngineRecord] = [
        BackendRecord(
            t_ms=t_ms,
            w="",
            seq=0,
            cell=cell,
            engine=engine,
            backend_id=str(backend.pid),
            state=backend.state or None,
            wait_event_type=backend.wait_event_type or None,
            wait_event=backend.wait_event or None,
            vm_rss_bytes=backend.vm_rss_bytes,
            age_s=backend.age_s,
            query_start_age_s=None if backend.query_age_s < 0 else backend.query_age_s,
        )
        for backend in backends
    ]
    out.append(
        EngineRecord(
            t_ms=t_ms,
            w="",
            seq=0,
            cell=cell,
            engine=engine,
            sample_class="light",
            metrics={
                "backends": len(backends),
                "backends_active": sum(1 for b in backends if b.state == "active"),
                "backends_idle": sum(1 for b in backends if b.state == "idle"),
                "backends_idle_in_transaction": sum(
                    1 for b in backends if b.state == "idle in transaction"
                ),
            },
        )
    )
    return out


class BackendSampler:
    """Polls `pg_stat_activity` and `/proc`, writing to its own shard."""

    def __init__(
        self,
        writer: ShardWriter,
        container: str,
        engine: str = "postgres",
        cell: str | None = None,
        interval_s: float = DEFAULT_INTERVAL_S,
        user: str = "postgres",
        password: str = "dsel",
    ) -> None:
        self.writer = writer
        self.container = container
        self.engine = engine
        self.cell = cell
        self.interval_s = interval_s
        self.user = user
        self.password = password
        self.ticks = 0
        self.failures = 0
        self.backpressure = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> int:
        """One snapshot. Returns the number of backends seen."""
        backends = read_backends(self.container, self.user, self.password)
        for record in to_records(backends, self.engine, self.cell):
            self.writer.write(record)
        self.ticks += 1
        self._apply_backpressure(len(backends))
        return len(backends)

    def _apply_backpressure(self, count: int) -> None:
        """Slow down above the threshold, and say so in the stream.

        A sampler that quietly changes cadence leaves a gap in the record that
        looks exactly like a period when nothing happened.
        """
        wanted = count > BACKPRESSURE_BACKENDS
        if wanted == self.backpressure:
            return
        self.backpressure = wanted
        self.interval_s = BACKPRESSURE_INTERVAL_S if wanted else DEFAULT_INTERVAL_S
        self.writer.write(
            ValidityRecord(
                t_ms=now_ms(),
                w="",
                seq=0,
                cell=self.cell,
                gate=GATE_SAMPLER_BACKPRESSURE,
                verdict="FLAG" if wanted else "OK",
                observed=count,
                limit=BACKPRESSURE_BACKENDS,
                detail=(
                    f"{count} backends: sampling every {self.interval_s:g}s so the "
                    "snapshot does not become the load it is measuring"
                ),
            )
        )

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except (BackendSamplerError, subprocess.SubprocessError):
                # A failed sample is a gap, not a reason to stop: the run is
                # still worth what it produced either side of it.
                self.failures += 1
            remaining = self.interval_s - (time.monotonic() - started)
            if self._stop.wait(max(0.0, remaining)):
                return

    def start(self) -> BackendSampler:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._loop, name="dsel-backend-sampler", daemon=True
            )
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(5.0, self.interval_s + 2.0))
            self._thread = None

    def __enter__(self) -> BackendSampler:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
