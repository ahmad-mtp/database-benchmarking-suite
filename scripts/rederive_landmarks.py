#!/usr/bin/env python3
"""Re-derive a load curve's landmarks from `metrics.ndjson` alone (PLAN.md S15).

    python scripts/rederive_landmarks.py runs/<run-id>/metrics.ndjson

Prints the curve and its knee, collapse and max sustainable rate as JSON.

This is the script S15's acceptance criterion asks for. It takes a file and
nothing else: no run directory, no Docker daemon, no engine, no state left over
from the run that produced it. If it cannot recover the same landmarks the run
reported, then those landmarks were not in the evidence -- they were in the
process's memory, and nobody outside that process could ever check them.

It deliberately imports the *rules* rather than restating them. A second copy
of "what a knee is" would drift from the first, and agreement between two
drifting definitions proves nothing. What is independent here is the data path,
which is the thing that has to be.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from dsel.live.merge import find_shards, merge_records, read_merged  # noqa: E402
from dsel.phenomena.conn_cliff import curve_from_records  # noqa: E402


def load(path: Path) -> list:
    """Records from a merged metrics file, or from a run's shard directory."""
    if path.is_dir():
        shards = find_shards(path / "shards" if (path / "shards").is_dir() else path)
        if not shards:
            raise SystemExit(f"no shards under {path}")
        return list(merge_records(shards))
    return list(read_merged(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("metrics", type=Path, help="metrics.ndjson, or a run directory")
    parser.add_argument("--table", action="store_true", help="also print the curve")
    args = parser.parse_args(argv)

    curve = curve_from_records(load(args.metrics))
    if not curve.points:
        raise SystemExit(f"{args.metrics} holds no latency_window records with a cell id")

    if args.table:
        print(f"{'offered':>9} {'achieved':>9} {'p99 us':>10} {'errors':>8}  verdict")
        for point in curve.points:
            print(
                f"{point.offered_rate_per_s:>9.0f} {point.achieved_rate_per_s:>9.0f} "
                f"{point.p99_us:>10.0f} {point.errors:>8}  {point.verdict}"
            )
    json.dump(
        {
            "source": str(args.metrics),
            "steps": len(curve.points),
            **curve.landmarks(),
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
