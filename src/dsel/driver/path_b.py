"""Running PATH B from inside the driver container (PLAN.md S14).

The same entry point shape as `dsel.driver.entrypoint`, pointed at the app tier
instead of the engine. It is a separate module rather than a flag because the
two paths must be *identical above the transport* -- same scheduler, same
histograms, same gates -- and the cleanest way to guarantee that is for both to
call `run_pool` with nothing different but the factory.
"""

from __future__ import annotations

import json
import sys

from dsel.driver.entrypoint import RUN_DIR, summarise
from dsel.driver.pool import plan_workers, run_pool
from dsel.driver.transport import HttpFactory


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m dsel.driver.path_b '<json spec>'", file=sys.stderr)
        return 2
    spec = json.loads(argv[0])
    ops = tuple(spec.get("ops", ["account"]))
    factory = HttpFactory(
        host=spec["host"],
        port=int(spec.get("port", 8000)),
        path_template=spec.get("path_template", "/noop"),
        scale=int(spec.get("scale", 10)),
    )
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
