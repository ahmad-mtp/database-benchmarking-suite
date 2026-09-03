"""A synthetic but structurally honest run, for exercising the live path.

S8a's criterion needs a *live* session and a replay of the same run. Until S10
lands there is no driver to produce one, so this writes the record stream a
real run would: several writers on different intervals, sharing one run
directory, going through the full phase sequence including the warmup ->
measure boundary the criterion asks to see marked.

The intervals are deliberately mismatched. The gate writer is sparse -- a
handful of records across the whole run -- which is what pins the tailer's
watermark and forces it to hold records back. A tailer that ignored ordering
would pass a test written against three chatty writers and fail on a real run,
so the awkward case is the one built in.

Run as a subprocess so the watcher is reading a file another process is
appending to, not one it wrote itself:

    python -m tests.support.fake_run <run-dir> [duration_s]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from dsel.live.ndjson import ShardWriter, now_ms
from dsel.live.schema import (
    AppRecord,
    BackendRecord,
    ContainerRecord,
    EngineRecord,
    LatencyWindowRecord,
    NetRecord,
    PhaseRecord,
    PoolRecord,
    ValidityRecord,
)

CELL = "uc1/postgres/oltp-mixed/r400/rep1"

# (phase, begin fraction, end fraction) across the run's duration.
PHASES: tuple[tuple[str, float, float], ...] = (
    ("gate", 0.00, 0.01),
    ("provision", 0.02, 0.08),
    ("init", 0.09, 0.12),
    ("load", 0.13, 0.24),
    ("warmup", 0.25, 0.44),
    ("measure", 0.45, 0.84),
    ("collect", 0.85, 0.94),
    ("teardown", 0.95, 1.00),
)

DRIVER_INTERVAL_S = 0.05
SAMPLER_INTERVAL_S = 0.30


def _driver_records(elapsed: float, duration: float) -> list[LatencyWindowRecord]:
    """Two ops per window. Latency degrades as the run loads up."""
    load = min(1.0, elapsed / duration * 1.4)
    return [
        LatencyWindowRecord(
            t_ms=now_ms(),
            w="",
            seq=0,
            cell=CELL,
            window_ms=int(DRIVER_INTERVAL_S * 1000),
            op=op,
            count=count,
            errors=errors,
            rate_per_s=count / DRIVER_INTERVAL_S,
            p50_us=base * (1.0 + load),
            p90_us=base * (2.0 + 3.0 * load),
            p99_us=base * (4.0 + 9.0 * load),
            max_us=base * (9.0 + 40.0 * load),
        )
        for op, count, errors, base in (
            ("order_read", 40, 0, 320.0),
            ("order_write", 12, 1 if elapsed > duration * 0.7 else 0, 1450.0),
            # A join-heavy op so the joins dashboard has something to resolve.
            ("order_join_report", 4, 0, 8200.0),
        )
    ]


def _sampler_records(tick: int) -> list[ContainerRecord | PoolRecord]:
    """Container resource readings plus the client pool, as S7 emits them."""
    t_ms = now_ms()
    records: list[ContainerRecord | PoolRecord] = [
        ContainerRecord(
            t_ms=t_ms,
            w="",
            seq=0,
            cell=CELL,
            container=container,
            cpu_usage_usec=1_000_000 * tick * factor,
            cpu_throttled_usec=12_000 * tick if container.endswith("engine") else 0,
            cpu_nr_throttled=tick if container.endswith("engine") else 0,
            memory_current=int(memory * (1.0 + 0.01 * tick)),
            memory_max=4 * 1024**3,
            pids_current=24 + tick,
        )
        for container, factor, memory in (
            ("dsel-engine", 3, 900 * 1024**2),
            ("dsel-driver", 2, 180 * 1024**2),
        )
    ]
    records.append(
        PoolRecord(
            t_ms=t_ms,
            w="",
            seq=0,
            cell=CELL,
            pool="app->postgres",
            size=32,
            in_use=min(32, 4 + tick),
            idle=max(0, 28 - tick),
            waiting=max(0, tick - 20),
            acquire_wait_us_p99=45.0 * (1 + tick),
        )
    )
    return records


# Backends climb with the tick, which is what the connections dashboard plots.
BACKEND_STATES = ("active", "idle", "idle in transaction")
WAIT_EVENTS: tuple[tuple[str | None, str | None], ...] = (
    (None, None),
    ("Lock", "transactionid"),
    ("IO", "DataFileRead"),
    ("Client", "ClientRead"),
)


def _engine_records(tick: int) -> list[EngineRecord | BackendRecord | NetRecord]:
    """Engine internals, per-backend rows and socket state.

    The per-backend rows are the reason the exporter aggregates: this alone
    reaches 30-odd backends, and the connection ramp at S16 goes far past that.
    """
    t_ms = now_ms()
    backends = min(30, 6 + tick)
    records: list[EngineRecord | BackendRecord | NetRecord] = [
        EngineRecord(
            t_ms=t_ms,
            w="",
            seq=0,
            cell=CELL,
            engine="postgres",
            sample_class="light",
            metrics={
                "backends": backends,
                "backends_active": max(1, backends // 2),
                "backends_idle_in_transaction": backends // 6,
                "commits_total": 1800 * (tick + 1),
                "rollbacks_total": 3 * tick,
                "blocks_read_total": 42_000 * (tick + 1),
                "blocks_hit_total": 910_000 * (tick + 1),
                "cache_hit_ratio": 0.955 - 0.001 * tick,
                "tup_returned_total": 5_400_000 * (tick + 1),
                "tup_fetched_total": 1_100_000 * (tick + 1),
                "deadlocks_total": 0,
                "temp_bytes_total": 1024 * 1024 * tick,
                "checkpoints_timed_total": tick // 8,
                "checkpoints_requested_total": 0,
                "wal_bytes_total": 8 * 1024 * 1024 * (tick + 1),
                "locks_waiting": max(0, tick - 12),
                "xact_age_max_s": 0.4 + 0.05 * tick,
                "connections_rejected_total": 0,
                "autovacuum_workers": 1 if tick % 5 == 0 else 0,
            },
        )
    ]
    for index in range(backends):
        wait_type, wait_event = WAIT_EVENTS[index % len(WAIT_EVENTS)]
        records.append(
            BackendRecord(
                t_ms=t_ms,
                w="",
                seq=0,
                cell=CELL,
                engine="postgres",
                backend_id=f"pid-{4000 + index}",
                state=BACKEND_STATES[index % len(BACKEND_STATES)],
                wait_event_type=wait_type,
                wait_event=wait_event,
                vm_rss_bytes=(9 + index % 5) * 1024 * 1024,
                age_s=float(tick) * SAMPLER_INTERVAL_S,
                query_start_age_s=0.002 * (index + 1),
            )
        )
    for scope, established in (("engine", backends), ("driver", 16), ("app", 32)):
        records.append(
            NetRecord(
                t_ms=t_ms,
                w="",
                seq=0,
                cell=CELL,
                scope=scope,  # type: ignore[arg-type]
                established=established,
                time_wait=4 * tick,
                syn_recv=0,
                listen_overflows=0,
                listen_drops=0,
                ephemeral_used=established + 20 * tick,
                ephemeral_available=28_232,
            )
        )
    return records


APP_ENDPOINTS = ("POST /orders", "GET /orders/{id}", "GET /reports/join", "GET /healthz")


def _app_records(tick: int, elapsed: float, duration: float) -> list[AppRecord]:
    """App-tier spans. PATH B's cost lives in the gap between these three."""
    t_ms = now_ms()
    load = min(1.0, elapsed / duration * 1.4)
    return [
        AppRecord(
            t_ms=t_ms,
            w="",
            seq=0,
            cell=CELL,
            endpoint=endpoint,
            count=count * (tick + 1),
            errors=tick // 10 if endpoint.startswith("POST") else 0,
            app_recv_to_db_start_us=90.0 * (1 + load),
            db_us=db_us * (1 + 2 * load),
            db_end_to_send_us=70.0 * (1 + load),
            cpu_pct=18.0 + 34.0 * load,
        )
        for endpoint, count, db_us in (
            ("POST /orders", 120, 1450.0),
            ("GET /orders/{id}", 400, 320.0),
            ("GET /reports/join", 40, 8200.0),
            ("GET /healthz", 60, 12.0),
        )
    ]


def write_run(run_dir: Path, duration_s: float = 6.0) -> int:
    """Write one run's shards in real time. Returns the record count."""
    shard_dir = run_dir / "shards"
    started = time.monotonic()
    written = 0

    pending_phases = [
        (phase, event, fraction)
        for phase, begin, end in PHASES
        for event, fraction in (("begin", begin), ("end", end))
    ]
    pending_phases.sort(key=lambda item: item[2])

    with (
        ShardWriter(shard_dir, "driver-0") as driver,
        ShardWriter(shard_dir, "sampler-0") as sampler,
        ShardWriter(shard_dir, "engine-0") as engine,
        ShardWriter(shard_dir, "app-0") as app,
        ShardWriter(shard_dir, "gates-0") as gates,
    ):
        next_driver = 0.0
        next_sampler = 0.0
        sampler_tick = 0
        while True:
            elapsed = time.monotonic() - started
            if elapsed > duration_s:
                break
            fraction = elapsed / duration_s

            while pending_phases and pending_phases[0][2] <= fraction:
                phase, event, _ = pending_phases.pop(0)
                gates.write(
                    PhaseRecord(t_ms=now_ms(), w="", seq=0, cell=CELL, phase=phase, event=event)
                )
                written += 1
                if phase == "measure" and event == "begin":
                    gates.write(
                        ValidityRecord(
                            t_ms=now_ms(),
                            w="",
                            seq=0,
                            cell=CELL,
                            gate="steady_state",
                            verdict="OK",
                            observed=0.03,
                            limit=0.05,
                            detail="throughput slope within tolerance",
                        )
                    )
                    written += 1
                if phase == "measure" and event == "end":
                    # A gate that fires late: the reducer must not let a later
                    # OK erase it, and replay must land on the same verdict.
                    gates.write(
                        ValidityRecord(
                            t_ms=now_ms(),
                            w="",
                            seq=0,
                            cell=CELL,
                            gate="driver_cpu",
                            verdict="FLAG",
                            observed=61.4,
                            limit=70.0,
                            detail="driver CPU high but under the invalidation limit",
                        )
                    )
                    written += 1

            if elapsed >= next_driver:
                for record in _driver_records(elapsed, duration_s):
                    driver.write(record)
                    written += 1
                next_driver = elapsed + DRIVER_INTERVAL_S

            if elapsed >= next_sampler:
                for record in _sampler_records(sampler_tick):
                    sampler.write(record)
                    written += 1
                for engine_record in _engine_records(sampler_tick):
                    engine.write(engine_record)
                    written += 1
                for app_record in _app_records(sampler_tick, elapsed, duration_s):
                    app.write(app_record)
                    written += 1
                sampler_tick += 1
                next_sampler = elapsed + SAMPLER_INTERVAL_S

            time.sleep(0.01)

    return written


def main() -> None:
    run_dir = Path(sys.argv[1])
    duration = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
    count = write_run(run_dir, duration)
    print(f"wrote {count} records to {run_dir / 'shards'}")


if __name__ == "__main__":
    main()
