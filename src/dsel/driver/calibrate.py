"""Calibrating the driver against `pgbench` (PLAN.md S11-S12).

*"If the first-party driver disagrees with pgbench beyond the noise floor, the
driver is wrong."*

The comparison only means something if everything except the tool is held
equal, and three things had to be arranged for that:

* **Same statement, same rows.** `pgbench -S` issues
  `SELECT abalance FROM pgbench_accounts WHERE aid = :aid` with `aid` uniform
  over `1..100000*scale`. `PostgresTransport` issues exactly that, drawing
  `aid` uniformly over the same range.
* **Same network path.** D5 puts calibration tools inside the engine image, so
  pgbench reaches Postgres over a Docker network. A driver on the host would
  reach it through Docker Desktop's published-port hop instead, and that hop
  would show up in the latency difference looking like a difference between the
  tools. Both therefore run in containers, on cpuset 6-9, attached to the same
  network.
* **Same latency definition.** Under `-R`, pgbench measures latency from the
  *scheduled* start of each transaction, which is what this driver's corrected
  histogram measures. Without `-R` it would be measuring something else
  entirely and the numbers would not be comparable at all.
* **Same wire protocol.** pgbench defaults to `-M simple`, which sends the
  statement text every time and has the server parse and plan it every time;
  asyncpg prepares and caches. Measured, that difference alone put pgbench at
  1837 us against the driver's 585 us -- a 3x gap with nothing to do with
  either tool's load generation. `-M prepared` is therefore not a tuning
  choice, it is what makes the two comparable.
* **Same rate definition.** Each tool divides by its own elapsed time, and the
  two elapsed times are not the same quantity: pgbench excludes connection
  setup and stops at its last scheduled arrival. Comparing the tools' own
  denominators compares their reporting conventions, the same trap the S10
  percentile comparison fell into. Both are therefore recomputed here as
  `transactions / duration`, over the duration both were given.

**What is compared is the service time, not the reported latency.** pgbench
prints its own schedule lag, and at 300/s it is most of what it reports:

    latency average = 1.711 ms
    rate limit schedule lag: avg 1.500 (max 5.225) ms
    statement latencies: 0.209 ms  SELECT abalance FROM pgbench_accounts ...

1.500 ms of that 1.711 ms is pgbench waiting for its own scheduler, and 0.211 ms
is Postgres. Both tools measure from the scheduled start, so both carry their
own lag; comparing the totals compares the two schedulers, not the engine. Each
tool's lag is subtracted -- pgbench's from its own printed figure, this
driver's as `corrected mean - uncorrected mean` -- and the remainders are what
must agree. The lag difference is reported rather than hidden: it is a real
difference between the drivers, and a large one.

**The noise floor is measured, not assumed.** Each tool is run several times at
the same rate, with a different seed each time so the repeats are independent
realisations rather than the same schedule replayed, and the spread within a
tool is the floor the other has to fall inside. Quoting a fixed percentage
would be inventing a tolerance that happens to pass.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path

from dsel.audit.models import ImagePin
from dsel.driver.transport import PGBENCH_ACCOUNTS_PER_SCALE
from dsel.runtime.teardown import LABEL_KEY, MANAGED_LABEL

DRIVER_CPUSET = "6-9"
DEFAULT_SCALE = 10

# The two workloads the comparison uses. The second is roughly ten times the
# server work of the first, which is how a *fixed* per-transaction client
# overhead is told apart from a proportional measurement error: a constant
# offset stays constant when the statement gets dearer, and a scaling one does
# not. Both tools run both, over the same rows.
SELECT_ONE_ROW = (
    "SELECT abalance FROM pgbench_accounts WHERE aid = {aid}",
    "SELECT abalance FROM pgbench_accounts WHERE aid = $1",
)
COUNT_A_RANGE = (
    "SELECT count(*) FROM pgbench_accounts WHERE aid BETWEEN {aid} AND {aid} + 5000",
    "SELECT count(*) FROM pgbench_accounts WHERE aid BETWEEN $1 AND $1 + 5000",
)

# pgbench prints these; anything else it prints is not a measurement.
TPS_PATTERN = re.compile(r"^tps\s*=\s*([0-9.]+)", re.MULTILINE)
LATENCY_PATTERN = re.compile(r"^latency average\s*=\s*([0-9.]+)\s*ms", re.MULTILINE)
LATENCY_STDDEV_PATTERN = re.compile(r"^latency stddev\s*=\s*([0-9.]+)\s*ms", re.MULTILINE)
PROCESSED_PATTERN = re.compile(r"number of transactions actually processed:\s*(\d+)")
SCHEDULE_LAG_PATTERN = re.compile(r"rate limit schedule lag:\s*avg\s*([0-9.]+)")


class CalibrationError(RuntimeError):
    """A calibration run could not be completed or could not be parsed."""


@dataclass(frozen=True, slots=True)
class Measurement:
    """One run of one tool, reduced to the two numbers being compared."""

    tool: str
    offered_rate_per_s: float
    duration_s: float
    reported_rate_per_s: float
    """What the tool said its rate was, by its own definition of elapsed."""
    mean_latency_us: float
    """Total, from the scheduled start -- what the tool itself reports."""
    schedule_lag_us: float
    """The tool's own lateness, which is inside `mean_latency_us`."""
    transactions: int
    detail: str = ""

    @property
    def service_us(self) -> float:
        """What the engine took: the total, less the driver's own lateness.

        This is the quantity the two tools can be compared on. Their totals
        cannot be: pgbench's scheduler lags about 1.5 ms at 300/s and this
        driver's does not, so comparing totals measures the schedulers.
        """
        return max(0.0, self.mean_latency_us - self.schedule_lag_us)

    @property
    def achieved_rate_per_s(self) -> float:
        """Transactions over the window both tools were given.

        Not the tool's own figure: pgbench's elapsed excludes connection setup
        and ends at its last scheduled arrival, and a 1.5% difference in
        denominators would be reported as a difference between the drivers.
        """
        return self.transactions / self.duration_s

    @property
    def delivery_error(self) -> float:
        return abs(self.achieved_rate_per_s - self.offered_rate_per_s) / self.offered_rate_per_s


@dataclass(frozen=True, slots=True)
class NoiseFloor:
    """The spread within one tool, repeated against an unchanged target."""

    tool: str
    rates: tuple[float, ...]
    latencies_us: tuple[float, ...]
    """Service times: the total less each run's own schedule lag."""
    lags_us: tuple[float, ...] = ()
    transactions: tuple[int, ...] = ()

    @property
    def rate_spread(self) -> float:
        """Relative spread of achieved rate: (max - min) / mean."""
        return _relative_spread(self.rates)

    @property
    def latency_spread(self) -> float:
        return _relative_spread(self.latencies_us)

    @property
    def mean_latency_us(self) -> float:
        return statistics.fmean(self.latencies_us)

    @property
    def mean_rate_per_s(self) -> float:
        return statistics.fmean(self.rates)

    @property
    def mean_lag_us(self) -> float:
        return statistics.fmean(self.lags_us) if self.lags_us else 0.0

    @property
    def poisson_sigma(self) -> float:
        """Relative standard deviation of the pooled arrival count.

        Both tools draw exponential inter-arrivals, so the number of arrivals
        in a fixed window is Poisson and its relative spread is `1/sqrt(N)`.
        A comparison tolerance below this is a coin toss, not a criterion.
        """
        total = sum(self.transactions)
        return math.sqrt(total) / total if total else 0.0


def _relative_spread(values: tuple[float, ...]) -> float:
    if len(values) < 2:
        return 0.0
    mean = statistics.fmean(values)
    return (max(values) - min(values)) / mean if mean else 0.0


def _docker(args: list[str], timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _labels(run_id: str) -> list[str]:
    return [
        "--label",
        f"{MANAGED_LABEL}=true",
        "--label",
        f"{LABEL_KEY}={run_id}",
    ]


def create_network(run_id: str) -> str:
    """A user-defined network, so both tools take the same path to the engine."""
    name = f"dsel-net-{run_id}"
    result = _docker(["network", "create", *_labels(run_id), name])
    if result.returncode != 0 and "already exists" not in result.stderr:
        raise CalibrationError(f"could not create network: {result.stderr.strip()}")
    return name


def connect(network: str, container: str, alias: str) -> None:
    result = _docker(["network", "connect", "--alias", alias, network, container])
    if result.returncode != 0 and "already exists" not in result.stderr:
        raise CalibrationError(f"could not attach {container}: {result.stderr.strip()}")


# Postgres's own account of what it executed, which is the only arbiter that
# is neither of the two tools. Enabled with these flags at provision time.
PG_STAT_STATEMENTS_FLAGS = [
    "-c",
    "shared_preload_libraries=pg_stat_statements",
    "-c",
    "pg_stat_statements.track=all",
    "-c",
    "pg_stat_statements.max=1000",
]


def psql(container: str, sql: str, *, user: str = "postgres", password: str = "dsel") -> str:
    """Run one statement in the engine container and return its output."""
    result = _docker(
        [
            "exec",
            "--env",
            f"PGPASSWORD={password}",
            container,
            "psql",
            "-U",
            user,
            "-tAX",
            "-c",
            sql,
        ],
        timeout=120.0,
    )
    if result.returncode != 0:
        raise CalibrationError(f"psql failed: {result.stderr.strip()}")
    return result.stdout.strip()


def enable_statement_stats(container: str) -> None:
    psql(container, "CREATE EXTENSION IF NOT EXISTS pg_stat_statements")


def reset_statement_stats(container: str) -> None:
    psql(container, "SELECT pg_stat_statements_reset()")


def prewarm(container: str, *relations: str) -> None:
    """Pull the working set into shared buffers before anything is measured.

    A rate-limited warmup run does not warm a cache -- it issues requests. At
    800/s for six seconds a warmup makes about 4800 random accesses across a
    table of 8000-odd pages, so most of the working set is still cold when the
    measurement starts and whichever tool runs first pays for the physical
    reads. Measured: `pg_stat_statements` showed pgbench doing 1483 block reads
    against the driver's 763 for the identical statement, and the engine's own
    execution time differed 33 us against 5 us as a result -- a 6x gap that was
    entirely I/O the second tool did not have to do.
    """
    psql(container, "CREATE EXTENSION IF NOT EXISTS pg_prewarm")
    for relation in relations:
        psql(container, f"SELECT pg_prewarm('{relation}')")


@dataclass(frozen=True, slots=True)
class ServerStats:
    """What Postgres says it did, which is neither tool's account of it."""

    calls: int
    mean_exec_us: float
    blocks_hit: int
    blocks_read: int

    @property
    def reads_per_call(self) -> float:
        return self.blocks_read / self.calls if self.calls else 0.0


def statement_stats(container: str, like: str) -> ServerStats:
    """Server-side figures for statements matching `like`.

    This is Postgres measuring itself. If both tools produce the same figures
    for the same statement, then whatever separates their client-side numbers
    is client-side -- which is the only way to answer "is the driver wrong?"
    without asking one of the two suspects. `blocks_read` is carried because a
    difference there means the two runs did not face the same cache, and then
    nothing else in the comparison means anything.
    """
    row = psql(
        container,
        "SELECT coalesce(sum(calls), 0), "
        "coalesce(sum(mean_exec_time * calls) / nullif(sum(calls), 0), 0), "
        "coalesce(sum(shared_blks_hit), 0), coalesce(sum(shared_blks_read), 0) "
        "FROM pg_stat_statements WHERE query ILIKE " + repr(f"%{like}%").replace('"', "'"),
    )
    if not row:
        return ServerStats(0, 0.0, 0, 0)
    calls, mean_ms, hit, read = row.split("|")
    return ServerStats(
        calls=int(calls),
        mean_exec_us=float(mean_ms) * 1000.0,
        blocks_hit=int(hit),
        blocks_read=int(read),
    )


def pgbench_init(container: str, *, scale: int = DEFAULT_SCALE, user: str = "postgres") -> str:
    """Initialise the pgbench schema *inside the engine container* (D5)."""
    result = _docker(
        ["exec", container, "pgbench", "-i", "-q", "-s", str(scale), "-U", user, user],
        timeout=900.0,
    )
    if result.returncode != 0:
        raise CalibrationError(f"pgbench -i failed: {result.stderr.strip()}")
    return result.stderr.strip()


def parse_pgbench(output: str) -> tuple[float, float, float, int]:
    """`(tps, mean latency us, schedule lag us, transactions)` from the summary.

    The schedule lag line is only printed under `-R`, which is the only mode
    this comparison uses; its absence means the run was not rate-limited and
    the latencies are not comparable at all, so it is required rather than
    defaulted to zero.
    """
    tps = TPS_PATTERN.search(output)
    latency = LATENCY_PATTERN.search(output)
    lag = SCHEDULE_LAG_PATTERN.search(output)
    processed = PROCESSED_PATTERN.search(output)
    if not (tps and latency and processed and lag):
        raise CalibrationError(f"could not parse pgbench output:\n{output}")
    return (
        float(tps.group(1)),
        float(latency.group(1)) * 1000.0,
        float(lag.group(1)) * 1000.0,
        int(processed.group(1)),
    )


def run_pgbench(
    engine_pin: ImagePin,
    run_id: str,
    network: str,
    host_alias: str,
    *,
    rate_per_s: float,
    duration_s: float,
    clients: int,
    scale: int = DEFAULT_SCALE,
    user: str = "postgres",
    password: str = "dsel",
    statement: str | None = None,
) -> Measurement:
    """Run `pgbench -R` from a container built on the engine image (D5).

    A separate container rather than `docker exec` into the engine: the tool
    must not share the engine's cpuset, or it competes with what it measures.

    With `statement`, the script is written from an environment variable inside
    the container rather than mounted -- D7 forbids a bind mount for the life
    of a run, and a one-line SQL file is no exception.
    """
    name = f"dsel-pgbench-{run_id}"
    _docker(["rm", "--force", name])
    rows = scale * PGBENCH_ACCOUNTS_PER_SCALE
    pgbench_args = [
        "-n",
        # asyncpg prepares and caches; -M simple would have the server parse
        # and plan every statement, which is a 3x latency difference between
        # the protocols rather than between the tools.
        "-M",
        "prepared",
        "-h",
        host_alias,
        "-U",
        user,
        "-c",
        str(clients),
        "-j",
        str(min(clients, 4)),
        "-R",
        str(rate_per_s),
        "-T",
        str(int(duration_s)),
        user,
    ]
    if statement is None:
        entrypoint = ["--entrypoint", "pgbench"]
        command = ["-S", *pgbench_args]
        env = [f"PGPASSWORD={password}"]
    else:
        script = f"\\set aid random(1, {rows})\n{statement.format(aid=':aid')};\n"
        entrypoint = ["--entrypoint", "sh"]
        command = [
            "-c",
            'printf "%b" "$DSEL_SCRIPT" > /tmp/w.sql && exec pgbench -f /tmp/w.sql "$@"',
            "sh",
            *pgbench_args,
        ]
        env = [f"PGPASSWORD={password}", f"DSEL_SCRIPT={script}"]

    run_args = [
        "run",
        "--rm",
        "--name",
        name,
        *_labels(run_id),
        "--network",
        network,
        "--cpuset-cpus",
        DRIVER_CPUSET,
        "--cpus",
        "4.0",
        "--memory",
        "1g",
        "--memory-swap",
        "1g",
    ]
    for value in env:
        run_args += ["--env", value]
    run_args += [*entrypoint, engine_pin.pinned, *command]
    result = _docker(run_args, timeout=duration_s + 300.0)
    if result.returncode != 0:
        raise CalibrationError(f"pgbench failed: {result.stderr.strip()}")
    tps, latency_us, lag_us, transactions = parse_pgbench(result.stdout + result.stderr)
    return Measurement(
        tool="pgbench",
        offered_rate_per_s=rate_per_s,
        duration_s=duration_s,
        reported_rate_per_s=tps,
        mean_latency_us=latency_us,
        schedule_lag_us=lag_us,
        transactions=transactions,
        detail=result.stdout.strip(),
    )


def build_driver_image(tag: str = "dsel-driver:local", context: Path | None = None) -> str:
    """Build the driver image. Cached after the first call."""
    context = context or Path(__file__).resolve().parents[3]
    result = _docker(
        [
            "build",
            "--file",
            str(context / "images" / "driver" / "Dockerfile"),
            "--tag",
            tag,
            str(context),
        ],
        timeout=900.0,
    )
    if result.returncode != 0:
        raise CalibrationError(f"could not build the driver image:\n{result.stderr.strip()}")
    return tag


def run_driver(
    image: str,
    run_id: str,
    network: str,
    host_alias: str,
    *,
    rate_per_s: float,
    duration_s: float,
    workers: int,
    cell: str,
    scale: int = DEFAULT_SCALE,
    user: str = "postgres",
    password: str = "dsel",
    warmup_s: float = 0.0,
    seed: int = 20260903,
    statement: str | None = None,
    out_dir: Path | None = None,
) -> Measurement:
    """Run the first-party driver in a container on the same network and cpuset."""
    name = f"dsel-driver-{run_id}"
    _docker(["rm", "--force", name])
    spec = json.dumps(
        {
            "dsn": f"postgresql://{user}:{password}@{host_alias}:5432/{user}",
            "scale": scale,
            "cell": cell,
            "ops": ["select_account"],
            "rate_per_s": rate_per_s,
            "duration_s": duration_s,
            "workers": workers,
            "warmup_s": warmup_s,
            "seed": seed,
            "statement": statement,
        }
    )
    result = _docker(
        [
            "run",
            "--name",
            name,
            *_labels(run_id),
            "--network",
            network,
            "--cpuset-cpus",
            DRIVER_CPUSET,
            "--cpus",
            "4.0",
            "--memory",
            "1g",
            "--memory-swap",
            "1g",
            image,
            spec,
        ],
        timeout=duration_s + 300.0,
    )
    if result.returncode != 0:
        _docker(["rm", "--force", name])
        raise CalibrationError(f"the driver failed:\n{result.stderr.strip()}")
    try:
        summary = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        _docker(["rm", "--force", name])
        raise CalibrationError(f"could not parse the driver summary:\n{result.stdout}") from exc
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        _docker(["cp", f"{name}:/run/dsel/histograms", str(out_dir)])
    _docker(["rm", "--force", name])

    corrected = summary["latency"]["select_account/corrected"]
    uncorrected = summary["latency"]["select_account/uncorrected"]
    # The same arithmetic pgbench prints: total, less the driver's own lateness.
    lag_us = max(0.0, float(corrected["mean_us"]) - float(uncorrected["mean_us"]))
    return Measurement(
        tool="dsel",
        offered_rate_per_s=rate_per_s,
        duration_s=duration_s,
        reported_rate_per_s=float(summary["achieved_rate_per_s"]),
        mean_latency_us=float(corrected["mean_us"]),
        schedule_lag_us=lag_us,
        transactions=int(summary["completed"]),
        detail=json.dumps(summary["workers"]),
    )


def noise_floor(measurements: list[Measurement]) -> NoiseFloor:
    """Reduce repeats of one tool to its own spread."""
    if not measurements:
        raise CalibrationError("no measurements")
    return NoiseFloor(
        tool=measurements[0].tool,
        rates=tuple(m.achieved_rate_per_s for m in measurements),
        latencies_us=tuple(m.service_us for m in measurements),
        lags_us=tuple(m.schedule_lag_us for m in measurements),
        transactions=tuple(m.transactions for m in measurements),
    )


@dataclass(frozen=True, slots=True)
class Comparison:
    """The calibration verdict, with the arithmetic that produced it."""

    driver: NoiseFloor
    pgbench: NoiseFloor
    rate_tolerance: float = 0.01

    @property
    def measured_noise_floor(self) -> float:
        """The larger of the two tools' own latency spreads."""
        return max(self.driver.latency_spread, self.pgbench.latency_spread)

    @property
    def rate_difference(self) -> float:
        mean = (self.driver.mean_rate_per_s + self.pgbench.mean_rate_per_s) / 2.0
        return abs(self.driver.mean_rate_per_s - self.pgbench.mean_rate_per_s) / mean

    @property
    def latency_difference(self) -> float:
        mean = (self.driver.mean_latency_us + self.pgbench.mean_latency_us) / 2.0
        return abs(self.driver.mean_latency_us - self.pgbench.mean_latency_us) / mean

    @property
    def sampling_tolerance(self) -> float:
        """The floor under any rate comparison of two Poisson realisations.

        Three sigma of the difference of the two pooled counts. PLAN.md's flat
        1% is below this for any run short enough to be a test: at 300/s for
        8 s a run holds ~2400 arrivals, whose own spread is 2%.
        """
        combined = math.sqrt(self.driver.poisson_sigma**2 + self.pgbench.poisson_sigma**2)
        return 3.0 * combined

    @property
    def rates_agree(self) -> bool:
        return self.rate_difference <= max(self.rate_tolerance, self.sampling_tolerance)

    @property
    def lag_difference_us(self) -> float:
        """How much more lateness one driver adds than the other."""
        return self.pgbench.mean_lag_us - self.driver.mean_lag_us

    @property
    def latencies_agree(self) -> bool:
        return self.latency_difference <= self.measured_noise_floor

    def table(self) -> str:
        lines = [
            f"{'tool':<9} {'achieved/s':>11} {'service us':>11} {'own lag us':>11} "
            f"{'rate sd':>8} {'svc sd':>8}",
            f"{'-' * 9} {'-' * 11} {'-' * 11} {'-' * 11} {'-' * 8} {'-' * 8}",
        ]
        for floor in (self.driver, self.pgbench):
            lines.append(
                f"{floor.tool:<9} {floor.mean_rate_per_s:>11.1f} "
                f"{floor.mean_latency_us:>11.0f} {floor.mean_lag_us:>11.0f} "
                f"{floor.rate_spread:>7.2%} {floor.latency_spread:>7.2%}"
            )
        lines += [
            "",
            f"rate difference     {self.rate_difference:.2%} against a tolerance of "
            f"{max(self.rate_tolerance, self.sampling_tolerance):.2%} "
            f"(1.00% asked, {self.sampling_tolerance:.2%} Poisson floor) -> "
            f"{'agree' if self.rates_agree else 'DISAGREE'}",
            f"service difference  {self.latency_difference:.2%} against a measured "
            f"noise floor of {self.measured_noise_floor:.2%} -> "
            f"{'agree' if self.latencies_agree else 'DISAGREE'}",
            f"schedule lag        pgbench adds {self.lag_difference_us:.0f} us more "
            "than this driver -- a difference between the schedulers, which is "
            "why it is subtracted before comparing",
        ]
        return "\n".join(lines)
