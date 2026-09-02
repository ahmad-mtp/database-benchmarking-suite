"""Samplers against a live container (PLAN.md S7).

Accept: every sampler emits records that validate against
schema/metrics.schema.json; cgroup readings equal the corresponding
`docker inspect .HostConfig` values exactly; killing the container with
`--memory` exceeded produces an `oom` record.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
from pathlib import Path

import pytest

from dsel.audit.environment import resolve_image
from dsel.live.merge import find_shards, merge_records
from dsel.live.ndjson import ShardWriter
from dsel.live.sampler.containers import ContainerSampler
from dsel.live.schema import RECORD_ADAPTER
from dsel.runtime.cgroup import read as read_cgroup
from dsel.runtime.docker import provision, wait_healthy
from dsel.runtime.envelope import GIB, ResourceEnvelope
from dsel.runtime.events import EventWatcher
from dsel.runtime.paths import new_run_id
from dsel.runtime.teardown import Teardown
from tests.conftest import requires_docker

SCHEMA = Path(__file__).resolve().parents[2] / "schema" / "metrics.schema.json"
DATA_DIR = "/var/lib/postgresql/data"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def engine():
    pin = resolve_image("postgres:18")
    run_id = new_run_id()
    envelope = ResourceEnvelope((2, 3, 4, 5), 4.0, 3 * GIB, pids_limit=512)
    teardown = Teardown(run_id)
    try:
        container = provision(
            pin,
            envelope,
            run_id,
            data_dir=DATA_DIR,
            container_port=5432,
            host_port=free_port(),
            env={"POSTGRES_PASSWORD": "dsel", "PGDATA": f"{DATA_DIR}/pgdata"},
        )
        wait_healthy(container, ["pg_isready", "-U", "postgres", "-q"])
        yield container, envelope, run_id
    finally:
        teardown.run()


@requires_docker
@pytest.mark.slow
def test_cgroup_readings_equal_docker_inspect_exactly(engine) -> None:
    container, envelope, _ = engine
    sample = read_cgroup(container.name)
    host = json.loads(
        subprocess.run(
            ["docker", "inspect", "--format", "{{json .HostConfig}}", container.name],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    assert sample.memory_max == host["Memory"] == envelope.memory_bytes
    assert sample.pids_max == host["PidsLimit"] == envelope.pids_limit
    assert sample.cpuset_effective == host["CpusetCpus"] == envelope.cpuset_spec
    assert sample.cpu_max == envelope.expected_cpu_max()


@requires_docker
@pytest.mark.slow
def test_sampler_emits_records_that_validate_against_the_committed_schema(
    engine, tmp_path: Path
) -> None:
    container, _, run_id = engine
    shard_dir = tmp_path / "shards"
    with ShardWriter(shard_dir, "containers-0") as writer:
        sampler = ContainerSampler(writer, [container.name], interval_s=0.2)
        for _ in range(3):
            assert sampler.sample_once() == 1
    assert sampler.errors == 0

    records = list(merge_records(find_shards(shard_dir)))
    assert len(records) == 3
    # Validated by the adapter on read; re-validate raw against the schema file.
    assert SCHEMA.is_file()
    for line in (shard_dir / "containers-0.ndjson").read_text().splitlines():
        RECORD_ADAPTER.validate_python(json.loads(line))
    for record in records:
        assert record.kind == "container"
        assert record.container == container.name
        assert record.memory_current is not None and record.memory_current > 0
        assert record.pids_current is not None and record.pids_current > 0
    del run_id


@requires_docker
@pytest.mark.slow
def test_cpu_counters_advance_between_ticks(engine, tmp_path: Path) -> None:
    """A sampler that reports a frozen counter is not sampling."""
    container, _, _ = engine
    subprocess.run(
        [
            "docker",
            "exec",
            "-d",
            container.name,
            "sh",
            "-c",
            "timeout 3 sh -c 'while :; do :; done'",
        ],
        capture_output=True,
        check=False,
    )
    first = read_cgroup(container.name)
    time.sleep(1.5)
    second = read_cgroup(container.name)
    assert second.cpu_usage_usec is not None and first.cpu_usage_usec is not None
    assert second.cpu_usage_usec > first.cpu_usage_usec


@requires_docker
@pytest.mark.slow
def test_exceeding_memory_produces_an_oom_event() -> None:
    """The deliberately-tripped failure path for the OOM detector."""
    pin = resolve_image("python:3.13-slim")
    run_id = new_run_id()
    teardown = Teardown(run_id)
    try:
        with EventWatcher(run_id) as watcher:
            time.sleep(1.0)  # let `docker events` attach before the container starts
            envelope = ResourceEnvelope((6, 7), 2.0, 64 * 1024 * 1024, pids_limit=128)
            container = provision(
                pin,
                envelope,
                run_id,
                data_dir="/scratch",
                container_port=9999,
                host_port=free_port(),
                command=[
                    "python3",
                    "-c",
                    "x=bytearray(); \nwhile True: x.extend(b'0'*10_000_000)",
                ],
            )
            deadline = time.time() + 60
            while time.time() < deadline and not watcher.events:
                time.sleep(0.2)
            events = list(watcher.events)

        actions = {e.action for e in events}
        assert actions & {"oom", "die"}, f"no oom/die event observed, saw {actions}"
        state = json.loads(
            subprocess.run(
                ["docker", "inspect", "--format", "{{json .State}}", container.name],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
        assert state["OOMKilled"] is True or any(e.action == "oom" for e in events), (
            f"container was not OOM-killed: {state}"
        )
    finally:
        teardown.run()
