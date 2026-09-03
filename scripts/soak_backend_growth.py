#!/usr/bin/env python3
"""A long soak against Postgres, for the per-backend RSS slope (S16-S18b).

    python scripts/soak_backend_growth.py --minutes 65 --out runs/soak

Provisions a digest-pinned Postgres, opens a fixed set of long-lived
connections through the app tier's pool, drives a steady low rate against them,
and samples every backend's `VmRSS` for the whole run. Writes shards; derives
nothing. `phenomena/backend_growth.py` does the fitting afterwards, from the
file.

*Accept: over a >=1 h soak the per-backend RSS slope has a bootstrap CI
excluding zero.* An hour is not arbitrary. A backend's RSS moves in page-sized
steps as caches fill, so over ten minutes the slope is mostly the first few
allocations and the interval swamps it; the growth only separates from that
start-up transient over a much longer window.

The connections are held open deliberately. A pool that recycles connections
measures nothing about connection *age*, which is the whole variable.

**They are also deliberately unlike each other.** A first soak drove 24
identical connections and got 21 identical slopes: the bootstrap had nothing to
resample, the interval came out with zero width, and it "excluded zero" for a
reason that had nothing to do with memory. Each connection now gets its own
statement mix, its own row range and its own cadence -- which is also what
production connections look like.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for candidate in (REPO_ROOT / "src", REPO_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from dsel.audit.environment import resolve_image  # noqa: E402
from dsel.driver.calibrate import (  # noqa: E402
    PG_STAT_STATEMENTS_FLAGS,
    enable_statement_stats,
    pgbench_init,
    prewarm,
)
from dsel.live.ndjson import ShardWriter  # noqa: E402
from dsel.live.sampler.backend_pg import BackendSampler  # noqa: E402
from dsel.live.sampler.containers import ContainerSampler  # noqa: E402
from dsel.runtime.docker import provision, wait_healthy  # noqa: E402
from dsel.runtime.envelope import GIB, ResourceEnvelope  # noqa: E402
from dsel.runtime.paths import RunLayout, new_run_id  # noqa: E402
from dsel.runtime.teardown import Teardown  # noqa: E402

ENGINE_IMAGE = "postgres:18"
DATA_DIR = "/var/lib/postgresql/data"
READY = ["pg_isready", "-U", "postgres", "-q"]
SCALE = 5


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# Four workloads of very different memory behaviour. A point lookup touches
# two pages; a range count walks thousands; a sort allocates work_mem; a join
# builds a hash table. Backends running these do not grow alike, which is the
# variation a bootstrap over backends needs in order to say anything.
WORKLOADS: tuple[tuple[str, str], ...] = (
    ("point", "SELECT abalance FROM pgbench_accounts WHERE aid = $1"),
    ("range", "SELECT count(*) FROM pgbench_accounts WHERE aid BETWEEN $1 AND $1 + 20000"),
    (
        "sort",
        "SELECT aid FROM pgbench_accounts WHERE aid BETWEEN $1 AND $1 + 40000 "
        "ORDER BY abalance DESC LIMIT 50",
    ),
    (
        "join",
        "SELECT b.bid, count(*) FROM pgbench_accounts a "
        "JOIN pgbench_branches b ON b.bid = a.bid "
        "WHERE a.aid BETWEEN $1 AND $1 + 20000 GROUP BY b.bid",
    ),
)


def hold_connections(dsn: str, count: int, minutes: float, cell: str) -> None:
    """Hold `count` connections open, working steadily, for the duration.

    Each connection gets one of `WORKLOADS`, its own slice of the key space and
    its own cadence. Enough work to keep the backend allocating -- an idle
    backend allocates nothing and its slope is trivially zero -- and light
    enough that the engine is nowhere near its limits, so what is measured is
    age rather than load.
    """
    import asyncio

    import asyncpg

    rows = SCALE * 100_000

    async def worker(index: int, deadline: float) -> None:
        name, statement = WORKLOADS[index % len(WORKLOADS)]
        # Cadence spread from 0.6 s to about 1.6 s, so the backends do not
        # even allocate in step with one another.
        interval = 0.6 + (index % 7) * 0.15
        connection = await asyncpg.connect(dsn)
        try:
            tick = 0
            while time.monotonic() < deadline:
                aid = 1 + ((index * 7919) + tick * 104_729) % max(1, rows - 40_001)
                await connection.fetchval(statement, aid)
                tick += 1
                await asyncio.sleep(interval)
        finally:
            await connection.close()

    async def run() -> None:
        deadline = time.monotonic() + minutes * 60.0
        await asyncio.gather(*(worker(index, deadline) for index in range(count)))

    asyncio.run(run())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--minutes", type=float, default=65.0)
    parser.add_argument("--connections", type=int, default=24)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "runs")
    parser.add_argument("--interval", type=float, default=5.0, help="sampler cadence")
    args = parser.parse_args(argv)

    layout = RunLayout.for_run(new_run_id(), base=args.out)
    layout.create()
    run_id = layout.run_id
    teardown = Teardown(run_id)
    cell = f"uc1/postgres/soak/r{args.connections}/rep1"
    print(f"run {run_id} -> {layout.root}", flush=True)

    try:
        pin = resolve_image(ENGINE_IMAGE)
        container = provision(
            pin,
            ResourceEnvelope(cpuset=(2, 3, 4, 5), cpus=4.0, memory_bytes=3 * GIB),
            run_id,
            data_dir=DATA_DIR,
            container_port=5432,
            host_port=(port := free_port()),
            env={"POSTGRES_PASSWORD": "dsel", "PGDATA": f"{DATA_DIR}/pgdata"},
            command=PG_STAT_STATEMENTS_FLAGS,
        )
        wait_healthy(container, READY)
        pgbench_init(container.name, scale=SCALE)
        enable_statement_stats(container.name)
        prewarm(container.name, "pgbench_accounts", "pgbench_accounts_pkey")

        backend_writer = ShardWriter(layout.shards, "backend-0")
        container_writer = ShardWriter(layout.shards, "container-0")
        sampler = BackendSampler(
            backend_writer, container.name, cell=cell, interval_s=args.interval
        )
        containers = ContainerSampler(
            container_writer, [container.name], interval_s=args.interval, cell=cell
        )
        sampler.start()
        containers.start()
        print(
            f"holding {args.connections} connections for {args.minutes:g} minutes, "
            f"sampling every {args.interval:g}s",
            flush=True,
        )
        try:
            hold_connections(
                f"postgresql://postgres:dsel@127.0.0.1:{port}/postgres",
                args.connections,
                args.minutes,
                cell,
            )
        finally:
            sampler.stop()
            containers.stop()
            backend_writer.close()
            container_writer.close()

        from dsel.live.merge import merge_to_file

        count = merge_to_file(layout.shards, layout.metrics)
        print(
            f"done: {sampler.ticks} sampler ticks, {sampler.failures} failures, "
            f"{count} records -> {layout.metrics}",
            flush=True,
        )
    finally:
        teardown.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
