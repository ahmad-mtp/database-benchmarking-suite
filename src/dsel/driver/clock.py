"""Waiting accurately enough to measure with (PLAN.md S10-S11).

Measured on this host, 2026-09-03, macOS 26.6.2 on an Apple M5:

    time.sleep(1 ms)   overshoots by  514 us (p50),   525 us (p99)
    time.sleep(5 ms)   overshoots by 2514 us (p50),  2528 us (p99)
    time.sleep(20 ms)  overshoots by 8529 us (p50), 10100 us (p99)

That is not a fixed granularity to subtract -- the overshoot is roughly *half
the requested interval* plus half a millisecond, because the kernel coalesces
timers. Sleeping `remaining - 2ms` therefore lands 17 ms late on a 40 ms wait,
and since an open-loop driver measures latency from the *scheduled* start, all
17 ms go straight into every latency it records. The first ramp built on a
fixed margin reported a p50 of 3.7 ms against a target whose service time was
0.5 ms, and the knee was invisible underneath it.

The fix does not need the host's constants. Sleeping a fixed *fraction* of the
time remaining converges on the deadline geometrically and cannot overshoot it:
a `0.4 x remaining` sleep elapses about `0.6 x remaining + 500 us`, which is
short of the deadline whenever `remaining` is over ~1.25 ms. Below that the
loop stops sleeping and spins, so the last stretch costs CPU but is exact.

Both places that wait use this: the worker waiting for an arrival, and the
synthetic transport spending a service time. A model target that overshot its
own service time by 50% would not be a known quantity.
"""

from __future__ import annotations

import time

# Sleep this fraction of what is left, repeatedly.
SLEEP_FRACTION = 0.4
# Below this, spin. Sized from the measurement above: at 1.25 ms the observed
# overshoot exactly consumes the remaining time, so the sleep loop must stop
# before then or it can pass the deadline.
SPIN_MARGIN_S = 0.0015


def wait_until(deadline: float) -> None:
    """Block until `time.perf_counter()` reaches `deadline`, without overshoot."""
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= SPIN_MARGIN_S:
            break
        time.sleep(remaining * SLEEP_FRACTION)
    while time.perf_counter() < deadline:
        pass


def spend(seconds: float) -> None:
    """Spend `seconds` from now, without overshoot."""
    if seconds <= 0:
        return
    wait_until(time.perf_counter() + seconds)


def measure_sleep_overshoot(target_s: float, repeats: int = 100) -> tuple[float, float]:
    """`(p50, p99)` overshoot of `time.sleep(target_s)`, in microseconds.

    Kept in the harness rather than in a test: the number belongs in the run
    manifest. A bundle recorded on a host with a different timer has a
    different noise floor under every latency in it.
    """
    overshoot: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        time.sleep(target_s)
        overshoot.append((time.perf_counter() - started - target_s) * 1_000_000.0)
    overshoot.sort()
    p50 = overshoot[len(overshoot) // 2]
    p99 = overshoot[min(len(overshoot) - 1, int(len(overshoot) * 0.99))]
    return p50, p99
