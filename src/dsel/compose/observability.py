"""Bringing the LIGHT observability stack up and down (PLAN.md S8b).

Prometheus and Grafana run from `observability/compose.yaml`, pinned to the
host slice 0-1 with 1.5 GiB between them -- the `observability-light` component
the budget already accounts for.

**Nothing is bind-mounted.** D7 forbids a VirtioFS mount for the life of a run,
and the configuration files are no exception: Grafana's provisioner polls its
dashboard directory and Prometheus re-reads its config on reload, so a bind
mount here would be live VirtioFS I/O for the whole measurement window. Instead
the rendered configuration is streamed into a named volume through `docker cp`,
which is the same choice `audit.environment` makes when it sends a probe script
over stdin rather than mounting it.

The port lands in the Prometheus config at seed time rather than through an
environment variable, so the file in the volume is the file that was used --
there is no second place to look when a scrape target is wrong.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from string import Template

from dsel.runtime.teardown import LABEL_KEY, MANAGED_LABEL

PROJECT = "dsel-obs"
CONFIG_VOLUME = "dsel-obs_dsel-obs-config"
SEED_IMAGE = "busybox:1.37.0"
CONFIG_MOUNTPOINT = "/etc/dsel"

DEFAULT_PROMETHEUS_PORT = 9090
DEFAULT_GRAFANA_PORT = 3000
DEFAULT_EXPORTER_PORT = 9464

REPO_ROOT = Path(__file__).resolve().parents[3]
OBSERVABILITY_DIR = REPO_ROOT / "observability"
COMPOSE_FILE = OBSERVABILITY_DIR / "compose.yaml"
DASHBOARD_DIR = OBSERVABILITY_DIR / "grafana" / "dashboards"


class ObservabilityError(RuntimeError):
    """The stack could not be brought up. Never fall back to a bind mount."""


@dataclass(frozen=True, slots=True)
class StackPorts:
    """Where the three moving parts answer."""

    exporter: int = DEFAULT_EXPORTER_PORT
    prometheus: int = DEFAULT_PROMETHEUS_PORT
    grafana: int = DEFAULT_GRAFANA_PORT


def render_config(run_id: str, ports: StackPorts) -> dict[str, str]:
    """The configuration tree as `relative path -> contents`.

    Returned as data so a test can assert what would be seeded without a Docker
    daemon, and so the rendered Prometheus config is inspectable next to the
    template it came from.
    """
    template = (OBSERVABILITY_DIR / "prometheus" / "prometheus.yml.tmpl").read_text(
        encoding="utf-8"
    )
    rendered = Template(template).substitute(
        DSEL_EXPORTER_PORT=str(ports.exporter), DSEL_RUN_ID=run_id
    )
    tree = {"prometheus/prometheus.yml": rendered}
    for source in sorted((OBSERVABILITY_DIR / "grafana").rglob("*")):
        if source.is_file():
            relative = source.relative_to(OBSERVABILITY_DIR)
            tree[str(relative)] = source.read_text(encoding="utf-8")
    return tree


def _run(
    command: list[str],
    env: dict[str, str] | None = None,
    timeout: float | None = 120.0,
) -> subprocess.CompletedProcess[str]:
    """Run a docker command, raising with its stderr rather than its exit code."""
    result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=timeout)
    if result.returncode != 0:
        raise ObservabilityError(
            f"{' '.join(command[:4])}... failed ({result.returncode}):\n{result.stderr.strip()}"
        )
    return result


def seed_config_volume(run_id: str, ports: StackPorts) -> str:
    """Stream the rendered configuration into a named volume.

    `docker cp` sends a tar over the daemon socket; no host path is ever
    mounted into a container, which is the whole point.
    """
    tree = render_config(run_id, ports)
    _run(
        [
            "docker",
            "volume",
            "create",
            "--label",
            f"{MANAGED_LABEL}=true",
            "--label",
            f"{LABEL_KEY}={run_id}",
            CONFIG_VOLUME,
        ]
    )
    holder = f"dsel-obs-seed-{run_id}"
    subprocess.run(["docker", "rm", "--force", holder], capture_output=True, text=True)
    _run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            holder,
            "--label",
            f"{MANAGED_LABEL}=true",
            "--label",
            f"{LABEL_KEY}={run_id}",
            "--volume",
            f"{CONFIG_VOLUME}:{CONFIG_MOUNTPOINT}",
            SEED_IMAGE,
            "sleep",
            "300",
        ]
    )
    try:
        with tempfile.TemporaryDirectory() as staging_dir:
            staging = Path(staging_dir)
            for relative, contents in tree.items():
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents, encoding="utf-8")
            _run(["docker", "cp", f"{staging}/.", f"{holder}:{CONFIG_MOUNTPOINT}"])
        # Grafana runs unprivileged; the seeded tree must be readable by it.
        _run(["docker", "exec", holder, "chmod", "-R", "a+rX", CONFIG_MOUNTPOINT])
    finally:
        subprocess.run(["docker", "rm", "--force", holder], capture_output=True, text=True)
    return CONFIG_VOLUME


def compose_env(run_id: str, ports: StackPorts) -> dict[str, str]:
    return {
        "DSEL_RUN_ID": run_id,
        "DSEL_PROMETHEUS_PORT": str(ports.prometheus),
        "DSEL_GRAFANA_PORT": str(ports.grafana),
        "DSEL_EXPORTER_PORT": str(ports.exporter),
    }


def up(run_id: str, ports: StackPorts | None = None) -> StackPorts:
    """Seed the configuration and start the stack. Idempotent per run id."""
    ports = ports or StackPorts()
    if shutil.which("docker") is None:
        raise ObservabilityError("docker is not on PATH")
    seed_config_volume(run_id, ports)
    env = dict(os.environ) | compose_env(run_id, ports)
    _run(
        ["docker", "compose", "--file", str(COMPOSE_FILE), "up", "--detach", "--wait"],
        env=env,
        timeout=300,
    )
    return ports


def down(run_id: str, ports: StackPorts | None = None) -> None:
    """Stop the stack and remove its volumes. Safe to call twice."""
    ports = ports or StackPorts()
    env = dict(os.environ) | compose_env(run_id, ports)
    subprocess.run(
        [
            "docker",
            "compose",
            "--file",
            str(COMPOSE_FILE),
            "down",
            "--volumes",
            "--timeout",
            "5",
        ],
        capture_output=True,
        text=True,
        env=env,
    )


def dashboards() -> dict[str, dict[str, object]]:
    """Every provisioned dashboard, by file stem."""
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(DASHBOARD_DIR.glob("*.json"))
    }


def panel_targets(dashboard: dict[str, object]) -> list[tuple[str, str]]:
    """`(panel title, expr)` for every target in a dashboard."""
    out: list[tuple[str, str]] = []
    panels = dashboard.get("panels", [])
    assert isinstance(panels, list)
    for panel in panels:
        title = str(panel.get("title", ""))
        for target in panel.get("targets", []):
            expr = target.get("expr")
            if expr:
                out.append((title, str(expr)))
    return out
