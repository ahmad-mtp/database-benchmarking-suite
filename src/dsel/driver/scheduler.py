"""Open-loop arrival schedule (PLAN.md S10).

**Open loop, always, for any latency claim.** A closed-loop driver issues the
next request when the last one finishes, so a slow server is rewarded with
fewer requests and the latency it does record is the latency of a system that
was never actually asked for the offered load. That is coordinated omission,
and it does not show up as an error -- it shows up as a good-looking p99.

Two things here make the loop genuinely open:

1. **The schedule does not depend on completions.** An arrival's time is a
   function of its index alone, so nothing a worker does -- finishing late,
   blocking, dying -- can move it.
2. **Latency is measured from the scheduled start**, not from the moment the
   request was issued. A worker that falls 4 ms behind adds 4 ms to every
   latency it then records, which is exactly the truth being hidden by the
   closed-loop version.

The schedule is *index-derived*, `blake2b(seed|worker|index)`, not a PRNG
stream -- the same choice the research made for data generation, for the same
reason: a stream's values depend on how many were drawn before them, so it is
neither parallelism- nor process-independent, and it cannot be re-derived from
the audit bundle. An index-derived schedule can be recomputed for any single
arrival, months later, on another machine.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import blake2b

# 53 bits: exactly the mantissa of a float64, so the uniform draw loses nothing.
MANTISSA_BITS = 53
UNIFORM_SCALE = float(1 << MANTISSA_BITS)


def uniform(seed: int, worker: int, index: int) -> float:
    """A uniform draw in [0, 1) from the index alone."""
    digest = blake2b(
        f"{seed}|{worker}|{index}".encode(), digest_size=8, usedforsecurity=False
    ).digest()
    return (int.from_bytes(digest, "big") >> (64 - MANTISSA_BITS)) / UNIFORM_SCALE


def exponential_interval(rate_per_s: float, u: float) -> float:
    """Inverse-transform an exponential inter-arrival from a uniform draw."""
    if rate_per_s <= 0:
        raise ValueError("rate must be positive")
    # 1 - u is in (0, 1], so the log is defined at u = 0 and never at u = 1.
    return -math.log(1.0 - u) / rate_per_s


@dataclass(frozen=True, slots=True)
class ArrivalSchedule:
    """The arrival times one worker is responsible for.

    Offered rate is split evenly across workers. Each worker draws its own
    Poisson process at `rate / workers`; the superposition of independent
    Poisson processes is Poisson at the summed rate, so the aggregate arrival
    process is the one the spec asked for.
    """

    rate_per_s: float
    worker: int = 0
    workers: int = 1
    seed: int = 20260903

    def __post_init__(self) -> None:
        if self.workers < 1:
            raise ValueError("workers must be at least 1")
        if not 0 <= self.worker < self.workers:
            raise ValueError(f"worker {self.worker} outside 0..{self.workers - 1}")

    @property
    def worker_rate_per_s(self) -> float:
        return self.rate_per_s / self.workers

    @property
    def mean_interval_s(self) -> float:
        return 1.0 / self.worker_rate_per_s

    def interval(self, index: int) -> float:
        """The gap before arrival `index`. A function of the index alone."""
        return exponential_interval(
            self.worker_rate_per_s, uniform(self.seed, self.worker, index)
        )

    def offset(self, index: int) -> float:
        """Seconds from the start of the phase to arrival `index`.

        O(index); use `offsets()` to walk a schedule forward. Present because
        being able to re-derive one arrival in isolation is the property that
        makes the schedule auditable.
        """
        return math.fsum(self.interval(i) for i in range(index + 1))

    def offsets(self, count: int) -> Iterator[float]:
        """The first `count` arrival offsets, accumulated forward."""
        total = 0.0
        for index in range(count):
            total += self.interval(index)
            yield total

    def offsets_until(self, duration_s: float) -> Iterator[float]:
        """Every arrival that falls within `duration_s` of the start."""
        total = 0.0
        index = 0
        while True:
            total += self.interval(index)
            if total > duration_s:
                return
            yield total
            index += 1

    def expected_count(self, duration_s: float) -> float:
        return self.worker_rate_per_s * duration_s
