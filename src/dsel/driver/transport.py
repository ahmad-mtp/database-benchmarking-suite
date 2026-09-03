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
import time
from dataclasses import dataclass
from hashlib import blake2b
from typing import Protocol, runtime_checkable


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
            utilisation = self.offered_rate_per_s / self.capacity_per_s
            if utilisation >= 1.0:
                # Past capacity the queue grows without bound; the shape here is
                # only required to be steep and monotonic, not a real M/M/1.
                service *= 1.0 + 40.0 * (utilisation - 1.0) + 4.0
            else:
                service *= 1.0 / (1.0 - utilisation)
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
        _busy_wait(self.service_us(index) / 1_000_000.0)


def _busy_wait(seconds: float) -> None:
    """Spend `seconds` without sleeping.

    `time.sleep` on macOS rounds to the timer's granularity, which is coarse
    against the sub-millisecond service times this transport produces -- a
    900 us sleep is not a 900 us sleep, and the histogram would be measuring
    the scheduler. Spinning costs a core, which is exactly why the driver has
    its own cpuset and a per-worker CPU gate.
    """
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        pass
