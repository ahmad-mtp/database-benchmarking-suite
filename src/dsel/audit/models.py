"""Manifest and environment models.

Pydantic rather than dataclasses because these serialise into the audit bundle
and are re-read by a third party (findings.md 8.6), so the schema is the
contract and must round-trip exactly.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Frozen(BaseModel):
    """Base: immutable, no undeclared fields reaching the bundle."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class HostCapture(Frozen):
    """The macOS host. Not where measurement happens, but it schedules the VM."""

    os_name: str
    os_version: str
    arch: str
    cpu_brand: str | None = None
    logical_cpus: int | None = None
    physical_cpus: int | None = None
    performance_cores: int | None = None
    efficiency_cores: int | None = None
    memory_bytes: int | None = None


class DaemonCapture(Frozen):
    """What the Docker daemon reports about itself."""

    server_version: str
    kernel_version: str
    operating_system: str
    storage_driver: str
    ncpu: int
    mem_total_bytes: int
    cgroup_version: str | None = None
    cgroup_driver: str | None = None
    compose_version: str | None = None


class VmCapture(Frozen):
    """Read from inside a container: the kernel the engine will actually see."""

    kernel_release: str
    cgroup_controllers: list[str] = Field(default_factory=list)
    transparent_hugepage: str | None = None
    swappiness: int | None = None
    numa_nodes: int | None = None
    shm_size_bytes: int | None = None
    cpuinfo_processors: int | None = None
    rotational: dict[str, str] = Field(default_factory=dict)


class ImagePin(Frozen):
    """findings.md: pin the index digest, record the resolved platform digest."""

    reference: str
    index_digest: str
    platform_digest: str
    platform: str

    @property
    def pinned(self) -> str:
        """`repo@sha256:...` -- what any `docker run` should actually be given.

        The *index* digest, never the platform one: a platform digest is
        architecture-locked and the same spec has to run on a dev Mac and on
        amd64 CI. Built here so no caller reconstructs it by hand and gets the
        tag by accident.
        """
        return f"{self.reference.split(':')[0]}@{self.index_digest}"


class AppCeilingRecord(Frozen):
    """The app tier's measured limit (PLAN.md S13).

    In the manifest because S14 schedules PATH B against it. A ceiling that
    lived only in the run that measured it would have to be re-measured or
    guessed, and guessing is how a tier-saturated result gets published as a
    database comparison.
    """

    noop_saturation_rate_per_s: float | None
    """Where `/noop` crossed the CPU gate. `None` means it was never reached --
    which is not the same as "no limit", and must not be read as one."""
    noop_max_delivered_rate_per_s: float
    """The highest rate served. Measured past the gate, so useless for planning."""
    cpu_limit_pct: float
    path_b_rate_per_s: float | None
    ceiling_fraction: float
    app_cpus: float
    """The quota, which is the denominator of `app_tier_cpu_pct`."""
    app_workers: int


class VcpuSpeed(Frozen):
    """One vCPU's measured speed, with the noise that qualifies it."""

    vcpu: int
    median_ips: float
    min_ips: float
    max_ips: float
    samples: int

    @property
    def spread_pct(self) -> float:
        """Within-vCPU spread as a percentage of the median."""
        if self.median_ips == 0:
            return 0.0
        return (self.max_ips - self.min_ips) / self.median_ips * 100.0


class VcpuProbe(Frozen):
    """The vCPU speed probe result (PLAN.md S1)."""

    work_ms: int
    repeats: int
    discarded_warmup: int
    per_vcpu: list[VcpuSpeed]
    relative_speed: list[float]
    engine_cpuset: list[int]
    driver_cpuset: list[int]
    engine_aggregate_ips: float
    driver_aggregate_ips: float
    set_difference_pct: float
    between_vcpu_span_pct: float
    max_within_vcpu_spread_pct: float
    image: ImagePin


class InterferenceLevel(Frozen):
    """Capacity retained by one cpuset at a given contention level on another."""

    contention_workers: int
    retained_median: float
    retained_ci_low: float
    retained_ci_high: float
    absolute_median_ops_per_s: float
    blocks: int


class InterferenceSweepRecord(Frozen):
    """One direction of the cross-cpuset interference sweep."""

    measured_cpuset: str
    contended_cpuset: str
    window_s: float
    blocks: int
    seed: int
    randomised_block_order: bool = True
    levels: list[InterferenceLevel]
    isolation_holds_within_10pct: bool


class Manifest(Frozen):
    """The run manifest. S1 populates the environment and probe blocks only."""

    schema_version: int = 1
    captured_at: datetime
    profile: str = "local"
    reportable: bool = False
    envelope_deviation: bool = True
    envelope_deviation_reasons: list[str] = Field(default_factory=list)
    harness_version: str
    harness_commit: str | None = None
    harness_dirty: bool = False
    host: HostCapture
    daemon: DaemonCapture
    vm: VmCapture
    vcpu_probe: VcpuProbe | None = None
    cpuset_interference: list[InterferenceSweepRecord] = Field(default_factory=list)
    app_ceiling: AppCeilingRecord | None = None
    ab_delta_valid: bool = False
    """Whether the PATH A / PATH B difference may be reported as the app
    tier's cost.

    **False on every local run, and not a placeholder.** The delta is a
    subtraction of two measurements taken on a machine where the driver, the
    app tier, the engine and the observability stack share ten cores that
    `cpuset` does not actually isolate -- S1 measured 20-30% cross-cpuset
    interference. Each side of the subtraction carries that interference, and
    the difference between two contaminated numbers is not a clean measure of
    anything. The *shape* transfers; the number does not.
    """
