"""A picklable synthetic transport factory.

`run_pool` starts workers with the `spawn` context, so the factory has to be
importable by name in a fresh interpreter -- a closure or a `__main__`-level
function cannot cross that boundary, and a `__main__`-level one re-imports the
launching script in every child.
"""

from __future__ import annotations

from dsel.driver.transport import SyntheticTransport
from dsel.driver.worker import WorkerSpec

MEDIAN_US = 700.0
SIGMA = 0.5


def synthetic_factory(spec: WorkerSpec) -> SyntheticTransport:
    return SyntheticTransport(
        median_us=MEDIAN_US,
        sigma=SIGMA,
        worker=spec.worker,
        seed=spec.seed,
        offered_rate_per_s=spec.rate_per_s,
    )


def saturating_factory(spec: WorkerSpec) -> SyntheticTransport:
    """A target with a capacity, so a ramp has a knee to find (S11)."""
    return SyntheticTransport(
        median_us=MEDIAN_US,
        sigma=SIGMA,
        capacity_per_s=1200.0,
        worker=spec.worker,
        seed=spec.seed,
        offered_rate_per_s=spec.rate_per_s,
    )


# The target the S11 ramp is checked against. Its limits are arithmetic:
#   knee     = C - (C - baseline) / 2      latency doubles against step one
#   collapse = W / (median + W/C)          offering more returns less
RAMP_MEDIAN_US = 400.0
RAMP_SIGMA = 0.35
RAMP_CAPACITY_PER_S = 600.0
RAMP_WORKERS = 4


def ramp_factory(spec: WorkerSpec) -> SyntheticTransport:
    """A capacity-limited target for the rate ramp."""
    return SyntheticTransport(
        median_us=RAMP_MEDIAN_US,
        sigma=RAMP_SIGMA,
        capacity_per_s=RAMP_CAPACITY_PER_S,
        worker=spec.worker,
        seed=spec.seed,
        offered_rate_per_s=spec.rate_per_s,
    )
