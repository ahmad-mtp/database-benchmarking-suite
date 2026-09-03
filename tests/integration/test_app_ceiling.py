"""S13 acceptance: the app tier's ceiling, and its gate tripping.

*PLAN.md S13:* "`/noop` ceiling is measured and written to the manifest;
driving past it makes the `app_tier_cpu_pct` gate fire and stamp
`INVALID(app_tier_saturated)` -- the gate is demonstrated tripping, not merely
implemented."

The failure this prevents is the expensive silent one. If the app tier
saturates before the engine does, every candidate engine returns the tier's
ceiling, the differences vanish, and the harness reports with confidence that
the choice does not matter. Nothing looks wrong -- the numbers are just all the
same. So the ceiling is measured against `/noop`, with no pool and no engine
behind it, before PATH B is wired at all.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from dsel.app.ceiling import measure_ceiling, wait_ready
from dsel.app.stack import build_app_image, copy_shards, start_app
from dsel.live.merge import find_shards, merge_records
from dsel.live.schema import AppRecord, ValidityRecord
from dsel.metrics.validity import (
    APP_CPU_LIMIT_PCT,
    GATE_APP_CPU,
    LOCAL_CEILING_FRACTION,
    REASON_APP_SATURATED,
    app_cpu_gate,
)
from dsel.runtime.paths import RunLayout, new_run_id
from dsel.runtime.teardown import Teardown
from tests.conftest import requires_docker

# Half a core, one worker. The quota is the denominator of app_tier_cpu_pct, so
# a smaller one puts saturation within reach of a driver that is itself Python:
# measured, the tier serves ~1740 /noop per second at 32% of a full core, which
# would need ~3800/s to trip a 70% gate. At half a core the same load reads 64%
# and the gate is reachable inside a test that finishes.
APP_CPUS = 0.5
APP_WORKERS = 1
RATES = (200.0, 500.0, 900.0, 1400.0, 2000.0, 2800.0)
DURATION_S = 3.0

pytestmark = [requires_docker, pytest.mark.slow]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def ceiling(tmp_path_factory: pytest.TempPathFactory):
    out = tmp_path_factory.mktemp("s13")
    layout = RunLayout.for_run(new_run_id(), base=out / "runs")
    layout.create()
    run_id = layout.run_id
    teardown = Teardown(run_id)
    try:
        image = build_app_image()
        port = _free_port()
        container = start_app(
            image,
            run_id,
            port,
            cell="uc1/app/noop/r0/rep1",
            cpus=APP_CPUS,
            workers=APP_WORKERS,
        )
        wait_ready("127.0.0.1", port)
        result = measure_ceiling(
            layout.root,
            "127.0.0.1",
            port,
            RATES,
            duration_s=DURATION_S,
            workers=4,
        )
        shards = copy_shards(container.name, out / "app")
        return result, layout, shards, port
    finally:
        teardown.run()


def test_the_tier_reports_its_cpu_against_its_own_quota(ceiling) -> None:
    """An unlimited container has no denominator, and a gate with no number
    passes by never being able to fire."""
    result, _, _, _ = ceiling
    assert result.cpu_pct_by_rate, "no CPU readings were taken"
    assert all(cpu > 0 for _, cpu in result.cpu_pct_by_rate), result.table()


def test_the_ceiling_is_measured_and_rises_with_load(ceiling) -> None:
    result, _, _, _ = ceiling
    print("\n" + result.table())
    rates = [rate for rate, _ in result.cpu_pct_by_rate]
    cpus = [cpu for _, cpu in result.cpu_pct_by_rate]
    assert rates == list(RATES)
    assert cpus[-1] > cpus[0] * 2, f"CPU did not track load\n{result.table()}"
    # Sampled *during* each step and taken as the peak. Reading once after a
    # step ends mixes load with the ramp's own teardown idle, and the curve
    # came out non-monotonic -- 93.7% at 1400/s against 65.1% at 2800/s, with
    # the gate closing again above the ceiling it had already crossed.
    #
    # Monotonicity is only required *below* the limit. Above it the container
    # is throttled at its quota and cannot read higher, so the readings are
    # noise around a cap -- 92.3% then 88.1% here. What must hold above the
    # limit is that the gate never re-closes.
    below = [cpu for cpu in cpus if cpu <= APP_CPU_LIMIT_PCT]
    assert below == sorted(below), f"CPU fell as load rose below the gate\n{result.table()}"
    crossed = next(i for i, cpu in enumerate(cpus) if cpu > APP_CPU_LIMIT_PCT)
    assert all(cpu > APP_CPU_LIMIT_PCT for cpu in cpus[crossed:]), (
        f"the gate re-closed above the ceiling it had crossed\n{result.table()}"
    )


def test_driving_past_the_ceiling_trips_the_gate(ceiling) -> None:
    """The gate is demonstrated tripping, not merely implemented."""
    result, _, _, _ = ceiling
    saturation = result.saturation_rate_per_s
    assert saturation is not None, (
        f"the tier never crossed {APP_CPU_LIMIT_PCT:.0f}% within {RATES[-1]:.0f}/s; "
        f"the ceiling was not reached, so the gate was not demonstrated\n{result.table()}"
    )
    gate = result.gate_at(saturation)
    assert gate.verdict == "INVALID"
    assert gate.reason == REASON_APP_SATURATED
    assert gate.observed > APP_CPU_LIMIT_PCT
    # And below it the gate is closed, so it is a gate and not a constant.
    below = [rate for rate, cpu in result.cpu_pct_by_rate if cpu <= APP_CPU_LIMIT_PCT]
    assert below, f"every step tripped; there is no closed side\n{result.table()}"
    assert result.gate_at(below[0]).verdict == "OK"


def test_the_gate_is_stamped_into_the_runs_own_metrics_stream(ceiling) -> None:
    """A cell is invalidated by something that happened during it, so the
    record has to be in the stream the audit bundle hashes -- not derived
    afterwards from a number somebody kept on the side."""
    _, _, shards, _ = ceiling
    records = list(merge_records(find_shards(shards)))
    gates = [r for r in records if isinstance(r, ValidityRecord) and r.gate == GATE_APP_CPU]
    assert gates, f"no {GATE_APP_CPU} records in {shards}"
    invalid = [r for r in gates if r.verdict == "INVALID"]
    assert invalid, "the tier never stamped INVALID despite being driven past its ceiling"
    assert REASON_APP_SATURATED in (invalid[0].detail or "")
    assert any(r.verdict == "OK" for r in gates), "the gate never read OK either"


def test_the_tier_recorded_its_own_spans(ceiling) -> None:
    """`/noop` has no engine, so its db interval must be zero by construction --
    that is what makes the ceiling a statement about the tier alone."""
    _, _, shards, _ = ceiling
    app_records = [
        r
        for r in merge_records(find_shards(shards))
        if isinstance(r, AppRecord) and r.endpoint == "/noop"
    ]
    assert app_records, "the tier recorded no spans"
    assert all((r.db_us or 0.0) == 0.0 for r in app_records)
    assert sum(r.count for r in app_records) > 1000
    # A CPU reading needs a previous sample to difference against, so the very
    # first window of each worker legitimately has none. That must stay `None`
    # rather than becoming a 0.0 the gate would happily pass.
    missing = [r for r in app_records if r.cpu_pct is None]
    assert len(missing) <= APP_WORKERS, f"{len(missing)} records without a CPU reading"
    assert any(r.cpu_pct is not None for r in app_records)


def test_the_ceiling_is_written_to_the_manifest(ceiling, tmp_path: Path) -> None:
    """S13 asks for it in the manifest: PATH B is scheduled against this number
    at S14, so it has to survive the run that measured it."""
    result, layout, _, _ = ceiling
    from dsel.audit.models import AppCeilingRecord

    record = AppCeilingRecord(
        noop_saturation_rate_per_s=result.saturation_rate_per_s,
        noop_max_delivered_rate_per_s=result.max_delivered_rate_per_s,
        cpu_limit_pct=APP_CPU_LIMIT_PCT,
        path_b_rate_per_s=result.path_b_rate_per_s,
        ceiling_fraction=LOCAL_CEILING_FRACTION,
        app_cpus=APP_CPUS,
        app_workers=APP_WORKERS,
    )
    path = tmp_path / "manifest.json"
    path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    loaded = json.loads(path.read_text())
    assert loaded["noop_saturation_rate_per_s"] == result.saturation_rate_per_s
    assert loaded["path_b_rate_per_s"] == pytest.approx(
        result.saturation_rate_per_s * LOCAL_CEILING_FRACTION
    )
    print(
        f"\n  ceiling {result.saturation_rate_per_s:.0f}/s at {APP_CPUS} core, "
        f"{APP_WORKERS} worker -> PATH B may run at {result.path_b_rate_per_s:.0f}/s"
    )


def test_the_cpu_endpoint_and_the_stream_agree(ceiling) -> None:
    """The gate fires on the number an operator can also read."""
    result, _, shards, _ = ceiling
    app_records = [r for r in merge_records(find_shards(shards)) if isinstance(r, AppRecord)]
    stream_max = max(r.cpu_pct or 0.0 for r in app_records)
    probe_max = max(cpu for _, cpu in result.cpu_pct_by_rate)
    assert app_cpu_gate(stream_max).verdict == app_cpu_gate(probe_max).verdict
