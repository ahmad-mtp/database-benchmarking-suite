"""docker events parsing (PLAN.md S7)."""

from __future__ import annotations

import json

from dsel.runtime.events import DockerEvent, _parse


def event(action: str, **attrs: object) -> str:
    return json.dumps(
        {
            "Action": action,
            "timeNano": 1_700_000_000_000_000_000,
            "Actor": {"Attributes": {"name": "dsel-engine", **attrs}},
        }
    )


def test_oom_is_fatal() -> None:
    parsed = _parse(event("oom"))
    assert parsed is not None and parsed.action == "oom" and parsed.is_fatal


def test_nonzero_die_is_fatal() -> None:
    parsed = _parse(event("die", exitCode="137"))
    assert parsed is not None and parsed.exit_code == 137 and parsed.is_fatal


def test_clean_die_is_not_fatal() -> None:
    """Teardown produces exit 0; that must not invalidate a cell."""
    parsed = _parse(event("die", exitCode="0"))
    assert parsed is not None and not parsed.is_fatal


def test_health_status_is_split_out() -> None:
    parsed = _parse(event("health_status: healthy"))
    assert parsed is not None
    assert parsed.action == "health_status"
    assert parsed.health_status == "healthy"
    assert not parsed.is_fatal


def test_timestamp_is_milliseconds() -> None:
    parsed = _parse(event("oom"))
    assert parsed is not None and parsed.t_ms == 1_700_000_000_000


def test_unparseable_line_returns_none() -> None:
    assert _parse("not json at all") is None


def test_container_name_falls_back_to_id() -> None:
    payload = json.dumps({"Action": "oom", "timeNano": 0, "id": "abcdef1234567890"})
    parsed = _parse(payload)
    assert parsed is not None and parsed.container == "abcdef123456"


def test_is_fatal_ignores_unknown_exit_code() -> None:
    assert not DockerEvent(t_ms=0, action="die", container="c").is_fatal
