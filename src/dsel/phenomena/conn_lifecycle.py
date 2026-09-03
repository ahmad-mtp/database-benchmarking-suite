"""Attributing connection failures to named mechanisms (PLAN.md S16-S18c).

*Accept: a `max_connections=32` run attributes every refusal to a named
mechanism with zero `unknown` causes.*

"The database refused connections" is not a finding. It is the beginning of one,
and which of half a dozen mechanisms did the refusing changes the answer
completely:

* `max_connections` -- the engine's own limit. Raise it, or pool.
* `idle_in_transaction_session_timeout` -- the engine reaping a client that
  held a transaction open. Fix the client; raising the limit makes it worse.
* `ephemeral_ports` -- the *host* ran out of source ports, usually behind a
  pile of `TIME_WAIT`. Nothing to do with the engine at all.
* `listen_backlog` -- arrivals outran `accept()`. A burst problem, not a
  capacity one.
* `connection_reset` / `server_closed` -- something in the middle, or the
  engine dying.

An `unknown` is therefore a failure of the harness, not a category of event:
it means a refusal happened and the report cannot say why, which is exactly
the state a decision must not be taken from. The acceptance criterion is zero
of them, and this module is built so that an unmatched message is *loudly*
unknown rather than quietly bucketed into the nearest neighbour.

Attribution is by SQLSTATE first and text second. SQLSTATE is stable across
versions and locales; the message text is neither, and a matcher that led with
it would silently stop working on a translated server.

Reads `metrics.ndjson` and nothing else.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from dsel.live.schema import AnyRecord, ConnectionEventRecord

UNKNOWN = "unknown"

# PostgreSQL error codes, appendix A. Stable across versions and locales,
# which is why they are tried first.
SQLSTATE_MECHANISMS: dict[str, str] = {
    "53300": "max_connections",  # too_many_connections
    "53400": "configuration_limit_exceeded",
    "57P01": "server_shutdown",  # admin_shutdown
    "57P02": "server_crash",  # crash_shutdown
    "57P03": "server_starting",  # cannot_connect_now
    "57P04": "database_dropped",
    "25P03": "idle_in_transaction_session_timeout",
    "57014": "statement_timeout",
    "08006": "connection_failure",
    "08001": "client_cannot_connect",
    "28000": "authentication",
    "28P01": "authentication",
    "3D000": "database_missing",
}

# Tried only when there is no SQLSTATE -- which is the case for every failure
# that never reached the engine, and those are the interesting host-side ones.
TEXT_MECHANISMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("max_connections", re.compile(r"too many clients", re.I)),
    (
        "idle_in_transaction_session_timeout",
        re.compile(r"idle-in-transaction (session )?timeout", re.I),
    ),
    ("idle_session_timeout", re.compile(r"idle session timeout", re.I)),
    ("ephemeral_ports", re.compile(r"cannot assign requested address|EADDRNOTAVAIL", re.I)),
    ("connection_reset", re.compile(r"connection reset by peer|ECONNRESET", re.I)),
    ("listen_backlog", re.compile(r"connection refused|ECONNREFUSED", re.I)),
    ("connect_timeout", re.compile(r"timeout|timed out", re.I)),
    ("server_closed", re.compile(r"server closed the connection|connection is closed", re.I)),
    ("dns", re.compile(r"name or service not known|nodename nor servname", re.I)),
    ("tls", re.compile(r"ssl|tls", re.I)),
)

# Host-side failures, by OS errno, when neither of the above applies.
ERRNO_MECHANISMS: dict[int, str] = {
    48: "ephemeral_ports",  # EADDRINUSE
    49: "ephemeral_ports",  # EADDRNOTAVAIL
    54: "connection_reset",  # ECONNRESET
    61: "listen_backlog",  # ECONNREFUSED
    60: "connect_timeout",  # ETIMEDOUT
}

FAILURE_EVENTS = frozenset({"refused", "reset", "timeout"})


def attribute(record: ConnectionEventRecord) -> str:
    """Name the mechanism behind one connection event.

    SQLSTATE, then errno, then text. An unmatched event returns `unknown`
    rather than a best guess: a wrong name is worse than an admitted gap,
    because a wrong name gets acted on.
    """
    if record.sqlstate:
        named = SQLSTATE_MECHANISMS.get(record.sqlstate)
        if named:
            return named
    if record.errno is not None:
        named = ERRNO_MECHANISMS.get(record.errno)
        if named:
            return named
    text = record.message or ""
    for mechanism, pattern in TEXT_MECHANISMS:
        if pattern.search(text):
            return mechanism
    return UNKNOWN


@dataclass(frozen=True, slots=True)
class Attribution:
    """Every connection event, counted by mechanism."""

    opened: int
    failures: int
    by_mechanism: dict[str, int]
    unattributed: tuple[ConnectionEventRecord, ...]

    @property
    def unknown(self) -> int:
        return self.by_mechanism.get(UNKNOWN, 0)

    @property
    def fully_attributed(self) -> bool:
        """Whether every failure has a named mechanism.

        The acceptance criterion. Not "mostly attributed": a single unexplained
        refusal in a report is a refusal somebody will explain for themselves,
        and they will guess.
        """
        return self.failures > 0 and self.unknown == 0

    def table(self) -> str:
        lines = [
            f"{'mechanism':<40} {'events':>8}",
            f"{'-' * 40} {'-' * 8}",
        ]
        for mechanism, count in sorted(
            self.by_mechanism.items(), key=lambda item: (-item[1], item[0])
        ):
            lines.append(f"{mechanism:<40} {count:>8}")
        lines += [
            f"{'-' * 40} {'-' * 8}",
            f"{'opened':<40} {self.opened:>8}",
            f"{'failures':<40} {self.failures:>8}",
            f"{'unattributed':<40} {self.unknown:>8}",
        ]
        for record in self.unattributed[:5]:
            lines.append(
                f"  unattributed: sqlstate={record.sqlstate} errno={record.errno} "
                f"message={(record.message or '')[:90]!r}"
            )
        return "\n".join(lines)


def attribute_records(records: Iterable[AnyRecord]) -> Attribution:
    """Attribute every connection failure in a metrics stream."""
    opened = 0
    failures = 0
    counts: dict[str, int] = {}
    unattributed: list[ConnectionEventRecord] = []
    for record in records:
        if not isinstance(record, ConnectionEventRecord):
            continue
        if record.event == "opened":
            opened += 1
            continue
        if record.event not in FAILURE_EVENTS:
            continue
        failures += 1
        mechanism = attribute(record)
        counts[mechanism] = counts.get(mechanism, 0) + 1
        if mechanism == UNKNOWN:
            unattributed.append(record)
    return Attribution(
        opened=opened,
        failures=failures,
        by_mechanism=counts,
        unattributed=tuple(unattributed),
    )
