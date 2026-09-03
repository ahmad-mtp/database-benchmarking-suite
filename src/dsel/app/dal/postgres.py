"""The Postgres data access layer (PLAN.md S13).

One place where SQL meets the pool, so the app tier's own cost can be measured
apart from the engine's. Every method takes a `Span` and marks the engine
boundary on it: the tier's entry and exit costs are then whatever is left, and
nobody has to trust a claim about where the time went.

The statements are the same ones PATH A issues, because S14's whole
subtraction depends on it: PATH B's `t_db_end - t_db_start` is only comparable
to PATH A's latency if both asked the engine for the same thing.
"""

from __future__ import annotations

from typing import Any

from dsel.app.spans import Span
from dsel.driver.transport import PGBENCH_ACCOUNTS_PER_SCALE

SELECT_ACCOUNT = "SELECT abalance FROM pgbench_accounts WHERE aid = $1"
COUNT_RANGE = "SELECT count(*) FROM pgbench_accounts WHERE aid BETWEEN $1 AND $1 + 5000"
# A real join, for S19. Kept beside the others so the join work is not a
# separate code path with its own timing.
JOIN_REPORT = (
    "SELECT b.bid, count(*) AS accounts, sum(a.abalance) AS balance "
    "FROM pgbench_accounts a JOIN pgbench_branches b ON b.bid = a.bid "
    "WHERE a.aid BETWEEN $1 AND $1 + 20000 GROUP BY b.bid"
)


class PostgresDal:
    """Statements against one asyncpg pool."""

    def __init__(self, pool: Any, scale: int = 10) -> None:
        self.pool = pool
        self.scale = scale

    @property
    def rows(self) -> int:
        return self.scale * PGBENCH_ACCOUNTS_PER_SCALE

    # Acquisition happens *before* `db_begin`. Pool acquisition is the tier's
    # grip on the engine, not the engine: it queues behind other requests, it
    # is bounded by the pool size, and at S16 it is the thing that produces the
    # connection cliff. Timing it as engine work put PATH B's "engine interval"
    # 40% above PATH A's whole latency -- 142 us against 101 us -- for two
    # measurements that are supposed to be of the same thing.

    async def select_account(self, aid: int, span: Span) -> int | None:
        async with self.pool.acquire() as connection:
            span.db_begin()
            try:
                return await connection.fetchval(SELECT_ACCOUNT, aid)  # type: ignore[no-any-return]
            finally:
                span.db_finish()

    async def count_range(self, aid: int, span: Span) -> int | None:
        async with self.pool.acquire() as connection:
            span.db_begin()
            try:
                return await connection.fetchval(COUNT_RANGE, aid)  # type: ignore[no-any-return]
            finally:
                span.db_finish()

    async def join_report(self, aid: int, span: Span) -> list[dict[str, Any]]:
        async with self.pool.acquire() as connection:
            span.db_begin()
            try:
                rows = await connection.fetch(JOIN_REPORT, aid)
                return [dict(row) for row in rows]
            finally:
                span.db_finish()
