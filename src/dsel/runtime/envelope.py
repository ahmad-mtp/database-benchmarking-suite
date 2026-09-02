"""Resource envelope and readback verification (PLAN.md S3-S5).

findings.md 6.3: no Docker flag combination makes CPU/memory detection agree
across APIs -- five APIs gave three answers in exp08, and ClickHouse reads the
cgroup while MongoDB reads /proc/cpuinfo on the same host with the same flags.
The response is not to find the right flag but to set every knob explicitly and
then read it back from the running container, refusing to measure if they
disagree.

Both `--cpuset-cpus` and `--cpus` are always set: quota alone leaves the
container seeing every host core.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

GIB = 1024**3
CPU_PERIOD_US = 100_000

# ResourceEnvelope.storage has exactly one legal value. Bind mounts raised
# run-to-run variance from 1.3% to 18% and blinded docker stats BlockIO
# (8.19 kB reported for 6.44 GB of writes) in exp04/exp05.
StorageKind = Literal["named_volume"]


class ReadbackError(RuntimeError):
    """The running container does not match the envelope it was given."""


@dataclass(frozen=True, slots=True)
class ResourceEnvelope:
    """What a container is allowed, stated explicitly and verified afterwards."""

    cpuset: tuple[int, ...]
    cpus: float
    memory_bytes: int
    pids_limit: int = 4096
    storage: StorageKind = "named_volume"
    # Swap is pinned equal to memory, which disables it. A container allowed to
    # swap produces latency numbers describing the host's page cache.
    swap_equals_memory: bool = True

    @property
    def cpuset_spec(self) -> str:
        """The `--cpuset-cpus` argument, e.g. "2-5" or "0,2,4"."""
        sorted_cpus = sorted(self.cpuset)
        runs: list[tuple[int, int]] = []
        for cpu in sorted_cpus:
            if runs and cpu == runs[-1][1] + 1:
                runs[-1] = (runs[-1][0], cpu)
            else:
                runs.append((cpu, cpu))
        return ",".join(f"{lo}-{hi}" if lo != hi else str(lo) for lo, hi in runs)

    @property
    def memory_swap_bytes(self) -> int:
        return self.memory_bytes if self.swap_equals_memory else -1

    def docker_flags(self) -> list[str]:
        """Every knob, always set. Never rely on a default."""
        return [
            "--cpuset-cpus",
            self.cpuset_spec,
            "--cpus",
            f"{self.cpus:g}",
            "--memory",
            str(self.memory_bytes),
            "--memory-swap",
            str(self.memory_swap_bytes),
            "--pids-limit",
            str(self.pids_limit),
        ]

    # --- readback ----------------------------------------------------------

    def expected_cpu_max(self) -> str:
        """cgroup v2 `cpu.max` as the kernel will render it."""
        return f"{int(self.cpus * CPU_PERIOD_US)} {CPU_PERIOD_US}"

    def verify(self, readback: Readback) -> None:
        """Refuse the run if any knob disagrees. Never warn and continue."""
        problems: list[str] = []
        if tuple(sorted(readback.cpuset_effective)) != tuple(sorted(self.cpuset)):
            problems.append(
                f"cpuset: envelope {sorted(self.cpuset)} != "
                f"cgroup cpuset.cpus.effective {sorted(readback.cpuset_effective)}"
            )
        if readback.cpu_max != self.expected_cpu_max():
            problems.append(
                f"cpu quota: envelope {self.expected_cpu_max()!r} != "
                f"cgroup cpu.max {readback.cpu_max!r}"
            )
        if readback.memory_max != self.memory_bytes:
            problems.append(
                f"memory: envelope {self.memory_bytes} != "
                f"cgroup memory.max {readback.memory_max}"
            )
        if readback.pids_max != self.pids_limit:
            problems.append(
                f"pids: envelope {self.pids_limit} != cgroup pids.max {readback.pids_max}"
            )
        if readback.host_cpuset != self.cpuset_spec:
            problems.append(
                f"cpuset: envelope {self.cpuset_spec!r} != "
                f"HostConfig.CpusetCpus {readback.host_cpuset!r}"
            )
        if readback.host_nano_cpus != int(self.cpus * 1e9):
            problems.append(
                f"cpus: envelope {int(self.cpus * 1e9)} != "
                f"HostConfig.NanoCpus {readback.host_nano_cpus}"
            )
        if readback.host_memory != self.memory_bytes:
            problems.append(
                f"memory: envelope {self.memory_bytes} != "
                f"HostConfig.Memory {readback.host_memory}"
            )
        if readback.host_memory_swap != self.memory_swap_bytes:
            problems.append(
                f"memory-swap: envelope {self.memory_swap_bytes} != "
                f"HostConfig.MemorySwap {readback.host_memory_swap}"
            )
        if problems:
            raise ReadbackError(
                "the running container does not match its envelope:\n  - "
                + "\n  - ".join(problems)
            )


@dataclass(frozen=True, slots=True)
class Readback:
    """What the running container says about itself.

    Two independent sources: the cgroup, which is what a well-behaved engine
    reads, and `docker inspect .HostConfig`, which is what was asked for. They
    are compared separately because exp08 showed them capable of disagreeing.
    """

    cpuset_effective: tuple[int, ...]
    cpu_max: str
    memory_max: int
    pids_max: int
    host_cpuset: str
    host_nano_cpus: int
    host_memory: int
    host_memory_swap: int
    nproc_visible: int | None = None
