#!/usr/bin/env python3
"""Fit per-backend RSS growth from a soak's `metrics.ndjson` (S16-S18b).

    python scripts/analyse_backend_growth.py runs/<run-id>/metrics.ndjson

Produces the pooled slope and its bootstrap confidence interval. Like
`rederive_landmarks.py` it takes a file and nothing else: no daemon, no engine,
no run in memory. A memory-growth claim that could only be made by the process
that observed it is not a claim anybody can check.
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
from dsel.phenomena.backend_growth import (  # noqa: E402
    BOOTSTRAP_RESAMPLES,
    DEFAULT_SEED,
    growth_from_records,
)


def load(path: Path) -> list:
    if path.is_dir():
        shards = find_shards(path / "shards" if (path / "shards").is_dir() else path)
        if not shards:
            raise SystemExit(f"no shards under {path}")
        return list(merge_records(shards))
    return list(read_merged(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("metrics", type=Path, help="metrics.ndjson, or a run directory")
    parser.add_argument("--resamples", type=int, default=BOOTSTRAP_RESAMPLES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--table", action="store_true")
    args = parser.parse_args(argv)

    result = growth_from_records(load(args.metrics), resamples=args.resamples, seed=args.seed)
    if not result.fits:
        raise SystemExit(f"{args.metrics} holds no backend records carrying VmRSS")
    if args.table:
        print(result.table())
        print()
    json.dump(
        {
            "source": str(args.metrics),
            "backends_seen": len(result.fits),
            "backends_fitted": len(result.usable_fits),
            "median_slope_bytes_per_s": result.median_slope_bytes_per_s,
            "median_slope_mib_per_hour": result.median_slope_mib_per_hour,
            "ci_low_bytes_per_s": result.ci_low_bytes_per_s,
            "ci_high_bytes_per_s": result.ci_high_bytes_per_s,
            "excludes_zero": result.excludes_zero,
            "distinct_slopes": result.distinct_slopes,
            "degenerate": result.degenerate,
            "significant": result.significant,
            "resamples": result.resamples,
            "seed": result.seed,
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
