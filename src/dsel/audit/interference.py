"""Cross-cpuset interference measurement (PLAN.md S1, follow-on).

PLAN.md's hardware slice treats `cpu 2-5` (engine) and `cpu 6-9` (driver) as
independent pools. On Docker Desktop they are not: `--cpuset-cpus` partitions
*guest* vCPUs, and Virtualization.framework multiplexes all ten guest vCPU
threads across the host's 4 performance + 6 efficiency cores. Load on one
cpuset therefore steals host cores from the other.

This measures how much. A contention load of N workers is pinned to one cpuset
while the other cpuset's aggregate capacity is measured; `retained` is that
capacity as a fraction of its uncontended value.

Two design points, both about not fooling ourselves:

* **Randomised block order.** A sweep run as 0,1,2,4 workers in sequence
  confounds the dose with thermal drift -- a machine that slows down over five
  minutes produces a textbook dose-response curve from nothing. Each block runs
  every level in a seeded shuffle instead, so drift lands on all levels alike.
* **Bootstrap CI over blocks**, not over raw samples. The block is the
  independent unit; resampling within a block would treat one thermal state as
  many observations and understate the interval.
"""

from __future__ import annotations

import json
import random
import statistics
import subprocess
import uuid
from dataclasses import dataclass

from dsel.audit.models import ImagePin, InterferenceLevel, InterferenceSweepRecord

DEFAULT_LEVELS = (0, 1, 2, 4)
DEFAULT_BLOCKS = 6
DEFAULT_WINDOW_S = 2.0
DEFAULT_SEED = 20260902
BOOTSTRAP_RESAMPLES = 10_000

# Recorded when the measured cpuset loses more than the tolerance under load
# on a cpuset it does not share. PLAN.md treats the two pools as independent.
REASON_ISOLATION_INEFFECTIVE = "cpuset_isolation_ineffective"

ENGINE_CPUSET_SPEC = "2-5"
DRIVER_CPUSET_SPEC = "6-9"
CPUSET_WIDTH = 4

# Measures aggregate capacity of whatever cpuset the cgroup grants. Workers are
# separate processes: one Python process cannot saturate four vCPUs (PLAN.md D6,
# the same reason the load driver is multi-process).
_CAPACITY_PAYLOAD = r"""
import json, multiprocessing as mp, sys, time

WINDOW_S = {window_s}
WORKERS = {workers}

def burn(q):
    n, x = 0, 1
    end = time.monotonic_ns() + int(WINDOW_S * 1e9)
    t0 = time.monotonic_ns()
    while time.monotonic_ns() < end:
        for _ in range(1000):
            x = (x * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
        n += 1
    q.put(n * 1000 / ((time.monotonic_ns() - t0) / 1e9))

if __name__ == "__main__":
    q = mp.Queue()
    ps = [mp.Process(target=burn, args=(q,)) for _ in range(WORKERS)]
    for p in ps: p.start()
    rates = [q.get() for _ in ps]
    for p in ps: p.join()
    json.dump({{"aggregate_ops_per_s": sum(rates), "per_worker": rates}}, sys.stdout)
"""

# Spins until killed. Detached, so the measurement runs against a live load.
_CONTENTION_PAYLOAD = r"""
import multiprocessing as mp

def spin():
    x = 1
    while True:
        x = (x * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF

if __name__ == "__main__":
    ps = [mp.Process(target=spin, daemon=True) for _ in range({workers})]
    for p in ps: p.start()
    for p in ps: p.join()
"""


@dataclass(frozen=True, slots=True)
class LevelResult:
    """Capacity retained by the measured cpuset at one contention level."""

    contention_workers: int
    retained_median: float
    retained_ci_low: float
    retained_ci_high: float
    absolute_median_ops_per_s: float
    blocks: int


@dataclass(frozen=True, slots=True)
class InterferenceSweep:
    """One direction of the sweep."""

    measured_cpuset: str
    contended_cpuset: str
    window_s: float
    blocks: int
    seed: int
    levels: list[LevelResult]

    @property
    def worst_retained(self) -> LevelResult:
        return min(self.levels, key=lambda level: level.retained_median)

    def isolation_holds(self, tolerance: float = 0.10) -> bool:
        """True when the heaviest contention costs less than `tolerance`."""
        return self.worst_retained.retained_ci_low >= 1.0 - tolerance


def _image_ref(image: ImagePin) -> str:
    return f"{image.reference.split(':')[0]}@{image.index_digest}"


def _measure_capacity(image: ImagePin, cpuset: str, workers: int, window_s: float) -> float:
    payload = _CAPACITY_PAYLOAD.format(window_s=window_s, workers=workers)
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            # Both flags, per the invariant: quota alone leaves the container
            # seeing every host core.
            "--cpuset-cpus",
            cpuset,
            "--cpus",
            str(workers),
            _image_ref(image),
            "python3",
            "-",
        ],
        input=payload,
        capture_output=True,
        text=True,
        timeout=window_s * 4 + 60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"capacity probe exited {result.returncode}: {result.stderr.strip()}"
        )
    return float(json.loads(result.stdout)["aggregate_ops_per_s"])


class _Contention:
    """A detached spinning load on a cpuset, removed on exit even on error."""

    def __init__(self, image: ImagePin, cpuset: str, workers: int) -> None:
        self._image = image
        self._cpuset = cpuset
        self._workers = workers
        self._name = f"dsel-contend-{uuid.uuid4().hex[:8]}"

    def __enter__(self) -> _Contention:
        if self._workers == 0:
            return self
        subprocess.run(
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                self._name,
                "--cpuset-cpus",
                self._cpuset,
                "--cpus",
                str(self._workers),
                _image_ref(self._image),
                "python3",
                "-c",
                _CONTENTION_PAYLOAD.format(workers=self._workers),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        return self

    def __exit__(self, *exc: object) -> None:
        if self._workers == 0:
            return
        subprocess.run(
            ["docker", "rm", "--force", self._name],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )


def _bootstrap_ci(
    ratios: list[float], resamples: int, rng: random.Random
) -> tuple[float, float]:
    """Percentile bootstrap over blocks. Blocks are the independent unit."""
    if len(ratios) < 2:
        return (min(ratios), max(ratios)) if ratios else (0.0, 0.0)
    medians = []
    n = len(ratios)
    for _ in range(resamples):
        medians.append(statistics.median(rng.choices(ratios, k=n)))
    medians.sort()
    return medians[int(0.025 * resamples)], medians[int(0.975 * resamples)]


def sweep(
    image: ImagePin,
    measured_cpuset: str = ENGINE_CPUSET_SPEC,
    contended_cpuset: str = DRIVER_CPUSET_SPEC,
    levels: tuple[int, ...] = DEFAULT_LEVELS,
    blocks: int = DEFAULT_BLOCKS,
    window_s: float = DEFAULT_WINDOW_S,
    seed: int = DEFAULT_SEED,
    workers: int = CPUSET_WIDTH,
) -> InterferenceSweep:
    """Measure `measured_cpuset` capacity under load on `contended_cpuset`."""
    if 0 not in levels:
        raise ValueError("levels must include 0: it is the uncontended reference")
    rng = random.Random(seed)
    raw: dict[int, list[float]] = {level: [] for level in levels}

    for block in range(blocks):
        order = list(levels)
        rng.shuffle(order)  # drift must not align with dose
        for level in order:
            with _Contention(image, contended_cpuset, level):
                raw[level].append(_measure_capacity(image, measured_cpuset, workers, window_s))
        del block

    baseline = raw[0]
    results: list[LevelResult] = []
    for level in sorted(levels):
        # Pair each block's measurement with that block's own baseline, so a
        # block that ran hot cancels rather than skewing the ratio.
        ratios = [
            obs / base for obs, base in zip(raw[level], baseline, strict=True) if base > 0
        ]
        low, high = _bootstrap_ci(ratios, BOOTSTRAP_RESAMPLES, rng)
        results.append(
            LevelResult(
                contention_workers=level,
                retained_median=round(statistics.median(ratios), 4),
                retained_ci_low=round(low, 4),
                retained_ci_high=round(high, 4),
                absolute_median_ops_per_s=round(statistics.median(raw[level]), 1),
                blocks=len(ratios),
            )
        )
    return InterferenceSweep(
        measured_cpuset=measured_cpuset,
        contended_cpuset=contended_cpuset,
        window_s=window_s,
        blocks=blocks,
        seed=seed,
        levels=results,
    )


def to_record(result: InterferenceSweep, tolerance: float = 0.10) -> InterferenceSweepRecord:
    """Convert a sweep into its manifest form."""
    return InterferenceSweepRecord(
        measured_cpuset=result.measured_cpuset,
        contended_cpuset=result.contended_cpuset,
        window_s=result.window_s,
        blocks=result.blocks,
        seed=result.seed,
        levels=[
            InterferenceLevel(
                contention_workers=level.contention_workers,
                retained_median=level.retained_median,
                retained_ci_low=level.retained_ci_low,
                retained_ci_high=level.retained_ci_high,
                absolute_median_ops_per_s=level.absolute_median_ops_per_s,
                blocks=level.blocks,
            )
            for level in result.levels
        ],
        isolation_holds_within_10pct=result.isolation_holds(tolerance),
    )


def reasons_for(sweeps: list[InterferenceSweep], tolerance: float = 0.10) -> list[str]:
    """Deviation reasons implied by the sweeps."""
    if any(not s.isolation_holds(tolerance) for s in sweeps):
        return [REASON_ISOLATION_INEFFECTIVE]
    return []
