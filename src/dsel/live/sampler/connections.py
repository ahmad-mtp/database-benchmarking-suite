"""Connection-attempt sampler (PLAN.md S16-S18c).

Opens connections and records what happened to each one. Writes records and
derives nothing: the SQLSTATE, the error text and the OS errno go into the
stream verbatim, and `phenomena/conn_lifecycle.py` names the mechanism
afterwards, from the file. A sampler that wrote its own conclusion would make
the conclusion unreviewable, and the whole point of attributing a refusal is
that somebody can check the attribution.
"""

from __future__ import annotations

import errno as errno_module
import re
import time
from dataclasses import dataclass
from typing import Any

from dsel.live.ndjson import ShardWriter, now_ms
from dsel.live.schema import ConnectionEventRecord

MESSAGE_LIMIT = 400


@dataclass(frozen=True, slots=True)
class Attempt:
    """What happened when one connection was attempted."""

    attempt: int
    ok: bool
    sqlstate: str | None = None
    message: str | None = None
    errno: int | None = None


def classify_exception(exc: BaseException) -> tuple[str | None, str, int | None]:
    """Pull the evidence out of an exception without interpreting it.

    `sqlstate` comes from asyncpg's own attribute when the engine answered;
    `errno` from the OS error when it did not. Both may be absent, and then
    only the text is left -- which is exactly the case the attribution table
    has to be able to fail loudly on.
    """
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate is None:
        sqlstate = getattr(getattr(exc, "__cause__", None), "sqlstate", None)
    number = getattr(exc, "errno", None)
    if number is None and isinstance(exc, OSError):
        number = exc.errno
    if number is None:
        cause = exc.__cause__
        if isinstance(cause, OSError):
            number = cause.errno
    text = str(exc).strip() or exc.__class__.__name__
    if number is not None and errno_module.errorcode.get(number):
        text = f"{text} [{errno_module.errorcode[number]}]"
    return (str(sqlstate) if sqlstate else None, text[:MESSAGE_LIMIT], number)


def to_record(
    attempt: Attempt, engine: str, cell: str | None, age_s: float | None = None
) -> ConnectionEventRecord:
    return ConnectionEventRecord(
        t_ms=now_ms(),
        w="",
        seq=0,
        cell=cell,
        engine=engine,
        event="opened" if attempt.ok else "refused",
        attempt=attempt.attempt,
        sqlstate=attempt.sqlstate,
        message=attempt.message,
        errno=attempt.errno,
        age_s=age_s,
    )


async def open_until_refused(
    dsn: str,
    writer: ShardWriter,
    *,
    limit: int,
    engine: str = "postgres",
    cell: str | None = None,
) -> list[Any]:
    """Open connections one at a time until `limit` attempts have been made.

    Every attempt is recorded, successes included: a refusal count means
    nothing without the number of connections that were already up when it
    happened, and that number is what turns "it refused" into "it refused at
    32".
    """
    import asyncpg

    opened: list[Any] = []
    for index in range(limit):
        started = time.monotonic()
        try:
            connection = await asyncpg.connect(dsn, timeout=10.0)
        except Exception as exc:
            sqlstate, message, number = classify_exception(exc)
            writer.write(
                to_record(
                    Attempt(index, False, sqlstate, message, number),
                    engine,
                    cell,
                    time.monotonic() - started,
                )
            )
            continue
        opened.append(connection)
        writer.write(to_record(Attempt(index, True), engine, cell, time.monotonic() - started))
    return opened


# --- the engine's own account -----------------------------------------------
#
# A client cannot always see why its connection ended. When Postgres reaps a
# session -- an idle-in-transaction timeout, an administrative termination, a
# crash -- it writes a FATAL to its log and closes the socket; by the time the
# client's next statement goes out there is nothing left to receive an error
# *from*, and the client sees only "connection closed". Measured: an
# idle-in-transaction reap provoked deliberately arrived client-side as
# `server_closed`, with no SQLSTATE and no reason.
#
# So the engine's log is a second evidence source, and for server-initiated
# terminations it is the *only* one. It has to be asked for: the SQLSTATE is
# not in the default `log_line_prefix`, and without it the attribution is back
# to matching English text.

LOG_LINE_PREFIX = "%m [%p] %e "
"""Provision the engine with this so its log carries the SQLSTATE (`%e`)."""

FATAL_LINE = re.compile(
    r"^(?P<stamp>\S+ \S+ \S+) \[(?P<pid>\d+)\] (?P<sqlstate>[0-9A-Z]{5}) "
    r"(?P<severity>FATAL|ERROR):\s+(?P<message>.*)$"
)


def parse_engine_log(text: str) -> list[tuple[str, str, int]]:
    """`(sqlstate, message, pid)` for every FATAL/ERROR the engine logged.

    Requires `log_line_prefix` to include `%e`. A log without it parses to
    nothing, which is the right outcome: silently falling back to matching
    message text would make the attribution locale-dependent without saying so.
    """
    out: list[tuple[str, str, int]] = []
    for line in text.splitlines():
        match = FATAL_LINE.match(line.strip())
        if match:
            out.append(
                (
                    match["sqlstate"],
                    match["message"].strip()[:MESSAGE_LIMIT],
                    int(match["pid"]),
                )
            )
    return out


def read_engine_log(container: str, since: str | None = None) -> str:
    import subprocess

    args = ["docker", "logs"]
    if since:
        args += ["--since", since]
    args.append(container)
    result = subprocess.run(args, capture_output=True, text=True, timeout=60, check=False)
    return result.stdout + result.stderr


def engine_log_events(
    container: str,
    writer: ShardWriter,
    *,
    engine: str = "postgres",
    cell: str | None = None,
    since: str | None = None,
) -> int:
    """Write one `connection_event` per session the *engine* ended.

    Only terminations are emitted -- a statement-level ERROR is not a
    connection event, and folding the two together would inflate the refusal
    count with things that never touched a connection.
    """
    written = 0
    for sqlstate, message, pid in parse_engine_log(read_engine_log(container, since)):
        if sqlstate not in SERVER_TERMINATION_SQLSTATES:
            continue
        writer.write(
            ConnectionEventRecord(
                t_ms=now_ms(),
                w="",
                seq=0,
                cell=cell,
                engine=engine,
                event="reset",
                attempt=pid,
                sqlstate=sqlstate,
                message=message,
            )
        )
        written += 1
    return written


SERVER_TERMINATION_SQLSTATES = frozenset(
    {
        "25P03",  # idle_in_transaction_session_timeout
        "57P01",  # admin_shutdown
        "57P02",  # crash_shutdown
        "57P04",  # database_dropped
        "53300",  # too_many_connections, as the server saw it
        "08006",  # connection_failure
    }
)
