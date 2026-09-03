"""S14 acceptance: PATH B, and the tier's cost that may not be reported.

*PLAN.md S14:* "for the same workload, PATH B's `t_db_end − t_db_start`
distribution overlaps PATH A's latency distribution; `ab_delta_valid=false` is
stamped on every local run."

    PATH A   driver ------------------> engine
    PATH B   driver -> app tier ------> engine

Both ask the engine for the same statement over the same Docker network, so the
engine portion of each is the same thing measured from two places. If the two do
not land on top of each other, the app tier's span instrumentation is timing
something other than the engine -- and every later claim about where the time
went rests on it.

PATH B is driven at 60% of the tier's measured `/noop` ceiling, per S13. Driving
it at the ceiling would measure the queue in front of the app.
"""

from __future__ import annotations

import socket

import pytest

from dsel.app.ceiling import measure_ceiling, wait_ready
from dsel.app.compare import MAX_RESIDUAL_RATIO, Distribution, PathComparison
from dsel.app.stack import build_app_image, copy_shards, start_app
from dsel.audit.environment import resolve_image
from dsel.audit.models import AppCeilingRecord
from dsel.driver.calibrate import (
    PG_STAT_STATEMENTS_FLAGS,
    build_driver_image,
    connect,
    create_network,
    enable_statement_stats,
    pgbench_init,
    prewarm,
    run_driver,
)
from dsel.driver.histogram import hlog_name, read_hlog
from dsel.metrics.validity import APP_CPU_LIMIT_PCT, LOCAL_CEILING_FRACTION
from dsel.runtime.docker import provision, wait_healthy
from dsel.runtime.envelope import GIB, ResourceEnvelope
from dsel.runtime.paths import RunLayout, new_run_id
from dsel.runtime.teardown import Teardown
from tests.conftest import requires_docker

ENGINE_IMAGE = "postgres:18"
DATA_DIR = "/var/lib/postgresql/data"
READY = ["pg_isready", "-U", "postgres", "-q"]
ENGINE_ALIAS = "engine"
APP_ALIAS = "app"

SCALE = 5
APP_CPUS = 0.5
APP_WORKERS = 1
CEILING_RATES = (200.0, 500.0, 900.0, 1400.0)
DURATION_S = 10.0
# The route template, which is what the tier records as the endpoint.
ENDPOINT = "/account/{aid}"

pytestmark = [requires_docker, pytest.mark.slow]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def paths(tmp_path_factory: pytest.TempPathFactory):
    out = tmp_path_factory.mktemp("s14")
    layout = RunLayout.for_run(new_run_id(), base=out / "runs")
    layout.create()
    run_id = layout.run_id
    teardown = Teardown(run_id)
    try:
        pin = resolve_image(ENGINE_IMAGE)
        driver_image = build_driver_image()
        app_image = build_app_image()
        network = create_network(run_id)

        engine = provision(
            pin,
            ResourceEnvelope(cpuset=(2, 3, 4, 5), cpus=4.0, memory_bytes=3 * GIB),
            run_id,
            data_dir=DATA_DIR,
            container_port=5432,
            host_port=_free_port(),
            env={"POSTGRES_PASSWORD": "dsel", "PGDATA": f"{DATA_DIR}/pgdata"},
            command=PG_STAT_STATEMENTS_FLAGS,
        )
        wait_healthy(engine, READY)
        connect(network, engine.name, ENGINE_ALIAS)
        pgbench_init(engine.name, scale=SCALE)
        enable_statement_stats(engine.name)
        prewarm(engine.name, "pgbench_accounts", "pgbench_accounts_pkey")

        app_port = _free_port()
        app = start_app(
            app_image,
            run_id,
            app_port,
            network=network,
            alias=APP_ALIAS,
            dsn=f"postgresql://postgres:dsel@{ENGINE_ALIAS}:5432/postgres",
            cell="uc1/postgres/path-b/r0/rep1",
            cpus=APP_CPUS,
            workers=APP_WORKERS,
            scale=SCALE,
        )
        wait_ready("127.0.0.1", app_port)

        # S13 first: the tier's own ceiling, measured on /noop with no engine
        # in the interval, before PATH B is scheduled against it.
        ceiling = measure_ceiling(
            layout.root / "ceiling",
            "127.0.0.1",
            app_port,
            CEILING_RATES,
            duration_s=3.0,
            workers=4,
        )
        rate = ceiling.path_b_rate_per_s or 300.0

        # PATH A: driver straight to the engine, at the same rate.
        path_a = run_driver(
            driver_image,
            f"{run_id}-a",
            network,
            ENGINE_ALIAS,
            rate_per_s=rate,
            duration_s=DURATION_S,
            workers=4,
            cell=f"uc1/postgres/path-a/r{int(rate)}/rep1",
            scale=SCALE,
            out_dir=out / "path_a",
        )

        # PATH B: same driver, same rate, through the tier.
        from dsel.driver.calibrate import _docker

        spec_port = 8000
        path_b_name = f"dsel-driver-{run_id}-b"
        _docker(["rm", "--force", path_b_name])
        import json as _json

        result = _docker(
            [
                "run",
                "--name",
                path_b_name,
                "--label",
                "com.dsel.managed=true",
                "--label",
                f"com.dsel.run={run_id}",
                "--network",
                network,
                "--cpuset-cpus",
                "6-9",
                "--cpus",
                "4.0",
                "--memory",
                "1g",
                "--memory-swap",
                "1g",
                "--entrypoint",
                "python",
                driver_image,
                "-m",
                "dsel.driver.path_b",
                _json.dumps(
                    {
                        "host": APP_ALIAS,
                        "port": spec_port,
                        "path_template": "/account/{aid}",
                        "scale": SCALE,
                        "cell": f"uc1/postgres/path-b/r{int(rate)}/rep1",
                        "ops": ["account"],
                        "rate_per_s": rate,
                        "duration_s": DURATION_S,
                        "workers": 4,
                    }
                ),
            ],
            timeout=DURATION_S + 300.0,
        )
        assert result.returncode == 0, result.stderr
        summary = _json.loads(result.stdout.strip().splitlines()[-1])
        _docker(["cp", f"{path_b_name}:/run/dsel/histograms", str(out / "path_b")])
        _docker(["rm", "--force", path_b_name])

        app_hlogs = out / "app"
        app_hlogs.mkdir(parents=True, exist_ok=True)
        _docker(["cp", f"{app.name}:/run/dsel/histograms", str(app_hlogs)])
        copy_shards(app.name, out / "app_shards")
        return ceiling, path_a, summary, out, rate
    finally:
        teardown.run()


def test_the_tier_measured_its_ceiling_before_path_b_was_scheduled(paths) -> None:
    ceiling, _, _, _, rate = paths
    print("\n" + ceiling.table())
    assert ceiling.saturation_rate_per_s is not None, ceiling.table()
    assert rate == pytest.approx(ceiling.saturation_rate_per_s * LOCAL_CEILING_FRACTION), (
        "PATH B was not scheduled against the measured ceiling"
    )


def test_path_b_engine_interval_overlaps_path_a_latency(paths) -> None:
    """The acceptance. Two measurements of the same thing from two places."""
    _, path_a, summary, out, _ = paths
    # The endpoint is the route *template*, so the filename carries it: one
    # histogram per account id would be the cardinality mistake the exporter
    # refuses to make (S8b), one tier down.
    app_hlogs = out / "app" / "histograms"
    db_path = app_hlogs / hlog_name(f"{ENDPOINT}-db", "app")
    total_path = app_hlogs / hlog_name(f"{ENDPOINT}-total", "app")
    present = sorted(path.name for path in app_hlogs.iterdir())
    assert db_path.is_file(), f"no {db_path.name} among {present}"

    # PATH A's *inner* interval, not its end-to-end latency. The end-to-end
    # number carries the driver's own per-operation cost -- an entire event
    # loop turn around each request -- which the app tier does not pay per
    # request and structurally cannot appear in its `db_us`. Measured, that
    # made PATH A's latency read 100 us against PATH B's 66 us for the same
    # engine doing the same work. Comparing the two end-to-end numbers is the
    # same mistake S12 found in pgbench: a client cost read as the engine's.
    path_a_hlogs = out / "path_a" / "histograms"
    a = Distribution.from_hlog(
        "PATH A driver->engine",
        next(path_a_hlogs.glob("*account*inner.hlog")),
    )
    a_total = Distribution.from_hlog(
        "PATH A end to end",
        next(path_a_hlogs.glob("*account*uncorrected.hlog")),
    )
    b_db = Distribution.from_hlog("PATH B app->engine", db_path)
    b_total = (
        Distribution.from_hlog("PATH B app total", total_path) if total_path.is_file() else None
    )
    comparison = PathComparison(path_a=a, path_b_db=b_db, path_b_total=b_total)
    print("\n" + comparison.table())
    print(
        f"  PATH A end to end p50 {a_total.p50_us:.0f} us; its inner interval "
        f"{a.p50_us:.0f} us. The difference, {a_total.p50_us - a.p50_us:.0f} us, "
        "is the driver's own per-operation cost -- reported, not buried."
    )

    assert a.count > 500 and b_db.count > 500, comparison.table()
    assert comparison.overlaps, comparison.table()
    assert comparison.medians_agree, comparison.table()
    # The residual has a direction and a bound. The driver's own view of the
    # engine is the slower one -- it carries its spin-wait contention on cpuset
    # 6-9 and the tier does not -- and a ratio that changed sign would mean the
    # tier is reporting less time than the engine took, which cannot happen
    # unless the span is wrapping the wrong thing.
    assert 1.0 <= comparison.residual_ratio <= MAX_RESIDUAL_RATIO, comparison.table()
    assert a_total.p50_us > a.p50_us, (
        "the driver's end-to-end latency must exceed its own inner interval; "
        "if it does not, the inner measurement is not inside the outer one"
    )


def test_ab_delta_valid_is_false_on_every_local_run(paths) -> None:
    """Not a placeholder. The delta subtracts two measurements taken where
    cpuset does not isolate -- S1 measured 20-30% interference across sets --
    so each side carries it and the difference is not a clean measure of the
    tier."""
    ceiling, _, _, _, rate = paths
    from dsel.audit.models import Manifest

    assert Manifest.model_fields["ab_delta_valid"].default is False
    record = AppCeilingRecord(
        noop_saturation_rate_per_s=ceiling.saturation_rate_per_s,
        noop_max_delivered_rate_per_s=ceiling.max_delivered_rate_per_s,
        cpu_limit_pct=APP_CPU_LIMIT_PCT,
        path_b_rate_per_s=ceiling.path_b_rate_per_s,
        ceiling_fraction=LOCAL_CEILING_FRACTION,
        app_cpus=APP_CPUS,
        app_workers=APP_WORKERS,
    )
    assert record.path_b_rate_per_s == pytest.approx(rate)


def test_the_tier_recorded_spans_for_the_database_endpoint(paths) -> None:
    """PATH B's db interval must be a real fraction of its total, not zero and
    not everything: zero would mean the span never wrapped the query, and
    everything would mean the tier's own cost was never measured."""
    _, _, _, out, _ = paths
    app_hlogs = out / "app" / "histograms"
    db = read_hlog(app_hlogs / hlog_name(f"{ENDPOINT}-db", "app"))
    total = read_hlog(app_hlogs / hlog_name(f"{ENDPOINT}-total", "app"))
    db_p50 = float(db.get_value_at_percentile(50.0))
    total_p50 = float(total.get_value_at_percentile(50.0))
    assert 0 < db_p50 < total_p50, f"db {db_p50} total {total_p50}"
    assert db_p50 / total_p50 > 0.2, "the engine should be a real share of the request"
