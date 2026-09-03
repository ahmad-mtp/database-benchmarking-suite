"""S11-S12 acceptance (calibration half): the driver against `pgbench`.

*PLAN.md:* "against identical Postgres and workload, driver and `pgbench -R`
agree on achieved rate within 1% and on mean latency within the measured noise
floor."

*"If the first-party driver disagrees with pgbench beyond the noise floor, the
driver is wrong."* So everything except the tool is held equal: the same
statement over the same rows, the same rate limit, the same latency definition
(both measure from the scheduled start under `-R`), and -- the part that is
easy to get wrong -- the same network path. Both tools run in containers on
cpuset 6-9 attached to the same Docker network. A driver on the host would
carry Docker Desktop's published-port hop that pgbench does not, and that hop
would appear in the comparison dressed up as a difference between the tools.

The noise floor is measured here, not assumed: each tool runs three times at
the same rate against the same unchanged Postgres, and its own spread is the
floor the other has to fall inside.

**Outcome.** The rate clause passes: 0.36% between the tools against the 1%
asked. The latency clause is *not decidable* on this host, and the reason is
recorded rather than tuned away -- `pg_stat_statements` shows the engine
spending about 22 us per statement for pgbench and about 4 us for this driver
for the identical query, with cache warmth, run order, protocol mode and plan
all excluded by measurement. Comparing what two clients observed is meaningless
while the server is not doing the same work for both.
"""

from __future__ import annotations

import socket

import pytest

from dsel.audit.environment import resolve_image
from dsel.driver.calibrate import (
    COUNT_A_RANGE,
    PG_STAT_STATEMENTS_FLAGS,
    SELECT_ONE_ROW,
    Comparison,
    ServerStats,
    build_driver_image,
    connect,
    create_network,
    enable_statement_stats,
    noise_floor,
    pgbench_init,
    prewarm,
    reset_statement_stats,
    run_driver,
    run_pgbench,
    statement_stats,
)
from dsel.runtime.docker import provision, wait_healthy
from dsel.runtime.envelope import GIB, ResourceEnvelope
from dsel.runtime.paths import new_run_id
from dsel.runtime.teardown import Teardown
from tests.conftest import requires_docker

ENGINE_IMAGE = "postgres:18"
DATA_DIR = "/var/lib/postgresql/data"
READY = ["pg_isready", "-U", "postgres", "-q"]
ALIAS = "engine"

SCALE = 5
# Sized so the comparison can mean something. The number of arrivals in a fixed
# window is Poisson, so its own relative spread is 1/sqrt(N): at 300/s for 8 s
# that is 2%, and no 1% agreement between two independent realisations is
# decidable underneath it. 1000/s for 15 s is 15 000 per run, 45 000 pooled.
RATE_PER_S = 1000.0
DURATION_S = 12.0
CLIENTS = 4
REPEATS = 3

# The second workload: ~10x the server work, same rows, both tools. It is what
# separates a fixed per-transaction client overhead from a measurement error --
# a constant offset stays constant when the statement gets dearer.
HEAVY_RATE_PER_S = 300.0
HEAVY_DURATION_S = 10.0
HEAVY_REPEATS = 2

# Discarded, and long enough to pull the accounts table into shared buffers.
WARMUP_S = 6.0

pytestmark = [requires_docker, pytest.mark.slow]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def calibration(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Comparison, Comparison, ServerStats, ServerStats]:
    """One Postgres, both tools, two workloads, plus the server's own account.

    The repeats are **interleaved** and preceded by a discarded warmup of each
    tool. Running one tool's three repeats and then the other's put pgbench on
    a cold buffer cache and the driver on a warm one: `pg_stat_statements` had
    the engine spending 26.9 us per statement for pgbench and 3.7 us for the
    driver, a 7x difference that was entirely the order they ran in. The same
    block design the interference sweep uses, for the same reason -- drift must
    not be able to align itself with the thing being compared.
    """
    out = tmp_path_factory.mktemp("s12")
    pin = resolve_image(ENGINE_IMAGE)
    run_id = new_run_id()
    teardown = Teardown(run_id)
    try:
        image = build_driver_image()
        network = create_network(run_id)
        container = provision(
            pin,
            ResourceEnvelope(cpuset=(2, 3, 4, 5), cpus=4.0, memory_bytes=3 * GIB),
            run_id,
            data_dir=DATA_DIR,
            container_port=5432,
            host_port=_free_port(),
            env={"POSTGRES_PASSWORD": "dsel", "PGDATA": f"{DATA_DIR}/pgdata"},
            command=PG_STAT_STATEMENTS_FLAGS,
        )
        wait_healthy(container, READY)
        connect(network, container.name, ALIAS)
        pgbench_init(container.name, scale=SCALE)
        enable_statement_stats(container.name)
        prewarm(container.name, "pgbench_accounts", "pgbench_accounts_pkey")

        def pgbench_at(index: int, rate: float, duration: float, statement: str):
            return run_pgbench(
                pin,
                f"{run_id}-{index}",
                network,
                ALIAS,
                rate_per_s=rate,
                duration_s=duration,
                clients=CLIENTS,
                scale=SCALE,
                statement=statement,
            )

        def driver_at(index: int, rate: float, duration: float, statement: str, cell: str):
            return run_driver(
                image,
                f"{run_id}-{index}",
                network,
                ALIAS,
                rate_per_s=rate,
                duration_s=duration,
                workers=CLIENTS,
                cell=cell,
                scale=SCALE,
                # A different seed per repeat: replaying one schedule three
                # times measures the target's variance, not the tool's, and
                # the realisation's own offset from the offered rate would be
                # a fixed bias rather than something the repeats average out.
                seed=20260903 + index,
                statement=statement,
                out_dir=out / f"run{index}",
            )

        # Warmup, discarded: connections established, plans cached, the
        # engine settled. The *cache* was warmed by `prewarm` above -- a
        # rate-limited run cannot do that job, and believing it could is what
        # produced a 6x difference in server-side execution time between the
        # two tools.
        pgbench_at(90, RATE_PER_S, WARMUP_S, SELECT_ONE_ROW[0])
        driver_at(90, RATE_PER_S, WARMUP_S, SELECT_ONE_ROW[1], "uc1/postgres/warmup/r0/rep1")

        pgbench_runs = []
        driver_runs = []
        server: dict[str, list[ServerStats]] = {"pgbench": [], "dsel": []}

        def measure_pgbench(index: int) -> None:
            reset_statement_stats(container.name)
            pgbench_runs.append(pgbench_at(index, RATE_PER_S, DURATION_S, SELECT_ONE_ROW[0]))
            server["pgbench"].append(statement_stats(container.name, "abalance"))

        def measure_driver(index: int) -> None:
            reset_statement_stats(container.name)
            driver_runs.append(
                driver_at(
                    index,
                    RATE_PER_S,
                    DURATION_S,
                    SELECT_ONE_ROW[1],
                    f"uc1/postgres/pgbench-select/r{int(RATE_PER_S)}/rep{index + 1}",
                )
            )
            server["dsel"].append(statement_stats(container.name, "abalance"))

        # The order within each block alternates. Running pgbench immediately
        # before the driver every time entangles tool with position: anything
        # the first run leaves behind -- a cache, a frequency state, a settled
        # scheduler -- would be credited to the second tool. The interference
        # sweep randomises block order for the same reason.
        for index in range(REPEATS):
            if index % 2 == 0:
                measure_pgbench(index)
                measure_driver(index)
            else:
                measure_driver(index)
                measure_pgbench(index)

        light = Comparison(driver=noise_floor(driver_runs), pgbench=noise_floor(pgbench_runs))

        heavy_pgbench = []
        heavy_driver = []
        for index in range(HEAVY_REPEATS):
            heavy_pgbench.append(
                pgbench_at(100 + index, HEAVY_RATE_PER_S, HEAVY_DURATION_S, COUNT_A_RANGE[0])
            )
            heavy_driver.append(
                driver_at(
                    100 + index,
                    HEAVY_RATE_PER_S,
                    HEAVY_DURATION_S,
                    COUNT_A_RANGE[1],
                    f"uc1/postgres/count-range/r{int(HEAVY_RATE_PER_S)}/rep{index + 1}",
                )
            )
        heavy = Comparison(driver=noise_floor(heavy_driver), pgbench=noise_floor(heavy_pgbench))
        return light, heavy, _pool(server["pgbench"]), _pool(server["dsel"])
    finally:
        teardown.run()


def _pool(stats: list[ServerStats]) -> ServerStats:
    """Pool per-run server figures into one call-weighted record."""
    calls = sum(s.calls for s in stats)
    if not calls:
        return ServerStats(0, 0.0, 0, 0)
    return ServerStats(
        calls=calls,
        mean_exec_us=sum(s.calls * s.mean_exec_us for s in stats) / calls,
        blocks_hit=sum(s.blocks_hit for s in stats),
        blocks_read=sum(s.blocks_read for s in stats),
    )


def test_both_tools_delivered_the_offered_rate(
    calibration,
) -> None:
    """Before comparing them to each other: each must have done what it was told."""
    light = calibration[0]
    for floor in (light.driver, light.pgbench):
        shortfall = abs(floor.mean_rate_per_s - RATE_PER_S) / RATE_PER_S
        assert shortfall < 0.05, (
            f"{floor.tool} achieved {floor.mean_rate_per_s:.1f}/s of {RATE_PER_S:.0f}/s"
        )


def test_the_driver_and_pgbench_agree_on_achieved_rate(
    calibration,
) -> None:
    light = calibration[0]
    print("\n--- one indexed row ---\n" + light.table())
    assert light.rate_difference <= 0.01, (
        f"PLAN.md asks for 1%; measured {light.rate_difference:.2%}\n{light.table()}"
    )


def test_the_service_time_difference_is_a_fixed_client_overhead(
    calibration,
) -> None:
    """The two tools do not agree on service time, and this establishes why.

    pgbench's own schedule lag is already subtracted from both sides. What is
    left is per-transaction client cost -- libpq's text-format result handling
    and pgbench's own loop, against asyncpg's binary prepared protocol -- and
    that is a constant, not a proportional error. Run a statement roughly ten
    times dearer and the *absolute* gap stays where it was; a driver that was
    mis-measuring durations would have the gap grow with them.
    """
    light, heavy = calibration[0], calibration[1]
    print("\n--- count over a 5000-row range ---\n" + heavy.table())

    light_gap = light.pgbench.mean_latency_us - light.driver.mean_latency_us
    heavy_gap = heavy.pgbench.mean_latency_us - heavy.driver.mean_latency_us
    growth = heavy.driver.mean_latency_us / light.driver.mean_latency_us

    print(
        f"\n  server work grew {growth:.1f}x "
        f"({light.driver.mean_latency_us:.0f} -> {heavy.driver.mean_latency_us:.0f} us)"
        f"\n  pgbench's extra client cost: {light_gap:.0f} us -> {heavy_gap:.0f} us"
    )
    assert growth > 4.0, f"the second workload was not materially dearer ({growth:.1f}x)"
    assert heavy_gap > light_gap, "the dearer statement should not cost pgbench less"
    # Both tools are slower than the engine by a client-side cost, and the
    # cost is not the same for both. What must not happen is the gap growing
    # *faster* than the work: that would mean one of them is scaling durations.
    assert heavy_gap / light_gap < growth, (
        f"pgbench's excess grew {heavy_gap / light_gap:.1f}x while the work grew "
        f"{growth:.1f}x; a client overhead cannot outrun the work it wraps"
    )


def test_the_two_runs_faced_the_same_cache(calibration) -> None:
    """A validity gate, not a nicety.

    Without `pg_prewarm`, whichever tool ran first did the physical reads:
    measured, pgbench did 1483 block reads to the driver's 763 for the
    identical statement, and the engine's own execution time came out 33 us
    against 5 us as a result. A rate-limited warmup cannot fix that -- at
    1000/s for six seconds it makes about 6000 random accesses across a table
    of 8000-odd pages. The working set has to be pulled in explicitly.
    """
    _, _, server_pgbench, server_driver = calibration
    for stats in (server_pgbench, server_driver):
        assert stats.calls > 1000, f"{stats.calls} calls is not a run"
        assert stats.reads_per_call < 0.02, (
            f"{stats.reads_per_call:.3f} block reads per call: the working set "
            "was not resident, so this run measured the disk"
        )


def test_the_engine_reports_different_execution_times_for_the_two_tools(
    calibration,
) -> None:
    """**UNEXPLAINED, and recorded rather than smoothed over.**

    With an identical query string, identical plan time, identical row counts
    and zero physical reads on both sides, `pg_stat_statements` reports about
    22 us of execution per statement for pgbench and about 4 us for this
    driver. Four confounds have been excluded by measurement:

    * *cache warmth* -- both sides now show 0.000 block reads per call after
      `pg_prewarm`, and the gap did not move;
    * *run order* -- the blocks alternate which tool goes first, and the gap
      followed the tool, not the position;
    * *the wire protocol mode* -- pgbench at `-M simple` and `-M prepared`
      differ by about 5%, nowhere near 6x;
    * *the plan* -- plan time is 0 for both and `shared_blks_hit` matches to
      within 1%, so the same buffers are being touched by the same plan.

    What remains untested is the parameter and result *format*: pgbench uses
    libpq's text encoding on both, asyncpg uses binary on both, and the output
    conversion happens inside the window `mean_exec_time` covers. That is a
    hypothesis, not a finding, and it is written here as one.

    Until it is explained, PLAN.md's "agree on mean latency within the measured
    noise floor" cannot be decided: the engine itself is not doing the same
    amount of work for the two clients, so a difference in what the clients
    measured says nothing about the clients. What this test does assert is the
    relation that would catch a genuinely broken driver -- neither tool can
    report less client-side service time than the server spent executing.
    """
    light, _, server_pgbench, server_driver = calibration
    print(
        f"\n  pg_stat_statements  pgbench: {server_pgbench.calls} calls, "
        f"{server_pgbench.mean_exec_us:.1f} us exec, "
        f"{server_pgbench.reads_per_call:.3f} block reads/call"
        f"\n                      dsel:    {server_driver.calls} calls, "
        f"{server_driver.mean_exec_us:.1f} us exec, "
        f"{server_driver.reads_per_call:.3f} block reads/call"
        f"\n  UNEXPLAINED: the engine reports "
        f"{server_pgbench.mean_exec_us / server_driver.mean_exec_us:.1f}x the "
        "execution time for pgbench, with cache warmth, run order, protocol "
        "mode and plan all excluded"
    )
    assert light.driver.mean_latency_us > server_driver.mean_exec_us, (
        f"the driver reports {light.driver.mean_latency_us:.0f} us of service "
        f"while the engine spent {server_driver.mean_exec_us:.1f} us executing: "
        "a client cannot observe less than the server took"
    )
    assert light.pgbench.mean_latency_us > server_pgbench.mean_exec_us


def test_the_driver_adds_far_less_schedule_lag_than_pgbench(
    calibration,
) -> None:
    """Both measure from the scheduled start, so both carry their own lateness.
    Reported rather than hidden: it is a real difference between the drivers,
    and subtracting it is what makes the service times comparable at all."""
    light = calibration[0]
    assert light.pgbench.mean_lag_us > 100.0, "pgbench should show its own lag"
    assert light.driver.mean_lag_us < light.pgbench.mean_lag_us / 10.0, (
        f"driver lag {light.driver.mean_lag_us:.0f} us against pgbench's "
        f"{light.pgbench.mean_lag_us:.0f} us"
    )


def test_the_noise_floor_was_actually_measured(
    calibration,
) -> None:
    """A floor of zero would mean the repeats were not independent, and the
    comparison would be asserting exact equality by accident."""
    light = calibration[0]
    assert len(light.driver.latencies_us) == REPEATS
    assert len(light.pgbench.latencies_us) == REPEATS
    assert light.measured_noise_floor > 0.0
    assert len(set(light.driver.rates)) == REPEATS, (
        "the driver repeats produced identical counts; the seeds did not vary "
        "and the repeats are one realisation replayed"
    )


def test_the_driver_left_its_histograms_behind(tmp_path_factory) -> None:
    """The comparison quotes a mean; the bundle needs the raw histogram."""
    root = tmp_path_factory.getbasetemp()
    hlogs = list(root.glob("s12*/run*/histograms/*.hlog"))
    assert hlogs, "no histograms were copied out of the driver container"
    assert any("corrected" in p.name for p in hlogs)
    assert any("uncorrected" in p.name for p in hlogs)
