"""Deterministic shard merge (PLAN.md S6).

Acceptance criterion: 100 shuffled shard merges produce a byte-identical file.

That holds because `(t_ms, w, seq)` is a total order over every record the run
produces -- `w` is unique per writer and `seq` is unique within a writer, so no
two records can tie on all three. Sorting on it therefore has exactly one
answer regardless of the order shards are read, and regardless of the order
lines were physically written.

Merging is streaming: shards are consumed by a heap rather than read whole, so
a long soak's metrics file does not have to fit in memory.
"""

from __future__ import annotations

import heapq
import json
from collections.abc import Iterable, Iterator
from pathlib import Path

from dsel.live.ndjson import SHARD_SUFFIX, dumps
from dsel.live.schema import RECORD_ADAPTER, AnyRecord

SortKey = tuple[int, str, int]


class MergeError(RuntimeError):
    """A shard could not be merged. Never skip a bad line silently."""


def sort_key(record: AnyRecord) -> SortKey:
    """The total order. `w` is unique per writer, `seq` unique within it."""
    return (record.t_ms, record.w, record.seq)


def read_shard(path: Path) -> Iterator[AnyRecord]:
    """Parse one shard, validating every line and its ordering.

    The merge is a k-way heap merge, which is only correct if each shard is
    already sorted by the same key. A sampler produces that naturally -- it
    writes forward in time with an increasing `seq` -- but relying on an
    unstated precondition means an out-of-order shard silently yields a
    wrongly-ordered file that is still perfectly *deterministic*, and so still
    passes a byte-identity check. The precondition is therefore enforced here
    rather than assumed.
    """
    previous: tuple[int, int] | None = None
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = RECORD_ADAPTER.validate_python(json.loads(stripped))
            except (json.JSONDecodeError, ValueError) as exc:
                raise MergeError(f"{path.name}:{lineno}: {exc}") from exc
            if previous is not None:
                last_t, last_seq = previous
                if record.t_ms < last_t:
                    raise MergeError(
                        f"{path.name}:{lineno}: t_ms went backwards "
                        f"({record.t_ms} after {last_t}); a shard must be written "
                        "forward in time or the merge order is wrong"
                    )
                if record.seq <= last_seq:
                    raise MergeError(
                        f"{path.name}:{lineno}: seq did not advance "
                        f"({record.seq} after {last_seq}); one writer per shard"
                    )
            previous = (record.t_ms, record.seq)
            yield record


def read_merged(path: Path) -> Iterator[AnyRecord]:
    """Parse a merged metrics file, enforcing the *total* order.

    A merged file interleaves writers, so `seq` repeats across lines by design
    and `read_shard`'s per-writer rule would reject every real merge. What must
    hold instead is that the full `(t_ms, w, seq)` triple strictly increases --
    the same order the audit bundle's hash is taken over. Replaying a file that
    does not satisfy it would put the screen out of step with the record the
    bundle carries, so it is checked rather than assumed.
    """
    previous: SortKey | None = None
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = RECORD_ADAPTER.validate_python(json.loads(stripped))
            except (json.JSONDecodeError, ValueError) as exc:
                raise MergeError(f"{path.name}:{lineno}: {exc}") from exc
            key = sort_key(record)
            if previous is not None and key <= previous:
                raise MergeError(
                    f"{path.name}:{lineno}: {key} does not follow {previous}; a "
                    "merged file must be in (t_ms, w, seq) order"
                )
            previous = key
            yield record


def find_shards(directory: Path) -> list[Path]:
    """Every shard in the directory, in a stable order.

    The order does not affect the result -- that is the point -- but a stable
    listing keeps failures reproducible.
    """
    return sorted(directory.glob(f"*{SHARD_SUFFIX}"))


def merge_records(shards: Iterable[Path]) -> Iterator[AnyRecord]:
    """Stream records from all shards in total order.

    Each shard is ordered within itself (checked by `read_shard`); the heap
    interleaves them on the full triple.
    """
    streams = [read_shard(path) for path in shards]
    yield from heapq.merge(*streams, key=sort_key)


def merge_to_file(shard_dir: Path, output: Path) -> int:
    """Merge every shard into one file. Returns the record count."""
    count = 0
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for record in merge_records(find_shards(shard_dir)):
            handle.write(dumps(record) + "\n")
            count += 1
    return count
