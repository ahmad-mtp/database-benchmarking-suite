"""The app tier's connection pool (PLAN.md S13).

**One pool per worker process, never one shared pool.** A shared pool would put
the workers' contention for connections inside the thing being measured, and
the connection-ramp work at S16 needs the pool's own state -- in use, idle,
waiting, acquire wait -- attributable to a single process.

The pool's size is the tier's grip on the engine: it is what turns "500
concurrent requests" into "32 backends", and the S16 connection cliff is found
by moving it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

DEFAULT_MIN_SIZE = 4
DEFAULT_MAX_SIZE = 16


@dataclass(slots=True)
class PoolConfig:
    dsn: str
    min_size: int = DEFAULT_MIN_SIZE
    max_size: int = DEFAULT_MAX_SIZE
    name: str = "app->postgres"

    @classmethod
    def from_env(cls) -> PoolConfig:
        return cls(
            dsn=os.environ["DSEL_DSN"],
            min_size=int(os.environ.get("DSEL_POOL_MIN", DEFAULT_MIN_SIZE)),
            max_size=int(os.environ.get("DSEL_POOL_MAX", DEFAULT_MAX_SIZE)),
        )


async def open_pool(config: PoolConfig) -> Any:
    """Create the pool for this worker.

    `statement_cache_size` is left at asyncpg's default so statements are
    prepared once per connection -- PATH A does the same, and a difference
    there would land in S14's subtraction as though it were the tier's cost.
    """
    import asyncpg

    return await asyncpg.create_pool(
        dsn=config.dsn,
        min_size=config.min_size,
        max_size=config.max_size,
        # The tier must not paper over a saturated engine by queueing forever;
        # a timeout surfaces as an error the driver counts.
        command_timeout=30.0,
    )


def pool_state(pool: Any, name: str = "app->postgres") -> dict[str, int]:
    """The pool's own counters, as `PoolRecord` wants them."""
    if pool is None:
        return {"size": 0, "in_use": 0, "idle": 0, "waiting": 0}
    size = int(pool.get_size())
    idle = int(pool.get_idle_size())
    return {
        "size": size,
        "in_use": max(0, size - idle),
        "idle": idle,
        # asyncpg does not expose a waiter count; the acquire wait histogram in
        # `metrics.py` is what shows queueing, and inventing a number here
        # would be worse than not having one.
        "waiting": 0,
    }
