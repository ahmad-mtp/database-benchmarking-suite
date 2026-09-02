"""Merge determinism (PLAN.md S6 Accept: 100 shuffled merges, byte-identical)."""

from __future__ import annotations

import hashlib
import random
from pathlib import Path

import pytest

from dsel.live.merge import MergeError, find_shards, merge_records, merge_to_file, sort_key
from dsel.live.ndjson import ShardWriter
from dsel.live.schema import (
    ContainerRecord,
    LatencyWindowRecord,
    PhaseRecord,
    ValidityRecord,
)

WRITERS = 6
RECORDS_PER_WRITER = 250
MERGES = 100


def build_shards(directory: Path, seed: int = 7) -> None:
    """Write shards whose timestamps interleave and collide across writers.

    Colliding `t_ms` across writers is the case that matters: if the order were
    keyed on time alone the merge would be ambiguous, and a "deterministic"
    merge that never sees a tie has not been tested.
    """
    rng = random.Random(seed)
    base = 1_700_000_000_000
    for index in range(WRITERS):
        # Each writer advances forward in time, as a real sampler does, but at
        # its own cadence and with a coarse tick -- so writers land on the same
        # millisecond constantly. Ties across writers are the case that matters:
        # a merge keyed on time alone would be ambiguous, and a "deterministic"
        # merge that never sees a tie has not been tested.
        t_ms = base
        with ShardWriter(directory, f"w{index}") as writer:
            for n in range(RECORDS_PER_WRITER):
                t_ms += rng.choice((0, 0, 0, 100, 100, 200))
                choice = n % 4
                if choice == 0:
                    writer.write(
                        PhaseRecord(t_ms=t_ms, w="", seq=0, phase="measure", event="begin")
                    )
                elif choice == 1:
                    writer.write(
                        ContainerRecord(
                            t_ms=t_ms,
                            w="",
                            seq=0,
                            container="engine",
                            cpu_usage_usec=n,
                            memory_current=n * 1024,
                        )
                    )
                elif choice == 2:
                    writer.write(
                        LatencyWindowRecord(
                            t_ms=t_ms,
                            w="",
                            seq=0,
                            window_ms=1000,
                            op="read",
                            count=n,
                            rate_per_s=float(n),
                            p99_us=float(n) * 1.5,
                        )
                    )
                else:
                    writer.write(
                        ValidityRecord(
                            t_ms=t_ms,
                            w="",
                            seq=0,
                            gate="driver_cpu_pct",
                            verdict="OK",
                            observed=float(n),
                            limit=70.0,
                        )
                    )


def test_one_hundred_shuffled_merges_are_byte_identical(tmp_path: Path) -> None:
    """The acceptance criterion, run exactly as written."""
    shard_dir = tmp_path / "shards"
    build_shards(shard_dir)
    shards = find_shards(shard_dir)
    assert len(shards) == WRITERS

    rng = random.Random(99)
    digests: set[str] = set()
    for _ in range(MERGES):
        shuffled = shards[:]
        rng.shuffle(shuffled)
        data = "".join(
            __import__("dsel.live.ndjson", fromlist=["dumps"]).dumps(r) + "\n"
            for r in merge_records(shuffled)
        ).encode()
        digests.add(hashlib.sha256(data).hexdigest())

    assert len(digests) == 1, f"{len(digests)} distinct outputs across {MERGES} merges"


def test_merged_output_is_in_total_order(tmp_path: Path) -> None:
    shard_dir = tmp_path / "shards"
    build_shards(shard_dir)
    keys = [sort_key(r) for r in merge_records(find_shards(shard_dir))]
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys), "the sort key must be a total order, with no ties"


def test_every_record_survives_the_merge(tmp_path: Path) -> None:
    shard_dir = tmp_path / "shards"
    build_shards(shard_dir)
    out = tmp_path / "metrics.ndjson"
    count = merge_to_file(shard_dir, out)
    assert count == WRITERS * RECORDS_PER_WRITER
    assert len(out.read_text().splitlines()) == count


def test_timestamp_ties_across_writers_are_broken_by_writer_id(tmp_path: Path) -> None:
    """Two writers stamping the same millisecond must still order deterministically."""
    shard_dir = tmp_path / "shards"
    for name in ("wb", "wa"):
        with ShardWriter(shard_dir, name) as writer:
            for _ in range(3):
                writer.write(PhaseRecord(t_ms=1000, w="", seq=0, phase="load", event="begin"))
    records = list(merge_records(find_shards(shard_dir)))
    assert [(r.w, r.seq) for r in records] == [
        ("wa", 0),
        ("wa", 1),
        ("wa", 2),
        ("wb", 0),
        ("wb", 1),
        ("wb", 2),
    ]


def test_writer_stamps_its_own_id_and_sequence(tmp_path: Path) -> None:
    with ShardWriter(tmp_path, "sampler-3") as writer:
        first = writer.write(
            PhaseRecord(t_ms=1, w="ignored", seq=999, phase="init", event="begin")
        )
        second = writer.write(
            PhaseRecord(t_ms=2, w="ignored", seq=999, phase="init", event="end")
        )
    assert (first.w, first.seq) == ("sampler-3", 0)
    assert (second.w, second.seq) == ("sampler-3", 1)


def test_a_corrupt_line_fails_loudly(tmp_path: Path) -> None:
    """Never skip an unparseable line: the bundle hash would silently change."""
    shard_dir = tmp_path / "shards"
    with ShardWriter(shard_dir, "w0") as writer:
        writer.write(PhaseRecord(t_ms=1, w="", seq=0, phase="init", event="begin"))
    (shard_dir / "w0.ndjson").open("a").write("{not json}\n")
    with pytest.raises(MergeError, match="w0.ndjson:2"):
        list(merge_records(find_shards(shard_dir)))


def test_blank_lines_are_tolerated(tmp_path: Path) -> None:
    shard_dir = tmp_path / "shards"
    with ShardWriter(shard_dir, "w0") as writer:
        writer.write(PhaseRecord(t_ms=1, w="", seq=0, phase="init", event="begin"))
    (shard_dir / "w0.ndjson").open("a").write("\n\n")
    assert len(list(merge_records(find_shards(shard_dir)))) == 1


def test_a_shard_written_out_of_time_order_is_refused(tmp_path: Path) -> None:
    """An unsorted shard would merge deterministically but wrongly."""
    shard_dir = tmp_path / "shards"
    with ShardWriter(shard_dir, "w0") as writer:
        writer.write(PhaseRecord(t_ms=2000, w="", seq=0, phase="init", event="begin"))
        writer.write(PhaseRecord(t_ms=1000, w="", seq=0, phase="init", event="end"))
    with pytest.raises(MergeError, match="t_ms went backwards"):
        list(merge_records(find_shards(shard_dir)))


def test_two_writers_sharing_a_shard_are_refused(tmp_path: Path) -> None:
    """`seq` is per-writer; a repeated seq means the shard has two authors."""
    shard_dir = tmp_path / "shards"
    with ShardWriter(shard_dir, "w0") as writer:
        writer.write(PhaseRecord(t_ms=1000, w="", seq=0, phase="init", event="begin"))
    (shard_dir / "w0.ndjson").open("a").write(
        '{"cell":null,"event":"end","kind":"phase","ok":true,"phase":"init",'
        '"seq":0,"t_ms":1001,"w":"w0"}\n'
    )
    with pytest.raises(MergeError, match="seq did not advance"):
        list(merge_records(find_shards(shard_dir)))
