"""Span timing across the app tier (PLAN.md S13).

Four instants per request:

    t_app_recv ---- t_db_start ---- t_db_end ---- t_app_send
       |                |               |              |
       +-- framework ---+--- engine ----+--- serialise +

The middle interval is the only one the engine is responsible for, and it is
the one PATH B has to be able to compare against PATH A's end-to-end latency
(S14). The two outer intervals are the tier's own cost, which is the whole
point of running PATH B at all -- without them the app tier is a black box that
makes every engine look the same.

Timestamps come from `time.perf_counter_ns`, which is monotonic and has
nanosecond resolution. Wall clock would be wrong here: it can step.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


def now_ns() -> int:
    return time.perf_counter_ns()


@dataclass(slots=True)
class Span:
    """One request's four instants, filled in as it progresses.

    Created by the ASGI middleware on the way in and finished on the way out,
    so `t_app_recv` precedes routing and `t_app_send` follows serialisation. A
    span created inside a handler misses both, and the tier's own cost then
    reads as a microsecond.
    """

    endpoint: str
    t_app_recv: int = field(default_factory=now_ns)
    t_db_start: int = 0
    t_db_end: int = 0
    t_app_send: int = 0
    ok: bool = True

    def db_begin(self) -> None:
        self.t_db_start = now_ns()

    def db_finish(self) -> None:
        self.t_db_end = now_ns()

    def send(self) -> None:
        self.t_app_send = now_ns()

    @property
    def recv_to_db_start_us(self) -> float:
        """Framework, routing, pool acquisition. The tier's entry cost."""
        if not self.t_db_start:
            return 0.0
        return (self.t_db_start - self.t_app_recv) / 1000.0

    @property
    def db_us(self) -> float:
        """What the engine took, as the app tier saw it.

        S14 compares this distribution against PATH A's end-to-end latency. It
        is not the same quantity -- it still carries the app-to-engine network
        hop -- and the comparison is of *overlap*, not equality.
        """
        if not (self.t_db_start and self.t_db_end):
            return 0.0
        return (self.t_db_end - self.t_db_start) / 1000.0

    @property
    def db_end_to_send_us(self) -> float:
        """Serialisation and response write. The tier's exit cost."""
        if not (self.t_db_end and self.t_app_send):
            return 0.0
        return (self.t_app_send - self.t_db_end) / 1000.0

    @property
    def total_us(self) -> float:
        end = self.t_app_send or self.t_db_end or self.t_app_recv
        return (end - self.t_app_recv) / 1000.0

    @property
    def tier_overhead_us(self) -> float:
        """Everything that was not the engine."""
        return self.total_us - self.db_us
