"""cgroup v2 readers (PLAN.md S7).

Read from inside the container, because that is what a well-behaved engine
reads. findings.md 6.3: five APIs gave three answers in exp08, so these values
are recorded alongside `docker inspect` rather than instead of it, and the two
are compared rather than merged.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

CGROUP_ROOT = "/sys/fs/cgroup"

# One exec per sample, not one per file: at 1 Hz across several containers the
# exec overhead is the thing being measured otherwise.
_READ_SCRIPT = r"""
for f in cpu.stat cpu.max cpuset.cpus.effective memory.max memory.current \
         memory.events pids.current pids.max io.stat; do
  if [ -r /sys/fs/cgroup/$f ]; then
    echo "@@$f"
    cat /sys/fs/cgroup/$f
  fi
done
"""


class CgroupError(RuntimeError):
    """The cgroup could not be read."""


@dataclass(frozen=True, slots=True)
class CgroupSample:
    """One reading of a container's cgroup."""

    cpu_usage_usec: int | None = None
    cpu_user_usec: int | None = None
    cpu_system_usec: int | None = None
    cpu_nr_throttled: int | None = None
    cpu_throttled_usec: int | None = None
    cpu_max: str | None = None
    cpuset_effective: str | None = None
    memory_max: int | None = None
    memory_current: int | None = None
    memory_oom: int | None = None
    memory_oom_kill: int | None = None
    pids_current: int | None = None
    pids_max: int | None = None
    io_read_bytes: int | None = None
    io_write_bytes: int | None = None

    @property
    def throttled_fraction(self) -> float | None:
        """`throttled_usec` as a fraction of CPU time used.

        PLAN.md adds a `cpu_throttling` gate at 5% of the window: surfaced and
        flagged, not INVALID.
        """
        if self.cpu_throttled_usec is None or not self.cpu_usage_usec:
            return None
        return self.cpu_throttled_usec / self.cpu_usage_usec


def _as_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def parse(raw: str) -> CgroupSample:
    """Parse the batched read into a sample."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in raw.splitlines():
        if line.startswith("@@"):
            current = line[2:]
            sections[current] = []
        elif current is not None:
            sections[current].append(line)

    def kv(section: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for line in sections.get(section, []):
            parts = line.split()
            if len(parts) == 2 and (value := _as_int(parts[1])) is not None:
                out[parts[0]] = value
        return out

    def single(section: str) -> str | None:
        lines = sections.get(section, [])
        return lines[0].strip() if lines else None

    cpu = kv("cpu.stat")
    mem_events = kv("memory.events")

    read_bytes = write_bytes = 0
    seen_io = False
    for line in sections.get("io.stat", []):
        for field in line.split()[1:]:
            key, _, value = field.partition("=")
            parsed = _as_int(value)
            if parsed is None:
                continue
            if key == "rbytes":
                read_bytes += parsed
                seen_io = True
            elif key == "wbytes":
                write_bytes += parsed
                seen_io = True

    memory_max_raw = single("memory.max")
    pids_max_raw = single("pids.max")
    return CgroupSample(
        cpu_usage_usec=cpu.get("usage_usec"),
        cpu_user_usec=cpu.get("user_usec"),
        cpu_system_usec=cpu.get("system_usec"),
        cpu_nr_throttled=cpu.get("nr_throttled"),
        cpu_throttled_usec=cpu.get("throttled_usec"),
        cpu_max=single("cpu.max"),
        cpuset_effective=single("cpuset.cpus.effective"),
        # "max" means unlimited; recorded as None rather than a fake number.
        memory_max=None if memory_max_raw in (None, "max") else _as_int(memory_max_raw or ""),
        memory_current=_as_int(single("memory.current") or ""),
        memory_oom=mem_events.get("oom"),
        memory_oom_kill=mem_events.get("oom_kill"),
        pids_current=_as_int(single("pids.current") or ""),
        pids_max=None if pids_max_raw in (None, "max") else _as_int(pids_max_raw or ""),
        io_read_bytes=read_bytes if seen_io else None,
        io_write_bytes=write_bytes if seen_io else None,
    )


def read(container: str, timeout: float = 15.0) -> CgroupSample:
    """Read a container's cgroup in one exec."""
    # `-i` is load-bearing: without it `docker exec` does not attach stdin and
    # the script silently never reaches the shell, yielding an empty sample.
    result = subprocess.run(
        ["docker", "exec", "-i", container, "sh", "-s"],
        input=_READ_SCRIPT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise CgroupError(f"cgroup read failed for {container}: {result.stderr.strip()}")
    return parse(result.stdout)
