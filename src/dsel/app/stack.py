"""Bringing the app tier up and down (PLAN.md S13-S14).

The tier gets the host slice, cpuset 0-1, which the budget already accounts
for. Its `--cpus` quota is not a detail: it is the *denominator* of
`app_tier_cpu_pct`. An unlimited container has no denominator, so the gate
would have no number and would pass by never being able to fire.

Nothing is bind-mounted. The image carries the code and the container is
configured entirely from environment variables (D7).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from dsel.runtime.teardown import LABEL_KEY, MANAGED_LABEL

APP_CPUSET = "0-1"
DEFAULT_CPUS = 1.0
DEFAULT_MEMORY = "1g"
CONTAINER_PORT = 8000

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "images" / "app" / "Dockerfile"


class AppStackError(RuntimeError):
    """The app tier could not be started."""


def _docker(args: list[str], timeout: float = 900.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def build_app_image(tag: str = "dsel-app:local") -> str:
    result = _docker(["build", "--file", str(DOCKERFILE), "--tag", tag, str(REPO_ROOT)])
    if result.returncode != 0:
        raise AppStackError(f"could not build the app image:\n{result.stderr.strip()}")
    return tag


@dataclass(frozen=True, slots=True)
class AppContainer:
    name: str
    host_port: int
    cpus: float
    workers: int


def start_app(
    image: str,
    run_id: str,
    host_port: int,
    *,
    network: str | None = None,
    alias: str = "app",
    dsn: str | None = None,
    cell: str = "uc1/app/noop/r0/rep1",
    cpus: float = DEFAULT_CPUS,
    memory: str = DEFAULT_MEMORY,
    workers: int = 1,
    scale: int = 10,
    pool_max: int = 16,
) -> AppContainer:
    """Start the tier. Without `dsn` only `/noop` and `/cpu` are live.

    That is deliberate: S13 measures the ceiling of the tier *alone*, and a
    pool opened against an engine would fold the engine into a number that is
    supposed to be about the tier.
    """
    name = f"dsel-app-{run_id}"
    _docker(["rm", "--force", name])
    args = [
        "run",
        "--detach",
        "--name",
        name,
        "--label",
        f"{MANAGED_LABEL}=true",
        "--label",
        f"{LABEL_KEY}={run_id}",
        "--cpuset-cpus",
        APP_CPUSET,
        "--cpus",
        str(cpus),
        "--memory",
        memory,
        "--memory-swap",
        memory,
        "--pids-limit",
        "1024",
        "--publish",
        f"{host_port}:{CONTAINER_PORT}",
        "--env",
        f"DSEL_CELL={cell}",
        "--env",
        f"DSEL_APP_WORKERS={workers}",
        "--env",
        f"DSEL_SCALE={scale}",
        "--env",
        f"DSEL_POOL_MAX={pool_max}",
    ]
    if dsn:
        args += ["--env", f"DSEL_DSN={dsn}"]
    if network:
        args += ["--network", network, "--network-alias", alias]
    args.append(image)
    result = _docker(args, timeout=300.0)
    if result.returncode != 0:
        raise AppStackError(f"could not start the app tier: {result.stderr.strip()}")
    return AppContainer(name=name, host_port=host_port, cpus=cpus, workers=workers)


def app_logs(name: str, tail: int = 50) -> str:
    return _docker(["logs", "--tail", str(tail), name]).stdout


def copy_shards(name: str, out_dir: Path) -> Path:
    """Take the tier's metrics shards out of the container (never a mount)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    _docker(["cp", f"{name}:/run/dsel/shards", str(out_dir)])
    return out_dir / "shards"
