"""Container sampler (PLAN.md S7).

Cadence: 1 Hz. Emits one `container` record per container per tick, reading the
cgroup from inside. Writes records; derives nothing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence

from dsel.live.ndjson import ShardWriter, now_ms
from dsel.live.schema import ContainerRecord
from dsel.runtime.cgroup import CgroupError, CgroupSample
from dsel.runtime.cgroup import read as read_cgroup

DEFAULT_INTERVAL_S = 1.0


def to_record(container: str, sample: CgroupSample, cell: str | None = None) -> ContainerRecord:
    """Map a cgroup reading onto a record. No interpretation."""
    return ContainerRecord(
        t_ms=now_ms(),
        w="",  # stamped by the writer
        seq=0,
        cell=cell,
        container=container,
        cpu_usage_usec=sample.cpu_usage_usec,
        cpu_throttled_usec=sample.cpu_throttled_usec,
        cpu_nr_throttled=sample.cpu_nr_throttled,
        memory_current=sample.memory_current,
        memory_max=sample.memory_max,
        memory_events_oom=sample.memory_oom,
        memory_events_oom_kill=sample.memory_oom_kill,
        pids_current=sample.pids_current,
        blkio_read_bytes=sample.io_read_bytes,
        blkio_write_bytes=sample.io_write_bytes,
        blkio_trusted=True,  # named volumes only; exp04 blinded bind mounts
    )


class ContainerSampler:
    """Polls containers on a fixed cadence, writing to its own shard."""

    def __init__(
        self,
        writer: ShardWriter,
        containers: Sequence[str],
        interval_s: float = DEFAULT_INTERVAL_S,
        cell: str | None = None,
    ) -> None:
        self.writer = writer
        self.containers = list(containers)
        self.interval_s = interval_s
        self.cell = cell
        self.errors = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sample_once(self) -> int:
        """One tick across every container. Returns records written."""
        written = 0
        for container in self.containers:
            try:
                sample = read_cgroup(container)
            except (CgroupError, OSError):
                self.errors += 1
                continue
            self.writer.write(to_record(container, sample, self.cell))
            written += 1
        return written

    def _loop(self) -> None:
        # Absolute deadlines, so a slow read does not make the cadence drift.
        next_at = time.monotonic()
        while not self._stop.is_set():
            self.sample_once()
            next_at += self.interval_s
            self._stop.wait(max(0.0, next_at - time.monotonic()))

    def start(self) -> ContainerSampler:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="dsel-containers")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_s * 3 + 5)
        self.writer.flush()

    def __enter__(self) -> ContainerSampler:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
