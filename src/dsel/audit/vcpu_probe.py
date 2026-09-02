"""The vCPU speed probe (PLAN.md S1).

PLAN.md's premise: the M5 is 4 performance + 6 efficiency cores, so `cpuset 2-5`
(engine) and `6-9` (driver) may straddle core classes, and findings.md chose
those sets without examining it. The probe measures fixed integer work on each
vCPU and flags `heterogeneous_cores` when the two sets differ by more than 10%.

What the probe found on this host is that the question cannot be answered from
inside the VM. Virtualization.framework schedules guest vCPU threads onto host
cores dynamically, so every guest vCPU sees the same statistical mix of P and E
cores: the between-vCPU span is ~3% while the within-vCPU spread reaches ~36%.
The noise is an order of magnitude larger than the signal.

So `heterogeneous_cores` does not fire here -- and a bare `false` would read as
"we checked, the cores are uniform", which is not what the data says. When the
between-vCPU span cannot be resolved above the within-vCPU noise, the probe
additionally reports `vcpu_speed_indistinguishable`: `--cpuset-cpus` gives
containers mutual isolation but does not select a host core class, and cannot be
relied on for a stable performance level.
"""

from __future__ import annotations

import json
import statistics
import subprocess
from dataclasses import dataclass

from dsel.audit.models import ImagePin, VcpuProbe, VcpuSpeed

# PLAN.md fixes the unit of work at 200 ms per vCPU.
WORK_MS = 200
# Repeats are not specified. Random dropouts of 20-36% were observed on single
# 200 ms windows, so a single sample per vCPU is not usable; the median over
# repeats is. The first repeat is discarded regardless.
DEFAULT_REPEATS = 6
WARMUP_REPEATS = 1

# PLAN.md's hardware slices.
ENGINE_CPUSET = (2, 3, 4, 5)
DRIVER_CPUSET = (6, 7, 8, 9)

# PLAN.md S1: "If engine-set and driver-set aggregate speed differ >10%".
HETEROGENEITY_THRESHOLD_PCT = 10.0

REASON_HETEROGENEOUS = "heterogeneous_cores"
REASON_INDISTINGUISHABLE = "vcpu_speed_indistinguishable"

# The payload runs inside the container. Passed on stdin, never bind-mounted:
# D7 forbids a VirtioFS mount inside a measurement window, and this is one.
_PAYLOAD = r'''
import json, os, statistics, sys, time

WORK_MS = {work_ms}
REPEATS = {repeats}

def burn(deadline_ns):
    """Fixed integer work. Deliberately not a library call: the unit must be
    identical across vCPUs and across runs, and must not be JIT-able away."""
    n, x = 0, 1
    while time.monotonic_ns() < deadline_ns:
        for _ in range(1000):
            x = (x * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
        n += 1
    return n

available = sorted(os.sched_getaffinity(0))
grid = {{cpu: [] for cpu in available}}
for _ in range(REPEATS):
    for cpu in available:
        os.sched_setaffinity(0, {{cpu}})
        burn(time.monotonic_ns() + 30_000_000)          # settle, discarded
        t0 = time.monotonic_ns()
        iters = burn(t0 + WORK_MS * 1_000_000)
        elapsed = (time.monotonic_ns() - t0) / 1e9
        grid[cpu].append(iters * 1000 / elapsed)        # inner loop is 1000 ops

json.dump({{"available": available, "samples": grid}}, sys.stdout)
'''


@dataclass(frozen=True, slots=True)
class ProbeAnalysis:
    """The derived numbers and the reasons they imply."""

    relative_speed: list[float]
    engine_aggregate: float
    driver_aggregate: float
    set_difference_pct: float
    between_vcpu_span_pct: float
    max_within_vcpu_spread_pct: float
    reasons: list[str]


def _pct_difference(a: float, b: float) -> float:
    """Difference between two aggregates as a percentage of the larger."""
    larger = max(a, b)
    if larger <= 0:
        return 0.0
    return abs(a - b) / larger * 100.0


def analyse(
    speeds: list[VcpuSpeed],
    engine_cpuset: tuple[int, ...] = ENGINE_CPUSET,
    driver_cpuset: tuple[int, ...] = DRIVER_CPUSET,
) -> ProbeAnalysis:
    """Derive the relative-speed vector and the deviation reasons.

    Pure: takes measurements, returns conclusions. The whole point is that the
    flag can be checked against vectors whose correct answer is known.
    """
    if not speeds:
        raise ValueError("no vCPU measurements")
    by_cpu = {s.vcpu: s for s in speeds}
    missing = [c for c in (*engine_cpuset, *driver_cpuset) if c not in by_cpu]
    if missing:
        raise ValueError(f"cpuset references vCPUs that were not measured: {missing}")

    medians = [s.median_ips for s in sorted(speeds, key=lambda s: s.vcpu)]
    fastest = max(medians)
    relative = [round(m / fastest, 6) for m in medians] if fastest > 0 else [0.0] * len(medians)

    engine_agg = sum(by_cpu[c].median_ips for c in engine_cpuset)
    driver_agg = sum(by_cpu[c].median_ips for c in driver_cpuset)
    set_diff = _pct_difference(engine_agg, driver_agg)

    slowest = min(medians)
    span = (fastest - slowest) / fastest * 100.0 if fastest > 0 else 0.0
    max_spread = max(s.spread_pct for s in speeds)

    reasons: list[str] = []
    if set_diff > HETEROGENEITY_THRESHOLD_PCT:
        reasons.append(REASON_HETEROGENEOUS)
    # The probe cannot resolve core classes when its own noise exceeds the
    # difference it is trying to measure. Recording this keeps a false
    # `heterogeneous_cores` from reading as a positive finding of uniformity.
    if span < max_spread:
        reasons.append(REASON_INDISTINGUISHABLE)
    return ProbeAnalysis(
        relative_speed=relative,
        engine_aggregate=engine_agg,
        driver_aggregate=driver_agg,
        set_difference_pct=round(set_diff, 4),
        between_vcpu_span_pct=round(span, 4),
        max_within_vcpu_spread_pct=round(max_spread, 4),
        reasons=reasons,
    )


def _summarise(samples: dict[str, list[float]], warmup: int) -> list[VcpuSpeed]:
    speeds: list[VcpuSpeed] = []
    for cpu, values in sorted(samples.items(), key=lambda kv: int(kv[0])):
        measured = values[warmup:] or values
        speeds.append(
            VcpuSpeed(
                vcpu=int(cpu),
                median_ips=statistics.median(measured),
                min_ips=min(measured),
                max_ips=max(measured),
                samples=len(measured),
            )
        )
    return speeds


def run_probe(
    image: ImagePin,
    repeats: int = DEFAULT_REPEATS,
    work_ms: int = WORK_MS,
    warmup: int = WARMUP_REPEATS,
) -> tuple[VcpuProbe, list[str]]:
    """Run the probe in a digest-pinned container; return the result and reasons."""
    if repeats <= warmup:
        raise ValueError(f"repeats ({repeats}) must exceed warmup ({warmup})")
    payload = _PAYLOAD.format(work_ms=work_ms, repeats=repeats)
    repo = image.reference.split(":")[0]
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            # Both, per the invariant: quota alone leaves the container seeing
            # all host cores. The probe needs every vCPU visible, so the cpuset
            # is the full set and the quota matches it.
            "--cpuset-cpus",
            f"0-{_daemon_ncpu() - 1}",
            f"{repo}@{image.index_digest}",
            "python3",
            "-",
        ],
        input=payload,
        capture_output=True,
        text=True,
        timeout=60 + repeats * 10 * (work_ms / 1000 + 0.2) * 2,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"vCPU probe container exited {result.returncode}: {result.stderr.strip()}"
        )
    raw = json.loads(result.stdout)
    speeds = _summarise(raw["samples"], warmup)
    analysis = analyse(speeds)
    probe = VcpuProbe(
        work_ms=work_ms,
        repeats=repeats,
        discarded_warmup=warmup,
        per_vcpu=speeds,
        relative_speed=analysis.relative_speed,
        engine_cpuset=list(ENGINE_CPUSET),
        driver_cpuset=list(DRIVER_CPUSET),
        engine_aggregate_ips=analysis.engine_aggregate,
        driver_aggregate_ips=analysis.driver_aggregate,
        set_difference_pct=analysis.set_difference_pct,
        between_vcpu_span_pct=analysis.between_vcpu_span_pct,
        max_within_vcpu_spread_pct=analysis.max_within_vcpu_spread_pct,
        image=image,
    )
    return probe, analysis.reasons


def _daemon_ncpu() -> int:
    out = subprocess.run(
        ["docker", "info", "--format", "{{.NCPU}}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return int(out.stdout.strip())
