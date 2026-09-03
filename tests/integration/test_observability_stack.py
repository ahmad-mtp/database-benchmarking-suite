"""S8b acceptance: Prometheus and Grafana against a real run.

*PLAN.md S8b:* "`count({__name__=~"dsbench_.*"})` stays <=500 for a full run;
every panel in all four dashboards resolves against a completed run with no
'No data'; every latency panel carries a visible 'within-window estimate, not
the reported figure' annotation."

All three clauses are decided here against a running stack. The exporter serves
a run on the host, Prometheus scrapes it every second through the whole run,
and each panel's expression is then evaluated against what Prometheus actually
stored. The third clause is also checked without containers in the unit suite;
it is repeated here against the dashboards Grafana loaded, because a dashboard
that fails to provision would otherwise pass the file-level check silently.

The cardinality clause is checked as a pair. An active-series count under the
cap means nothing on its own -- admission control could have produced it by
refusing everything -- so the refusal count must be zero as well.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry

from dsel.compose.observability import (
    StackPorts,
    dashboards,
    down,
    panel_targets,
    up,
)
from dsel.live.exporter import MAX_ACTIVE_SERIES, RunCollector, serve, wait_for_scrape
from dsel.runtime.paths import RunLayout, new_run_id
from tests.conftest import requires_docker

WRITER_SECONDS = 30.0
REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [requires_docker, pytest.mark.slow]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _query(port: int, expr: str, path: str = "query") -> list[dict[str, object]]:
    """One PromQL instant query. Raises on anything but a clean success."""
    url = f"http://127.0.0.1:{port}/api/v1/{path}?" + urllib.parse.urlencode({"query": expr})
    with urllib.request.urlopen(url, timeout=30) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise AssertionError(f"{expr!r} -> {payload.get('error')}")
    result = payload["data"]["result"]
    assert isinstance(result, list)
    return result


def _scalar(port: int, expr: str) -> float:
    result = _query(port, expr)
    assert result, f"{expr!r} returned nothing"
    return float(result[0]["value"][1])


@pytest.fixture(scope="module")
def stack(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[StackPorts, RunCollector]]:
    """A full run, exported, scraped, and left in Prometheus for querying."""
    tmp_path = tmp_path_factory.mktemp("s8b")
    run_id = new_run_id()
    layout = RunLayout.for_run(run_id, base=tmp_path / "runs")
    layout.create()

    ports = StackPorts(exporter=_free_port(), prometheus=_free_port(), grafana=_free_port())
    collector, _ = serve(layout.root, ports.exporter, registry=CollectorRegistry())
    wait_for_scrape(ports.exporter)

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)])

    up(run_id, ports)
    try:
        writer = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.support.fake_run",
                str(layout.root),
                str(WRITER_SECONDS),
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, err = writer.communicate(timeout=WRITER_SECONDS + 60)
        assert writer.returncode == 0, err
        assert int(out.split()[1]) > 500, out
        # Let the tailer's watermark drain and Prometheus scrape the last state.
        collector.ingest(final=True)
        time.sleep(5)
        yield ports, collector
    finally:
        down(run_id, ports)


def test_the_series_budget_holds_for_the_whole_run(
    stack: tuple[StackPorts, RunCollector],
) -> None:
    ports, _ = stack
    peak = _scalar(ports.prometheus, 'max_over_time(count({__name__=~"dsbench_.*"})[10m:1s])')
    refused = _scalar(ports.prometheus, "max_over_time(dsbench_series_refused_total[10m:1s])")
    print(f"\n  peak active dsbench_* series: {peak:.0f} / {MAX_ACTIVE_SERIES} cap")
    print(f"  series refused by the budget:  {refused:.0f}")
    assert peak <= MAX_ACTIVE_SERIES, f"peak active series {peak:.0f} > {MAX_ACTIVE_SERIES}"
    assert refused == 0, (
        f"{refused:.0f} series were refused; a count under the cap means nothing "
        "if admission control produced it by dropping series"
    )


def test_backends_are_aggregated_not_published_per_backend(
    stack: tuple[StackPorts, RunCollector],
) -> None:
    """The run holds 30 backends; publishing one series each is what the
    budget exists to prevent."""
    ports, collector = stack
    assert len(collector.state.backends) >= 20, "the run should have many backends"
    series = _query(ports.prometheus, "dsbench_backends")
    assert 0 < len(series) <= 6, f"expected a handful of states, got {len(series)}"
    assert all("backend_id" not in row["metric"] for row in series)


def test_every_panel_in_every_dashboard_resolves(
    stack: tuple[StackPorts, RunCollector],
) -> None:
    ports, _ = stack
    empty: list[str] = []
    checked = 0
    for name, board in dashboards().items():
        for title, expr in panel_targets(board):
            checked += 1
            if not _query(ports.prometheus, expr):
                empty.append(f"{name} / {title}\n    {expr}")
    print(
        f"\n  {checked} panel targets across {len(dashboards())} dashboards, "
        f"{checked - len(empty)} resolved"
    )
    assert checked >= 30, f"only {checked} targets checked"
    detail = "\n".join(empty)
    assert not empty, f"{len(empty)} of {checked} targets returned No data:\n{detail}"


def test_grafana_provisioned_all_four_dashboards(
    stack: tuple[StackPorts, RunCollector],
) -> None:
    ports, _ = stack
    deadline = time.monotonic() + 60
    found: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{ports.grafana}/api/search?type=dash-db", timeout=10
            ) as response:
                found = json.load(response)
            if len(found) >= 4:
                break
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    uids = {str(entry["uid"]) for entry in found}
    assert uids == {"dsel-now", "dsel-connections", "dsel-joins", "dsel-validity"}, uids


def test_the_datasource_provisioned_and_is_healthy(
    stack: tuple[StackPorts, RunCollector],
) -> None:
    ports, _ = stack
    with urllib.request.urlopen(
        f"http://127.0.0.1:{ports.grafana}/api/datasources/uid/dsel-prometheus", timeout=10
    ) as response:
        datasource = json.load(response)
    assert datasource["type"] == "prometheus"
    assert datasource["url"] == "http://prometheus:9090"


def test_grafana_serves_the_estimate_annotation_it_loaded(
    stack: tuple[StackPorts, RunCollector],
) -> None:
    """Checked against what Grafana loaded, not against the file on disk: a
    dashboard that failed to provision would pass the file-level check."""
    ports, _ = stack
    phrase = "within-window estimate, not the reported figure"
    seen = 0
    for uid in ("dsel-now", "dsel-connections", "dsel-joins"):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{ports.grafana}/api/dashboards/uid/{uid}", timeout=10
        ) as response:
            board = json.load(response)["dashboard"]
        for panel in board["panels"]:
            exprs = [t.get("expr", "") for t in panel.get("targets", [])]
            if any("estimate_microseconds" in expr for expr in exprs):
                seen += 1
                assert phrase in panel["title"], f"{uid}: {panel['title']}"
    assert seen >= 3, f"only {seen} latency panels found in Grafana"


def test_the_series_carry_the_local_profile_stamps(
    stack: tuple[StackPorts, RunCollector],
) -> None:
    """Every sample says it is not reportable, so an exported graph cannot be
    mistaken for a capacity number."""
    ports, _ = stack
    result = _query(ports.prometheus, "dsbench_records_total")
    assert result
    labels = result[0]["metric"]
    assert isinstance(labels, dict)
    assert labels["profile"] == "local"
    assert labels["reportable"] == "false"
