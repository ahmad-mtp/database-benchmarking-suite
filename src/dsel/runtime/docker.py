"""Container provisioning, health gating and envelope readback (PLAN.md S3-S5).

The lifecycle is provision -> health gate -> readback -> (measure) -> teardown.
Readback happens before anything is measured: a container whose knobs do not
match its envelope is torn down, not measured with a caveat.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from dataclasses import dataclass

from dsel.audit.models import ImagePin
from dsel.runtime.envelope import Readback, ResourceEnvelope
from dsel.runtime.storage import VolumeMount, check_fstype
from dsel.runtime.teardown import label_flags

HEALTH_TIMEOUT_S = 120.0
HEALTH_INTERVAL_S = 0.5


class ProvisionError(RuntimeError):
    """The container could not be started, or never became healthy."""


def _docker(args: list[str], timeout: float = 300.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _exec(container: str, command: list[str], timeout: float = 60.0) -> str:
    result = _docker(["exec", container, *command], timeout=timeout)
    if result.returncode != 0:
        raise ProvisionError(f"exec {' '.join(command)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _parse_cpuset(spec: str) -> tuple[int, ...]:
    """Expand "2-5,8" into (2, 3, 4, 5, 8)."""
    cpus: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            cpus.extend(range(int(lo), int(hi) + 1))
        else:
            cpus.append(int(part))
    return tuple(sorted(cpus))


@dataclass(frozen=True, slots=True)
class Container:
    """A provisioned container, with everything needed to verify and remove it."""

    name: str
    container_id: str
    image: ImagePin
    envelope: ResourceEnvelope
    mount: VolumeMount
    run_id: str
    host_port: int


def provision(
    image: ImagePin,
    envelope: ResourceEnvelope,
    run_id: str,
    *,
    data_dir: str,
    container_port: int,
    host_port: int,
    env: dict[str, str] | None = None,
    command: list[str] | None = None,
    name: str | None = None,
) -> Container:
    """Create the named volume and start the container with every knob set."""
    container_name = name or f"dsel-{run_id}-{uuid.uuid4().hex[:6]}"
    volume_name = f"{container_name}-data"

    created = _docker(["volume", "create", *label_flags(run_id), volume_name])
    if created.returncode != 0:
        raise ProvisionError(f"volume create failed: {created.stderr.strip()}")
    mount = VolumeMount(volume=volume_name, target=data_dir)

    args = [
        "run",
        "--detach",
        "--name",
        container_name,
        *label_flags(run_id),
        *envelope.docker_flags(),
        *mount.docker_flags(),
        "--publish",
        f"{host_port}:{container_port}",
    ]
    for key, value in (env or {}).items():
        args += ["--env", f"{key}={value}"]
    args.append(image.pinned)
    args += command or []

    started = _docker(args)
    if started.returncode != 0:
        raise ProvisionError(f"docker run failed: {started.stderr.strip()}")

    return Container(
        name=container_name,
        container_id=started.stdout.strip(),
        image=image,
        envelope=envelope,
        mount=mount,
        run_id=run_id,
        host_port=host_port,
    )


def wait_healthy(
    container: Container,
    probe: list[str],
    timeout_s: float = HEALTH_TIMEOUT_S,
    interval_s: float = HEALTH_INTERVAL_S,
) -> float:
    """Poll `probe` inside the container until it succeeds. Returns seconds waited.

    The gate is the engine's own readiness command, not a TCP connect: an
    engine can accept a socket well before it will answer a query.
    """
    deadline = time.monotonic() + timeout_s
    started = time.monotonic()
    last = ""
    while time.monotonic() < deadline:
        alive = _docker(["inspect", "--format", "{{.State.Running}}", container.name])
        if alive.stdout.strip() != "true":
            logs = _docker(["logs", "--tail", "20", container.name])
            raise ProvisionError(
                f"{container.name} exited before becoming healthy:\n{logs.stdout}{logs.stderr}"
            )
        result = _docker(["exec", container.name, *probe], timeout=30.0)
        if result.returncode == 0:
            return time.monotonic() - started
        last = (result.stderr or result.stdout).strip()
        time.sleep(interval_s)
    raise ProvisionError(f"{container.name} never became healthy in {timeout_s}s: {last}")


def read_back(container: Container) -> Readback:
    """Read every knob from the running container, two independent ways."""
    cpuset_effective = _parse_cpuset(
        _exec(container.name, ["cat", "/sys/fs/cgroup/cpuset.cpus.effective"])
    )
    cpu_max = _exec(container.name, ["cat", "/sys/fs/cgroup/cpu.max"])
    memory_max = int(_exec(container.name, ["cat", "/sys/fs/cgroup/memory.max"]))
    pids_max_raw = _exec(container.name, ["cat", "/sys/fs/cgroup/pids.max"])
    pids_max = -1 if pids_max_raw == "max" else int(pids_max_raw)

    inspected = _docker(["inspect", "--format", "{{json .HostConfig}}", container.name])
    if inspected.returncode != 0:
        raise ProvisionError(f"inspect failed: {inspected.stderr.strip()}")
    host = json.loads(inspected.stdout)

    nproc: int | None = None
    result = _docker(["exec", container.name, "nproc"], timeout=30.0)
    if result.returncode == 0:
        nproc = int(result.stdout.strip())

    return Readback(
        cpuset_effective=cpuset_effective,
        cpu_max=cpu_max,
        memory_max=memory_max,
        pids_max=pids_max,
        host_cpuset=host.get("CpusetCpus", ""),
        host_nano_cpus=int(host.get("NanoCpus", 0)),
        host_memory=int(host.get("Memory", 0)),
        host_memory_swap=int(host.get("MemorySwap", 0)),
        nproc_visible=nproc,
    )


def verify_storage(container: Container) -> str:
    """Assert the data directory sits on a named volume's filesystem."""
    fstype = _exec(container.name, ["stat", "-f", "-c", "%T", container.mount.target])
    check_fstype(fstype, container.mount.target)
    return fstype
