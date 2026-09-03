"""Running one cell from inside the driver container (PLAN.md S11-S12).

The driver runs in a container so that it reaches the engine over the same
network path `pgbench` does; a host driver would carry Docker Desktop's
published-port hop and pgbench would not, and the difference would land in the
latency comparison as though it were a difference between the tools.

Invoked as `python -m dsel.driver.entrypoint <json spec>`. The spec goes in on
the command line and the summary comes back on stdout as JSON: no bind mount,
nothing shared, one process boundary. Histograms are written inside the
container and taken out with `docker cp`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dsel.driver.histogram import CORRECTED, UNCORRECTED, read_hlog
from dsel.driver.pool import DriverResult, plan_workers, run_pool
from dsel.driver.transport import PostgresFactory

RUN_DIR = Path("/run/dsel")


def summarise(result: DriverResult, ops: tuple[str, ...]) -> dict[str, object]:
    """Everything the caller needs, without shipping the histograms inline."""
    out: dict[str, object] = {
        "issued": result.issued,
        "completed": result.completed,
        "errors": result.errors,
        "achieved_rate_per_s": result.achieved_rate_per_s,
        "max_cpu_fraction": result.max_cpu_fraction,
        "verdict": result.verdict,
        "workers": [
            {
                "worker": w.worker,
                "issued": w.issued,
                "cpu_fraction": w.cpu_fraction,
                "lag_p50_us": w.lag_p50_us,
                "lag_p99_us": w.lag_p99_us,
                "service_p50_us": w.service_p50_us,
                "lag_share": w.lag_share,
                "verdicts": w.verdicts,
            }
            for w in result.workers
        ],
    }
    latency: dict[str, dict[str, float]] = {}
    for op in ops:
        for kind in (CORRECTED, UNCORRECTED):
            path = result.hlogs.get(f"{op}/{kind}")
            if path is None:
                continue
            histogram = read_hlog(path)
            latency[f"{op}/{kind}"] = {
                "count": float(histogram.get_total_count()),
                "mean_us": float(histogram.get_mean_value()),
                "p50_us": float(histogram.get_value_at_percentile(50.0)),
                "p99_us": float(histogram.get_value_at_percentile(99.0)),
                "p999_us": float(histogram.get_value_at_percentile(99.9)),
                "max_us": float(histogram.get_max_value()),
            }
    out["latency"] = latency
    out["hlogs"] = {key: str(path) for key, path in result.hlogs.items()}
    return out


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m dsel.driver.entrypoint '<json spec>'", file=sys.stderr)
        return 2
    spec = json.loads(argv[0])
    statement = spec.get("statement")
    factory = PostgresFactory(
        dsn=spec["dsn"],
        scale=int(spec.get("scale", 10)),
        **({"statement": statement} if statement else {}),
    )
    ops = tuple(spec.get("ops", ["select_account"]))

    specs = plan_workers(
        RUN_DIR,
        spec["cell"],
        ops,
        rate_per_s=float(spec["rate_per_s"]),
        duration_s=float(spec["duration_s"]),
        workers=int(spec.get("workers", 4)),
        warmup_s=float(spec.get("warmup_s", 0.0)),
        seed=int(spec.get("seed", 20260903)),
    )
    result = run_pool(specs, factory)
    json.dump(summarise(result, ops), sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
