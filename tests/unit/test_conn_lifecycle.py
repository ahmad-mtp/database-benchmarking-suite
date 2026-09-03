"""Refusal attribution (PLAN.md S16-S18c).

*Accept: a `max_connections=32` run attributes every refusal to a named
mechanism with zero `unknown` causes.*

An `unknown` is not a category of event, it is a failure of the harness: a
refusal happened and the report cannot say why, which is the state a decision
must not be taken from. These tests pin the table down without needing an
engine; the live provocation is in the integration suite.
"""

from __future__ import annotations

import pytest

from dsel.live.sampler.connections import classify_exception
from dsel.live.schema import ConnectionEventRecord
from dsel.phenomena.conn_lifecycle import UNKNOWN, attribute, attribute_records


def event(**kwargs: object) -> ConnectionEventRecord:
    base: dict[str, object] = {
        "t_ms": 1,
        "w": "c",
        "seq": 0,
        "engine": "postgres",
        "event": "refused",
    }
    return ConnectionEventRecord(**{**base, **kwargs})  # type: ignore[arg-type]


def test_sqlstate_wins_over_text() -> None:
    """SQLSTATE is stable across versions and locales; the message is neither.
    A matcher that led with text would silently stop working on a translated
    server, and 'silently' is the part that matters."""
    record = event(sqlstate="53300", message="algo sobre demasiados clientes")
    assert attribute(record) == "max_connections"


@pytest.mark.parametrize(
    ("sqlstate", "mechanism"),
    [
        ("53300", "max_connections"),
        ("57P01", "server_shutdown"),
        ("57P03", "server_starting"),
        ("25P03", "idle_in_transaction_session_timeout"),
        ("28P01", "authentication"),
    ],
)
def test_known_sqlstates_are_named(sqlstate: str, mechanism: str) -> None:
    assert attribute(event(sqlstate=sqlstate)) == mechanism


@pytest.mark.parametrize(
    ("message", "mechanism"),
    [
        ("FATAL:  sorry, too many clients already", "max_connections"),
        (
            "terminating connection due to idle-in-transaction timeout",
            "idle_in_transaction_session_timeout",
        ),
        ("[Errno 49] Cannot assign requested address", "ephemeral_ports"),
        ("Connection reset by peer", "connection_reset"),
        ("Connection refused", "listen_backlog"),
        ("server closed the connection unexpectedly", "server_closed"),
    ],
)
def test_text_is_used_when_the_engine_never_answered(message: str, mechanism: str) -> None:
    """Every host-side failure arrives without a SQLSTATE, and those are the
    ones that have nothing to do with the engine -- which is exactly why they
    have to be told apart from the ones that do."""
    assert attribute(event(message=message)) == mechanism


@pytest.mark.parametrize(
    ("number", "mechanism"), [(49, "ephemeral_ports"), (61, "listen_backlog")]
)
def test_errno_is_used_when_there_is_no_text_to_match(number: int, mechanism: str) -> None:
    assert attribute(event(errno=number, message="")) == mechanism


def test_an_unmatched_failure_is_loudly_unknown() -> None:
    """Not bucketed into the nearest neighbour. A wrong name gets acted on."""
    record = event(message="the flux capacitor declined")
    assert attribute(record) == UNKNOWN
    result = attribute_records([record])
    assert result.unknown == 1
    assert not result.fully_attributed
    assert result.unattributed == (record,)
    assert "flux capacitor" in result.table()


def test_successes_are_counted_separately_from_failures() -> None:
    """A refusal count means nothing without how many were already up: that is
    what turns "it refused" into "it refused at 32"."""
    records = [event(event="opened") for _ in range(32)] + [
        event(sqlstate="53300") for _ in range(8)
    ]
    result = attribute_records(records)
    assert result.opened == 32
    assert result.failures == 8
    assert result.by_mechanism == {"max_connections": 8}
    assert result.fully_attributed


def test_no_failures_at_all_is_not_full_attribution() -> None:
    """A run that never provoked anything has not demonstrated the gate. The
    criterion is that every refusal is named, and zero refusals names nothing."""
    result = attribute_records([event(event="opened")])
    assert not result.fully_attributed


def test_the_exception_classifier_keeps_evidence_and_adds_nothing() -> None:
    sqlstate, message, number = classify_exception(OSError(61, "Connection refused"))
    assert sqlstate is None
    assert number == 61
    assert "Connection refused" in message
    assert "ECONNREFUSED" in message, (
        "the symbolic name helps a reader, the number alone does not"
    )
