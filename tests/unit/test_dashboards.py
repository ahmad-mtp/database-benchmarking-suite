"""The provisioned dashboards (PLAN.md S8b).

Two of S8b's three acceptance clauses can be decided without a container, and
those are checked here so a typo in an expression fails in a second rather than
after a stack has come up:

* every expression must name a metric the exporter actually publishes;
* every latency panel must carry the "within-window estimate" annotation
  *visibly*, not buried in a tooltip.

The third clause -- that each panel resolves against a real Prometheus with no
"No data" -- needs the stack, and lives in the integration suite.
"""

from __future__ import annotations

import re

import pytest

from dsel.compose.observability import (
    DEFAULT_EXPORTER_PORT,
    StackPorts,
    dashboards,
    panel_targets,
    render_config,
)
from dsel.live.exporter import PREFIX, StateExporter
from dsel.live.schema import (
    AppRecord,
    BackendRecord,
    ContainerRecord,
    EngineRecord,
    LatencyWindowRecord,
    NetRecord,
    PhaseRecord,
    PoolRecord,
    ValidityRecord,
)
from dsel.live.state import ScreenState, apply

ESTIMATE_PHRASE = "within-window estimate, not the reported figure"
METRIC_PATTERN = re.compile(rf"\b{PREFIX}_[a-z0-9_]+\b")
EXPECTED_DASHBOARDS = {"now", "connections", "joins", "validity"}


def _populated_state() -> ScreenState:
    """A state touching every record kind, so every family is published."""
    cell = "uc1/postgres/oltp-mixed/r400/rep1"
    records = [
        PhaseRecord(t_ms=1, w="g", seq=0, cell=cell, phase="warmup", event="begin"),
        PhaseRecord(t_ms=2, w="g", seq=1, cell=cell, phase="warmup", event="end"),
        PhaseRecord(t_ms=3, w="g", seq=2, cell=cell, phase="measure", event="begin"),
        LatencyWindowRecord(
            t_ms=4,
            w="d",
            seq=0,
            cell=cell,
            window_ms=100,
            op="order_join_report",
            count=10,
            errors=1,
            rate_per_s=100.0,
            p50_us=1.0,
            p99_us=2.0,
            max_us=3.0,
        ),
        ContainerRecord(
            t_ms=5,
            w="s",
            seq=0,
            cell=cell,
            container="dsel-engine",
            cpu_usage_usec=1,
            cpu_throttled_usec=2,
            memory_current=3,
            memory_max=4,
            pids_current=5,
        ),
        PoolRecord(
            t_ms=6,
            w="s",
            seq=1,
            cell=cell,
            pool="app->postgres",
            size=32,
            in_use=4,
            idle=28,
            waiting=0,
            acquire_wait_us_p99=9.0,
            timeouts=0,
        ),
        AppRecord(
            t_ms=7,
            w="a",
            seq=0,
            cell=cell,
            endpoint="GET /reports/join",
            count=1,
            errors=0,
            app_recv_to_db_start_us=1.0,
            db_us=2.0,
            db_end_to_send_us=3.0,
            cpu_pct=18.0,
        ),
        EngineRecord(
            t_ms=8,
            w="e",
            seq=0,
            cell=cell,
            engine="postgres",
            metrics={
                "backends": 20,
                "temp_bytes_total": 1,
                "blocks_read_total": 2,
                "blocks_hit_total": 3,
                "cache_hit_ratio": 0.9,
                "tup_returned_total": 4,
                "tup_fetched_total": 5,
                "locks_waiting": 0,
            },
        ),
        BackendRecord(
            t_ms=9,
            w="e",
            seq=1,
            cell=cell,
            engine="postgres",
            backend_id="pid-1",
            state="active",
            wait_event_type="Lock",
            vm_rss_bytes=10,
        ),
        NetRecord(
            t_ms=10,
            w="e",
            seq=2,
            cell=cell,
            scope="engine",
            established=20,
            time_wait=1,
            syn_recv=0,
            listen_overflows=0,
            listen_drops=0,
            ephemeral_used=21,
            ephemeral_available=28_232,
        ),
        ValidityRecord(
            t_ms=11,
            w="g",
            seq=3,
            cell=cell,
            gate="driver_cpu",
            verdict="FLAG",
            observed=61.4,
            limit=70.0,
        ),
    ]
    state = ScreenState()
    for record in records:
        state = apply(state, record)
    return state


def _published_metric_names() -> set[str]:
    families = StateExporter().families(_populated_state())
    return {family.name for family in families}


@pytest.fixture(scope="module")
def boards() -> dict[str, dict[str, object]]:
    return dashboards()


def test_all_four_dashboards_are_provisioned(boards: dict[str, dict[str, object]]) -> None:
    assert set(boards) == EXPECTED_DASHBOARDS


def test_every_expression_names_a_published_metric(
    boards: dict[str, dict[str, object]],
) -> None:
    """A dashboard referring to a metric nobody emits is a panel that will never
    resolve. Catching that here costs a second; catching it against a live
    Prometheus costs a stack."""
    published = _published_metric_names()
    missing: list[str] = []
    for name, board in boards.items():
        for title, expr in panel_targets(board):
            for metric in METRIC_PATTERN.findall(expr):
                if metric not in published:
                    missing.append(f"{name}/{title}: {metric}")
    assert not missing, "expressions naming metrics the exporter never emits:\n" + "\n".join(
        missing
    )


def test_every_latency_panel_carries_the_estimate_annotation(
    boards: dict[str, dict[str, object]],
) -> None:
    """S8b requires it *visible*. A description is a hover tooltip, so the
    phrase has to be in the title as well -- a caveat nobody sees is not a
    caveat, and a scrape bucket becoming a reported percentile is exactly the
    mistake it exists to prevent."""
    unannotated: list[str] = []
    for name, board in boards.items():
        panels = board["panels"]
        assert isinstance(panels, list)
        for panel in panels:
            exprs = [t.get("expr", "") for t in panel.get("targets", [])]
            if not any("estimate_microseconds" in expr for expr in exprs):
                continue
            title = str(panel.get("title", ""))
            description = str(panel.get("description", ""))
            if ESTIMATE_PHRASE not in title:
                unannotated.append(f"{name}/{title}: title")
            if "WITHIN-WINDOW ESTIMATE" not in description:
                unannotated.append(f"{name}/{title}: description")
    assert not unannotated, "latency panels without a visible annotation:\n" + "\n".join(
        unannotated
    )


def test_at_least_one_panel_joins_step_and_repeat_from_cell_info(
    boards: dict[str, dict[str, object]],
) -> None:
    """PLAN.md is explicit that step and repeat join rather than become labels.
    A dashboard that never performs the join would leave the rule untested."""
    joins = [
        expr
        for board in boards.values()
        for _, expr in panel_targets(board)
        if "dsbench_cell_info" in expr and "group_left" in expr
    ]
    assert joins, "no panel demonstrates the cell_info join"
    assert any("step" in expr and "repeat" in expr for expr in joins)


def test_step_and_repeat_are_labels_on_cell_info_alone() -> None:
    """The rule the join exists to serve, checked against the exporter itself."""
    families = StateExporter().families(_populated_state())
    offenders = [
        family.name
        for family in families
        if family.name != f"{PREFIX}_cell_info"
        for sample in family.samples
        if {"step", "repeat"} & set(sample.labels)
    ]
    assert not offenders, f"step/repeat leaked onto {sorted(set(offenders))}"


def test_every_dashboard_targets_the_provisioned_datasource(
    boards: dict[str, dict[str, object]],
) -> None:
    for name, board in boards.items():
        panels = board["panels"]
        assert isinstance(panels, list)
        for panel in panels:
            assert panel["datasource"]["uid"] == "dsel-prometheus", name


def test_the_rendered_prometheus_config_carries_the_port_and_the_stamps() -> None:
    """The port lands in the file that is seeded, not in an environment
    variable read somewhere else."""
    rendered = render_config("run-1", StackPorts(exporter=DEFAULT_EXPORTER_PORT))
    config = rendered["prometheus/prometheus.yml"]
    assert f"host.docker.internal:{DEFAULT_EXPORTER_PORT}" in config
    assert 'run_id: "run-1"' in config
    assert "profile: local" in config
    assert 'reportable: "false"' in config
    assert "$" not in config, "an unsubstituted placeholder would fail silently"
    assert "grafana/provisioning/datasources/prometheus.yaml" in rendered
    assert "grafana/dashboards/now.json" in rendered


def test_the_compose_file_bind_mounts_nothing() -> None:
    """D7. A config bind mount is still a VirtioFS mount for the life of a run."""
    from dsel.compose.observability import COMPOSE_FILE

    text = COMPOSE_FILE.read_text(encoding="utf-8")
    volume_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("- ") and ":" in line and "/" in line.split(":")[0]
    ]
    assert not volume_lines, f"host paths mounted into containers: {volume_lines}"
