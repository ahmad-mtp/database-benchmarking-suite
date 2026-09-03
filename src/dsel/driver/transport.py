"""What a worker issues an operation against (PLAN.md S10).

The driver is deliberately ignorant of what it is talking to. PATH A points a
transport straight at an engine, PATH B points it at the app tier, and the
delta between the two is the tier's cost -- which is only a clean subtraction
if the driver above them is identical.

`SyntheticTransport` is not a stub. S11 needs "a synthetic target whose limits
are known by construction" to check that the ramp recovers a knee and a
collapse point, and S10 needs a target whose latency distribution is known
exactly so that a percentile disagreement can only be the histogram's fault.
Its service time is index-derived for the same reason the arrival schedule is:
a rerun reproduces the same target, and a failure can be replayed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from hashlib import blake2b
from typing import Protocol, runtime_checkable

from dsel.driver.clock import spend


class TransportError(RuntimeError):
    """The operation failed. Counted as an error, never silently retried."""


@runtime_checkable
class Transport(Protocol):
    """One connection's worth of capability, owned by one worker."""

    def open(self) -> None:
        """Establish whatever the transport needs. Called before the schedule."""

    def execute(self, op: str, index: int) -> None:
        """Issue one operation. Raise `TransportError` to count a failure."""

    def close(self) -> None:
        """Release everything. Called even when the phase failed."""


def _uniform(seed: int, worker: int, index: int, salt: str) -> float:
    digest = blake2b(
        f"{seed}|{worker}|{index}|{salt}".encode(), digest_size=8, usedforsecurity=False
    ).digest()
    return (int.from_bytes(digest, "big") >> 11) / float(1 << 53)


def deliverable_rate(
    median_us: float, capacity_per_s: float, workers: int, offered_per_s: float
) -> float:
    """What `workers` one-in-flight workers can actually deliver at `offered`.

    Closed form, so a ramp against this target has a knee and a collapse point
    that are known before the ramp is run rather than read off its own output:

        s(r) = median / (1 - r/C)          service time under utilisation r/C
        achieved(r) = min(r, workers/s(r))

    `achieved` rises with `r` until the two terms cross and then falls, which
    is throughput collapse -- the same shape an overloaded system gives, and
    here it is arithmetic.
    """
    service_s = service_seconds(median_us, capacity_per_s, offered_per_s)
    return min(offered_per_s, workers / service_s)


# Where the M/M/1 term is cut off. 1/(1 - rho) is unbounded as rho -> 1, and a
# model with a pole in it is not a target -- past this the growth continues
# linearly from the same value, so the curve is continuous and monotonic
# everywhere instead of jumping at the pole.
KNEE_CUTOFF = 0.95
CUTOFF_SLOPE = 100.0


def service_multiplier(utilisation: float) -> float:
    """How much slower the target is at this utilisation. Continuous at the cut."""
    if utilisation < KNEE_CUTOFF:
        return 1.0 / (1.0 - utilisation)
    return 1.0 / (1.0 - KNEE_CUTOFF) + CUTOFF_SLOPE * (utilisation - KNEE_CUTOFF)


def service_seconds(median_us: float, capacity_per_s: float, offered_per_s: float) -> float:
    """The mean service time the synthetic target gives at an offered rate."""
    return median_us * service_multiplier(offered_per_s / capacity_per_s) / 1_000_000.0


def collapse_rate(median_us: float, capacity_per_s: float, workers: int) -> float:
    """Where `achieved(r)` peaks: `r = workers / s(r)` solved for r.

    Below the cutoff `s(r) = median / (1 - r/C)`, so

        r = workers (1 - r/C) / median   ->   r = workers / (median + workers/C)

    Past that point offering more returns less, which is throughput collapse.
    """
    median_s = median_us / 1_000_000.0
    return workers / (median_s + workers / capacity_per_s)


def knee_rate(capacity_per_s: float, baseline_per_s: float, factor: float = 2.0) -> float:
    """Where latency reaches `factor` times its value at `baseline_per_s`.

    A ramp measures the knee against its own first step, not against an
    unloaded target it never ran, so the closed form has to say the same:
    `(1 - b/C) / (1 - r/C) = factor` gives `r = C - (C - b) / factor`.
    """
    return capacity_per_s - (capacity_per_s - baseline_per_s) / factor


@dataclass(slots=True)
class SyntheticTransport:
    """A target with a service-time distribution known by construction.

    Log-normal service time, which is the shape a database's response time
    actually takes -- a tight body with a long right tail -- rather than the
    uniform or constant a placeholder would use. Percentiles of a log-normal
    are closed-form, so the histogram can be checked against arithmetic instead
    of against itself.

    `capacity_per_s` optionally adds a queue: past that arrival rate the target
    degrades rather than staying flat, which is what gives S11 a knee to find.
    """

    median_us: float = 800.0
    sigma: float = 0.55
    capacity_per_s: float | None = None
    error_rate: float = 0.0
    seed: int = 20260903
    worker: int = 0
    offered_rate_per_s: float = 0.0
    _opened: bool = False

    def open(self) -> None:
        self._opened = True

    def close(self) -> None:
        self._opened = False

    def service_us(self, index: int) -> float:
        """The service time for one operation. Pure in the index."""
        u = _uniform(self.seed, self.worker, index, "service")
        # Box-Muller from two independent uniforms, then exponentiate.
        v = _uniform(self.seed, self.worker, index, "service2")
        normal = math.sqrt(-2.0 * math.log(1.0 - u)) * math.cos(2.0 * math.pi * v)
        service = self.median_us * math.exp(self.sigma * normal)
        if self.capacity_per_s and self.offered_rate_per_s > 0:
            # The multiplier scales the whole distribution, so every percentile
            # moves together and the knee is where the closed form says it is.
            service *= service_multiplier(self.offered_rate_per_s / self.capacity_per_s)
        return service

    def theoretical_percentile_us(self, percentile: float) -> float:
        """The closed-form percentile of the underlying log-normal.

        Only valid without the queue term; used to check the histogram against
        arithmetic rather than against another histogram.
        """
        if self.capacity_per_s:
            raise ValueError("closed form does not hold once the queue term is on")
        # Inverse normal CDF by Acklam's rational approximation is overkill
        # here: math.erf is exact enough, inverted by bisection.
        target = percentile / 100.0
        low, high = -10.0, 10.0
        for _ in range(200):
            mid = (low + high) / 2.0
            if 0.5 * (1.0 + math.erf(mid / math.sqrt(2.0))) < target:
                low = mid
            else:
                high = mid
        return self.median_us * math.exp(self.sigma * (low + high) / 2.0)

    def execute(self, op: str, index: int) -> None:
        if not self._opened:
            raise TransportError("transport used before open()")
        if self.error_rate and _uniform(self.seed, self.worker, index, "err") < self.error_rate:
            raise TransportError(f"{op}: synthetic failure at index {index}")
        # A model target that overshot its own service time by 50% would not
        # be a known quantity; `clock.spend` is why it does not.
        spend(self.service_us(index) / 1_000_000.0)


# --- Postgres -------------------------------------------------------------
#
# asyncpg rather than psycopg: psycopg3 is LGPL-3.0 and PLAN.md's toolchain is
# GPL-free by locked decision, checked by `tests/unit/test_no_copyleft_deps`.
# asyncpg is Apache-2.0 and is already the locked choice for the app tier, so
# PATH A and PATH B talk to Postgres through the same library and the delta
# between them is the tier, not the driver.

# pgbench -S, verbatim. The calibration comparison is only meaningful if the
# two tools issue the same statement against the same table.
PGBENCH_SELECT = "SELECT abalance FROM pgbench_accounts WHERE aid = $1"
PGBENCH_ACCOUNTS_PER_SCALE = 100_000


@dataclass(slots=True)
class PostgresTransport:
    """One asyncpg connection, driven synchronously.

    A worker holds one request in flight, so an event loop per worker running
    one coroutine at a time is the whole concurrency model. The loop is created
    once and reused: `asyncio.run` per operation would build and tear down a
    loop inside the measurement window and put that in the histogram.

    The account id is drawn index-derived, from the same `blake2b` construction
    as the arrival schedule and over the same range pgbench uses, so the two
    tools read the same rows in the same proportions.
    """

    dsn: str
    scale: int = 10
    seed: int = 20260903
    worker: int = 0
    statement: str = PGBENCH_SELECT
    _loop: object = None
    _conn: object = None

    @property
    def rows(self) -> int:
        return self.scale * PGBENCH_ACCOUNTS_PER_SCALE

    def open(self) -> None:
        import asyncio

        import asyncpg

        loop = asyncio.new_event_loop()
        self._loop = loop
        self._conn = loop.run_until_complete(asyncpg.connect(self.dsn))

    def close(self) -> None:
        import asyncio

        loop = self._loop
        if loop is None:
            return
        assert isinstance(loop, asyncio.AbstractEventLoop)
        if self._conn is not None:
            loop.run_until_complete(self._conn.close())  # type: ignore[attr-defined]
            self._conn = None
        loop.close()
        self._loop = None

    def account_id(self, index: int) -> int:
        """Uniform over the same range as pgbench's `random(1, 100000 * scale)`."""
        return 1 + int(_uniform(self.seed, self.worker, index, "aid") * self.rows)

    def execute(self, op: str, index: int) -> None:
        import asyncio

        loop, conn = self._loop, self._conn
        if loop is None or conn is None:
            raise TransportError("transport used before open()")
        assert isinstance(loop, asyncio.AbstractEventLoop)
        try:
            loop.run_until_complete(
                conn.fetchval(self.statement, self.account_id(index))  # type: ignore[attr-defined]
            )
        except Exception as exc:  # asyncpg raises a wide family; all count as errors
            raise TransportError(f"{op}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class PostgresFactory:
    """A picklable transport factory for the worker pool.

    Workers start under `spawn`, which re-imports the module and pickles the
    factory. A module-level function would therefore carry only its *default*
    configuration into the child -- a DSN assigned to a global in `main` never
    arrives. An instance carries its own state across the boundary.
    """

    dsn: str
    scale: int = 10
    statement: str = PGBENCH_SELECT

    def __call__(self, spec: object) -> PostgresTransport:
        worker = getattr(spec, "worker", 0)
        seed = getattr(spec, "seed", 20260903)
        return PostgresTransport(
            dsn=self.dsn,
            scale=self.scale,
            seed=seed,
            worker=worker,
            statement=self.statement,
        )
