"""metrics.ndjson schema and drift check (PLAN.md S6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from dsel.live.schema import (
    RECORD_ADAPTER,
    RECORD_KINDS,
    ContainerRecord,
    LatencyWindowRecord,
    PhaseRecord,
    ValidityRecord,
    json_schema,
)

COMMITTED = Path(__file__).resolve().parents[2] / "schema" / "metrics.schema.json"


def test_committed_schema_matches_the_models() -> None:
    """The drift check: models changed without regenerating the schema."""
    assert COMMITTED.is_file(), f"{COMMITTED} is missing"
    on_disk = json.loads(COMMITTED.read_text())
    assert on_disk == json_schema(), (
        "schema/metrics.schema.json is out of date. Regenerate it:\n"
        '  uv run python -c "import json;from pathlib import Path;'
        "from dsel.live.schema import json_schema;"
        "Path('schema/metrics.schema.json').write_text("
        'json.dumps(json_schema(),indent=2,sort_keys=True)+chr(10))"'
    )


@pytest.mark.parametrize("kind", RECORD_KINDS)
def test_every_declared_kind_is_in_the_union(kind: str) -> None:
    schema = json_schema()
    text = json.dumps(schema)
    assert f'"{kind}"' in text


def test_unknown_kind_is_refused() -> None:
    """An unknown record kind must fail, not be silently dropped."""
    with pytest.raises(ValidationError):
        RECORD_ADAPTER.validate_python({"kind": "wat", "t_ms": 1, "w": "a", "seq": 0})


def test_undeclared_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        RECORD_ADAPTER.validate_python(
            {
                "kind": "phase",
                "t_ms": 1,
                "w": "a",
                "seq": 0,
                "phase": "measure",
                "event": "begin",
                "surprise": 1,
            }
        )


def test_negative_seq_is_refused() -> None:
    with pytest.raises(ValidationError):
        PhaseRecord(t_ms=1, w="a", seq=-1, phase="measure", event="begin")


def test_latency_window_is_permanently_stamped_as_an_estimate() -> None:
    """A scrape bucket must never become a reported percentile."""
    record = LatencyWindowRecord(
        t_ms=1, w="a", seq=0, window_ms=1000, op="read", count=10, rate_per_s=10.0
    )
    assert record.estimate_only is True
    with pytest.raises(ValidationError):
        LatencyWindowRecord(
            t_ms=1,
            w="a",
            seq=0,
            window_ms=1000,
            op="read",
            count=10,
            rate_per_s=10.0,
            estimate_only=False,  # type: ignore[arg-type]
        )


def test_blkio_carries_a_trust_flag() -> None:
    """exp04: BlockIO reported 8.19 kB for 6.44 GB of writes on a bind mount."""
    assert "blkio_trusted" in ContainerRecord.model_fields


def test_validity_verdicts_include_the_driver_bound_case() -> None:
    """INCONCLUSIVE_DRIVER_BOUND is distinct from INVALID, deliberately."""
    for verdict in ("OK", "FLAG", "INVALID", "INCONCLUSIVE_DRIVER_BOUND"):
        ValidityRecord(t_ms=1, w="a", seq=0, gate="driver_cpu_pct", verdict=verdict)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ValidityRecord(t_ms=1, w="a", seq=0, gate="g", verdict="probably_fine")  # type: ignore[arg-type]


def test_records_round_trip_through_json() -> None:
    original = ContainerRecord(
        t_ms=1700000000000,
        w="sampler-0",
        seq=7,
        container="engine",
        cpu_usage_usec=123,
        memory_current=456,
    )
    restored = RECORD_ADAPTER.validate_python(json.loads(original.model_dump_json()))
    assert restored == original
