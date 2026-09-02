"""Sharded NDJSON writers (PLAN.md S6).

Each writer owns one shard file and one monotonic `seq`. Writers never
coordinate: a shared file with a lock would put the samplers' contention inside
the measurement window, which is the thing being measured.

Ordering is recovered at merge time from `(t_ms, w, seq)`, so the shards may be
written in any order and merged in any order and still produce one byte for
byte identical file.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import TracebackType

from dsel.live.schema import AnyRecord, Record

SHARD_SUFFIX = ".ndjson"


def now_ms() -> int:
    """Sample time in milliseconds since the epoch."""
    return time.time_ns() // 1_000_000


def dumps(record: Record) -> str:
    """Serialise one record to its canonical single line.

    Keys are sorted and separators fixed so the same record always produces the
    same bytes -- the merge output is hashed into the audit bundle.
    """
    payload = record.model_dump(mode="json", exclude_none=True)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class ShardWriter:
    """Appends records to one shard, stamping `w` and `seq`.

    Not thread-safe by design: one writer per thread or process, which is what
    keeps `seq` monotonic without a lock.
    """

    def __init__(self, directory: Path, writer_id: str, flush_every: int = 1) -> None:
        self.writer_id = writer_id
        self.path = directory / f"{writer_id}{SHARD_SUFFIX}"
        self._seq = 0
        self._since_flush = 0
        self._flush_every = flush_every
        directory.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")

    @property
    def seq(self) -> int:
        return self._seq

    def write(self, record: AnyRecord) -> AnyRecord:
        """Stamp the record with this writer's id and next seq, then append."""
        stamped = record.model_copy(update={"w": self.writer_id, "seq": self._seq})
        self._seq += 1
        self._handle.write(dumps(stamped) + "\n")
        self._since_flush += 1
        if self._since_flush >= self._flush_every:
            self.flush()
        return stamped

    def flush(self) -> None:
        self._handle.flush()
        self._since_flush = 0

    def close(self) -> None:
        if not self._handle.closed:
            self.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()

    def __enter__(self) -> ShardWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
