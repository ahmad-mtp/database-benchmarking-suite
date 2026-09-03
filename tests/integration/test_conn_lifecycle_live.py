"""S16-S18c acceptance: every refusal named, against a real engine.

*PLAN.md:* "a `max_connections=32` run attributes every refusal to a named
mechanism with zero `unknown` causes."

PLAN.md also notes the thing that makes this awkward and is easy to get wrong:
`max_connections` **is not a runtime knob**. It needs its own provision cycle,
so the provocation cannot be folded into a run that is measuring something
else -- which is precisely why it gets its own test rather than a flag on an
existing one.

Two mechanisms are provoked, not one. A table that only ever sees
`max_connections` has not been shown to *discriminate*; it has been shown to
return a constant.

And the second one is only attributable from the *engine's* log. When Postgres
reaps an idle-in-transaction session it writes a FATAL and closes the socket;
the client's next statement finds nothing left to receive an error from, and
sees only `server_closed` with no SQLSTATE. Measured here before the log
reader existed. For anything the server initiates, the client's account is not
evidence -- so the engine is provisioned with `%e` in `log_line_prefix` and its
log is read as a second source.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket

import pytest

from dsel.audit.environment import resolve_image
from dsel.live.merge import find_shards, merge_records
from dsel.live.ndjson import ShardWriter
from dsel.live.sampler.connections import (
    LOG_LINE_PREFIX,
    engine_log_events,
    open_until_refused,
)
from dsel.live.schema import ConnectionEventRecord
from dsel.phenomena.conn_lifecycle import UNKNOWN, attribute_records
from dsel.runtime.docker import provision, wait_healthy
from dsel.runtime.envelope import GIB, ResourceEnvelope
from dsel.runtime.paths import RunLayout, new_run_id
from dsel.runtime.teardown import Teardown
from tests.conftest import requires_docker

ENGINE_IMAGE = "postgres:18"
DATA_DIR = "/var/lib/postgresql/data"
READY = ["pg_isready", "-U", "postgres", "-q"]

# Postgres reserves superuser slots on top of max_connections, so the number of
# ordinary clients that get in is a few below this. That is the point: the test
# asserts refusals *happened* and were named, never a specific count.
MAX_CONNECTIONS = 32
ATTEMPTS = 48
IDLE_TIMEOUT_MS = 1000
CELL = "uc1/postgres/conn-lifecycle/r0/rep1"

pytestmark = [requires_docker, pytest.mark.slow]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def refusals(tmp_path_factory: pytest.TempPathFactory):
    """One provision cycle at max_connections=32, driven past it."""
    out = tmp_path_factory.mktemp("s18c")
    layout = RunLayout.for_run(new_run_id(), base=out / "runs")
    layout.create()
    run_id = layout.run_id
    teardown = Teardown(run_id)
    try:
        pin = resolve_image(ENGINE_IMAGE)
        container = provision(
            pin,
            ResourceEnvelope(cpuset=(2, 3, 4, 5), cpus=4.0, memory_bytes=2 * GIB),
            run_id,
            data_dir=DATA_DIR,
            container_port=5432,
            host_port=(port := _free_port()),
            env={"POSTGRES_PASSWORD": "dsel", "PGDATA": f"{DATA_DIR}/pgdata"},
            # Not a runtime knob: it takes a provision cycle, which is why this
            # provocation cannot ride along on another run.
            command=[
                "-c",
                f"max_connections={MAX_CONNECTIONS}",
                "-c",
                f"idle_in_transaction_session_timeout={IDLE_TIMEOUT_MS}",
                # `%e` puts the SQLSTATE in the log. Without it the engine's
                # own account is English prose and the attribution would be
                # locale-dependent.
                "-c",
                f"log_line_prefix={LOG_LINE_PREFIX}",
            ],
        )
        wait_healthy(container, READY)
        dsn = f"postgresql://postgres:dsel@127.0.0.1:{port}/postgres"

        writer = ShardWriter(layout.shards, "conn-0")
        # One event loop for the whole scenario. asyncpg connections are bound
        # to the loop that opened them, so a second `asyncio.run` could not
        # even close them.
        asyncio.run(_scenario(dsn, writer))
        # The engine's own account of the sessions *it* ended. The client
        # could not see these at all.
        from_log = engine_log_events(container.name, writer, cell=CELL)
        assert from_log, "the engine logged no terminations; %e may be missing"
        writer.close()
        return list(merge_records(find_shards(layout.shards)))
    finally:
        teardown.run()


async def _scenario(dsn: str, writer: ShardWriter) -> None:
    """Provoke both mechanisms, in the order that lets both happen.

    The idle-in-transaction reap goes first: it needs a free connection slot,
    and the `max_connections` staircase deliberately leaves none.
    """
    await _provoke_idle_in_transaction(dsn, writer)
    opened = await open_until_refused(dsn, writer, limit=ATTEMPTS, cell=CELL)
    for connection in opened:
        await connection.close()


async def _provoke_idle_in_transaction(dsn: str, writer: ShardWriter) -> None:
    """Hold a transaction open past the timeout and let the engine reap it.

    A different SQLSTATE from `max_connections`, and a genuinely different
    remedy -- fixing the client, where raising a limit would make it worse --
    which is why telling the two apart is the whole exercise.
    """
    import asyncpg

    from dsel.live.sampler.connections import Attempt, classify_exception, to_record

    connection = await asyncpg.connect(dsn)
    try:
        transaction = connection.transaction()
        await transaction.start()
        await connection.fetchval("SELECT 1")
        await asyncio.sleep(IDLE_TIMEOUT_MS / 1000.0 + 1.5)
        try:
            await connection.fetchval("SELECT 1")
        except Exception as exc:
            # Recorded for what it is: the client's view, which for a
            # server-initiated reap carries no SQLSTATE and no reason.
            sqlstate, message, number = classify_exception(exc)
            record = to_record(
                Attempt(ATTEMPTS, False, sqlstate, message, number), "postgres", CELL
            )
            writer.write(record.model_copy(update={"event": "reset"}))
    finally:
        with contextlib.suppress(Exception):
            await connection.close()


def test_the_engine_actually_refused(refusals) -> None:
    """Before attributing anything: the provocation has to have worked."""
    events = [r for r in refusals if isinstance(r, ConnectionEventRecord)]
    opened = [e for e in events if e.event == "opened"]
    failed = [e for e in events if e.event != "opened"]
    assert len(opened) + len(failed) >= ATTEMPTS
    assert failed, f"no refusals at max_connections={MAX_CONNECTIONS} over {ATTEMPTS} attempts"
    assert len(opened) < ATTEMPTS, "every attempt succeeded; the limit was not reached"


def test_every_refusal_is_attributed_to_a_named_mechanism(refusals) -> None:
    """The acceptance. Zero unknown."""
    result = attribute_records(refusals)
    print("\n" + result.table())
    assert result.fully_attributed, result.table()
    assert UNKNOWN not in result.by_mechanism
    assert result.by_mechanism.get("max_connections", 0) > 0


def test_the_table_discriminates_rather_than_returning_a_constant(refusals) -> None:
    """Two mechanisms provoked, two names out. A table that only ever sees one
    mechanism has not been shown to tell them apart."""
    result = attribute_records(refusals)
    assert len(result.by_mechanism) >= 2, result.table()
    assert "idle_in_transaction_session_timeout" in result.by_mechanism, result.table()


def test_the_client_alone_could_not_have_named_the_reap(refusals) -> None:
    """The finding this test exists to pin: for a session the *server* ends,
    the client's account carries no SQLSTATE and no reason. It arrives as
    `server_closed`, and only the engine's log says why."""
    events = [r for r in refusals if isinstance(r, ConnectionEventRecord)]
    client_side = [e for e in events if e.event == "reset" and e.sqlstate is None]
    assert client_side, "the client-side view of the reap was not recorded"
    assert all("closed" in (e.message or "").lower() for e in client_side), [
        e.message for e in client_side
    ]
    engine_side = [e for e in events if e.event == "reset" and e.sqlstate == "25P03"]
    assert engine_side, "the engine's log did not supply the reason the client lacked"
    assert "idle-in-transaction" in (engine_side[0].message or "")


def test_the_evidence_is_in_the_stream_not_the_conclusion(refusals) -> None:
    """The sampler records SQLSTATE and text; the naming happens afterwards,
    from the file. A record carrying its own conclusion would make the
    conclusion unreviewable."""
    events = [r for r in refusals if isinstance(r, ConnectionEventRecord)]
    refused = [e for e in events if e.event == "refused"]
    assert refused
    assert any(e.sqlstate == "53300" for e in refused), (
        "the engine's own code is what makes the attribution stable across "
        "versions and locales; it must be recorded verbatim"
    )
    assert all(not hasattr(e, "mechanism") for e in events)
