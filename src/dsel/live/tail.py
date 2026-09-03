"""Tailing a run's shards while they are still being written (PLAN.md S8a).

S8a's acceptance criterion is that the live screen and `--replay` finish in the
*same* state. That is a statement about ordering, not about arithmetic: the
reducer folds records one at a time, so if the live view applies them in a
different order from the merge it will land somewhere else and the criterion
fails. The naive tailer -- read whatever is new in each shard and apply it --
does exactly that, because a slow sampler's 5-second record arrives long after
the driver records it should have preceded.

So the tailer performs the *same* k-way merge the offline path does, over
sources that have not ended yet. The only extra machinery is a watermark. A
record may be released once no shard can still produce anything that would sort
before it:

    watermark = min over known shards of (last t_ms seen in that shard)

and a record is released only while `t_ms < watermark` (strict). Any record a
shard writes later has `t_ms >= watermark` by the per-shard monotonicity
`read_shard` already enforces, so it cannot sort before anything released. At
`close()` the run is over, the watermark becomes unbounded, and the remainder
drains in merged order.

The cost is honest and bounded: the screen lags the slowest sampler's interval.
That is the price of live and replay agreeing, and it is far cheaper than a
screen that is subtly wrong.

Two file-level realities are handled here rather than assumed away:

* **A trailing partial line is not a record.** A reader can arrive between a
  writer's `write` and its `flush`. A line with no terminating newline is left
  in place and re-read next tick; the byte offset only advances past complete
  lines.
* **A shard may appear after tailing starts.** That is normal -- samplers start
  at different times -- but a *late* shard whose first record predates the
  watermark would break the ordering guarantee, so it is refused loudly.
"""

from __future__ import annotations

import heapq
import json
from collections.abc import Iterator
from pathlib import Path

from dsel.live.merge import MergeError, SortKey, sort_key
from dsel.live.ndjson import SHARD_SUFFIX
from dsel.live.schema import RECORD_ADAPTER, AnyRecord


class _Shard:
    """One shard file being followed: a byte offset and a pending queue."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.writer_id: str | None = None
        self.last_t_ms: int | None = None
        self.last_seq: int | None = None
        self.pending: list[AnyRecord] = []

    def poll(self) -> int:
        """Parse whatever complete lines have appeared. Returns how many."""
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read()
        except FileNotFoundError:  # pragma: no cover - shard removed mid-run
            return 0
        if not chunk:
            return 0
        # Anything after the final newline is a half-written line. Leave it.
        cut = chunk.rfind(b"\n")
        if cut < 0:
            return 0
        complete, self.offset = chunk[: cut + 1], self.offset + cut + 1

        added = 0
        for lineno, raw in enumerate(complete.decode("utf-8").splitlines(), start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                record = RECORD_ADAPTER.validate_python(json.loads(stripped))
            except (json.JSONDecodeError, ValueError) as exc:
                raise MergeError(f"{self.path.name}:+{lineno}: {exc}") from exc
            if self.last_t_ms is not None and record.t_ms < self.last_t_ms:
                raise MergeError(
                    f"{self.path.name}:+{lineno}: t_ms went backwards "
                    f"({record.t_ms} after {self.last_t_ms}); a shard must be "
                    "written forward in time or the merge order is wrong"
                )
            if self.last_seq is not None and record.seq <= self.last_seq:
                raise MergeError(
                    f"{self.path.name}:+{lineno}: seq did not advance "
                    f"({record.seq} after {self.last_seq}); one writer per shard"
                )
            self.last_t_ms, self.last_seq = record.t_ms, record.seq
            self.writer_id = record.w
            self.pending.append(record)
            added += 1
        return added


class LiveTailer:
    """A k-way merge over shards that are still being appended to.

    Yields records in exactly the order `merge_records` would, at the cost of
    holding each one back until no shard can still undercut it.
    """

    def __init__(self, shard_dir: Path) -> None:
        self.shard_dir = shard_dir
        self._shards: dict[Path, _Shard] = {}
        self._released: SortKey | None = None
        self._count = 0
        self._ingested = 0
        self._closed = False

    @property
    def shard_count(self) -> int:
        return len(self._shards)

    @property
    def released_count(self) -> int:
        return self._count

    @property
    def ingested_count(self) -> int:
        """Lines parsed, released or still held. Drives the idle timer.

        The idle test must watch ingestion, not release: a held-back record is
        evidence the run is alive, and timing out on release alone would end a
        session while a slow sampler is mid-interval.
        """
        return self._ingested

    def _discover(self) -> None:
        for path in sorted(self.shard_dir.glob(f"*{SHARD_SUFFIX}")):
            if path not in self._shards:
                self._shards[path] = _Shard(path)

    def _watermark(self) -> int | None:
        """`None` means "release nothing": some shard has produced no record yet."""
        if not self._shards:
            return None
        marks = [shard.last_t_ms for shard in self._shards.values()]
        if any(mark is None for mark in marks):
            return None
        return min(mark for mark in marks if mark is not None)

    def poll(self) -> list[AnyRecord]:
        """Read new lines and return every record that is now safe to apply."""
        if self._closed:
            return []
        known = set(self._shards)
        self._discover()
        for path, shard in self._shards.items():
            fresh = path not in known
            self._ingested += shard.poll()
            if fresh and self._released is not None and shard.pending:
                first = sort_key(shard.pending[0])
                if first < self._released:
                    raise MergeError(
                        f"{path.name}: appeared late carrying {first}, which sorts "
                        f"before {self._released} already shown; the live view and "
                        "the merged file would disagree"
                    )
        return self._drain(self._watermark())

    def close(self) -> list[AnyRecord]:
        """The run is over: no shard can write again, so drain everything."""
        if self._closed:
            return []
        self._discover()
        for shard in self._shards.values():
            self._ingested += shard.poll()
        remainder = self._drain(None, unbounded=True)
        self._closed = True
        return remainder

    def _drain(self, watermark: int | None, *, unbounded: bool = False) -> list[AnyRecord]:
        if not unbounded and watermark is None:
            return []
        streams = [iter(shard.pending) for shard in self._shards.values() if shard.pending]
        if not streams:
            return []
        ordered = list(heapq.merge(*streams, key=sort_key))
        if unbounded:
            release, hold = ordered, []
        else:
            assert watermark is not None
            cut = 0
            while cut < len(ordered) and ordered[cut].t_ms < watermark:
                cut += 1
            release, hold = ordered[:cut], ordered[cut:]

        if release:
            self._released = sort_key(release[-1])
            self._count += len(release)
        held: dict[str, list[AnyRecord]] = {}
        for record in hold:
            held.setdefault(record.w, []).append(record)
        for shard in self._shards.values():
            # `w` is unique per shard, so each held record returns to its own queue.
            shard.pending = held.get(shard.writer_id or "", [])
        return release


def tail_records(shard_dir: Path) -> Iterator[AnyRecord]:
    """Drain a finished shard directory through the tailer. For tests."""
    tailer = LiveTailer(shard_dir)
    yield from tailer.poll()
    yield from tailer.close()
