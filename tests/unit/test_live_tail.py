"""The live tailer (PLAN.md S8a).

The live screen and `--replay` can only end in the same state if the live view
applies records in the same total order the offline merge does. That is what
`LiveTailer` exists for, and it is what these tests hold it to -- including the
awkward cases a happy-path test would never reach: a sparse writer pinning the
watermark, a half-written line, and a shard that turns up late.
"""

from __future__ import annotations

import pytest

from dsel.live.merge import MergeError, find_shards, merge_records
from dsel.live.ndjson import ShardWriter, dumps
from dsel.live.schema import AnyRecord, ContainerRecord, LatencyWindowRecord
from dsel.live.tail import LiveTailer, tail_records


def _window(t_ms: int, op: str = "read") -> LatencyWindowRecord:
    return LatencyWindowRecord(
        t_ms=t_ms, w="", seq=0, cell="c", window_ms=100, op=op, count=1, rate_per_s=10.0
    )


def _sample(t_ms: int) -> ContainerRecord:
    return ContainerRecord(t_ms=t_ms, w="", seq=0, cell="c", container="dsel-engine")


def _write(shard_dir, writer_id: str, times: list[int], kind: str = "window") -> None:
    with ShardWriter(shard_dir, writer_id) as writer:
        for t_ms in times:
            writer.write(_window(t_ms) if kind == "window" else _sample(t_ms))


def test_tailer_order_matches_the_offline_merge(tmp_path) -> None:
    """The whole point: two paths, one order."""
    shards = tmp_path / "shards"
    _write(shards, "driver-0", list(range(1000, 1200, 10)))
    _write(shards, "sampler-0", list(range(1005, 1200, 30)), kind="sample")
    _write(shards, "gates-0", [1000, 1190])

    tailed = list(tail_records(shards))
    merged = list(merge_records(find_shards(shards)))
    assert tailed == merged
    assert len(tailed) == 20 + 7 + 2


def test_a_sparse_writer_holds_records_back_until_close(tmp_path) -> None:
    """A slow sampler pins the watermark. Nothing past it may be shown early.

    This is the case that separates a correct tailer from one that reads
    whatever is new: the driver has written up to t=1190, but until the sparse
    gate writer speaks again nothing after t=1000 can be proven to be next.
    """
    shards = tmp_path / "shards"
    _write(shards, "driver-0", list(range(1000, 1200, 10)))
    _write(shards, "gates-0", [1000])

    tailer = LiveTailer(shards)
    released = tailer.poll()
    assert released == [], "watermark sits at 1000; nothing sorts strictly before it"
    assert tailer.ingested_count == 21, "held back, not unread"

    remainder = tailer.close()
    assert len(remainder) == 21
    assert remainder == list(merge_records(find_shards(shards)))


def test_a_half_written_line_is_not_a_record(tmp_path) -> None:
    """A reader can arrive between a write and its flush."""
    shards = tmp_path / "shards"
    shards.mkdir()
    complete = dumps(_window(1000).model_copy(update={"w": "driver-0", "seq": 0}))
    tail = dumps(_window(1010).model_copy(update={"w": "driver-0", "seq": 1}))
    path = shards / "driver-0.ndjson"
    path.write_text(complete + "\n" + tail[: len(tail) // 2], encoding="utf-8")

    tailer = LiveTailer(shards)
    seen = tailer.poll()
    assert tailer.ingested_count == 1, "the truncated line is not yet a record"

    path.write_text(complete + "\n" + tail + "\n", encoding="utf-8")
    seen += tailer.poll()
    assert tailer.ingested_count == 2, "and is picked up once it completes"
    seen += tailer.close()
    assert [r.t_ms for r in seen] == [1000, 1010], "no duplicate, no gap"


def test_a_shard_appearing_late_with_old_records_is_refused(tmp_path) -> None:
    """Silently accepting it would put the screen out of step with the file."""
    shards = tmp_path / "shards"
    _write(shards, "a-0", [100, 200, 300])
    _write(shards, "b-0", [100, 200, 300])

    tailer = LiveTailer(shards)
    assert [r.t_ms for r in tailer.poll()] == [100, 100, 200, 200]

    _write(shards, "c-0", [150])
    with pytest.raises(MergeError, match="appeared late"):
        tailer.poll()


def test_a_shard_appearing_late_with_new_records_is_fine(tmp_path) -> None:
    """Samplers legitimately start at different times."""
    shards = tmp_path / "shards"
    _write(shards, "a-0", [100, 200, 300])
    _write(shards, "b-0", [100, 200, 300])

    tailer = LiveTailer(shards)
    tailer.poll()
    _write(shards, "c-0", [400])
    tailer.poll()
    assert tailer.close()
    assert tailer.released_count == 7


def test_out_of_order_within_one_shard_is_refused(tmp_path) -> None:
    """Same precondition the offline reader enforces, same loud failure."""
    shards = tmp_path / "shards"
    _write(shards, "driver-0", [1000, 900])
    with pytest.raises(MergeError, match="t_ms went backwards"):
        list(tail_records(shards))


def test_nothing_is_lost_across_many_polls(tmp_path) -> None:
    """Incremental reading must be conservative, not lossy."""
    shards = tmp_path / "shards"
    tailer = LiveTailer(shards)
    collected: list[AnyRecord] = []
    with ShardWriter(shards, "driver-0") as driver, ShardWriter(shards, "gates-0") as gates:
        for t_ms in range(1000, 1100, 10):
            driver.write(_window(t_ms))
            if t_ms % 40 == 0:
                gates.write(_sample(t_ms))
            collected.extend(tailer.poll())
    collected.extend(tailer.close())
    assert collected == list(merge_records(find_shards(shards)))
