"""Provisioning, readback and teardown against a live engine (PLAN.md S3-S5).

Accept: every knob read back from the running engine matches the envelope;
teardown exits 0 twice.
"""

from __future__ import annotations

import socket

import pytest

from dsel.audit.environment import resolve_image
from dsel.runtime.docker import provision, read_back, verify_storage, wait_healthy
from dsel.runtime.envelope import GIB, ResourceEnvelope
from dsel.runtime.paths import new_run_id
from dsel.runtime.storage import EXPECTED_FSTYPES
from dsel.runtime.teardown import Teardown, list_managed
from tests.conftest import requires_docker

IMAGE = "postgres:18"
DATA_DIR = "/var/lib/postgresql/data"
READY = ["pg_isready", "-U", "postgres", "-q"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def engine():
    """A provisioned, healthy Postgres, torn down whatever happens."""
    pin = resolve_image(IMAGE)
    run_id = new_run_id()
    envelope = ResourceEnvelope(
        cpuset=(2, 3, 4, 5), cpus=4.0, memory_bytes=3 * GIB, pids_limit=512
    )
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
        wait_healthy(container, READY)
        yield container, envelope, run_id, teardown
    finally:
        teardown.run()


@requires_docker
@pytest.mark.slow
def test_every_knob_reads_back_equal_to_the_envelope(engine) -> None:
    container, envelope, _, _ = engine
    readback = read_back(container)
    envelope.verify(readback)  # raises on any disagreement
    assert readback.cpuset_effective == envelope.cpuset
    assert readback.cpu_max == envelope.expected_cpu_max()
    assert readback.memory_max == envelope.memory_bytes
    assert readback.host_memory_swap == envelope.memory_swap_bytes


@requires_docker
@pytest.mark.slow
def test_the_engine_sees_only_its_cpuset(engine) -> None:
    """`--cpus` alone would leave nproc reporting all ten (findings.md 6.3)."""
    container, envelope, _, _ = engine
    assert read_back(container).nproc_visible == len(envelope.cpuset)


@requires_docker
@pytest.mark.slow
def test_data_directory_is_on_a_named_volume(engine) -> None:
    container, _, _, _ = engine
    assert verify_storage(container) in EXPECTED_FSTYPES


@requires_docker
@pytest.mark.slow
def test_teardown_is_idempotent_and_exits_zero_twice(engine) -> None:
    container, _, run_id, teardown = engine
    assert len(list_managed(run_id, "container")) == 1
    assert len(list_managed(run_id, "volume")) == 1

    first = teardown.run()
    assert first == 2, f"expected container + volume removed, got {first}"
    assert list_managed(run_id, "container") == []
    assert list_managed(run_id, "volume") == []

    second = teardown.run()
    assert second == 0, "a second teardown must remove nothing and still succeed"
    del container


@requires_docker
@pytest.mark.slow
def test_teardown_leaves_other_runs_alone(engine) -> None:
    """The machine carries unrelated containers; teardown is label-scoped."""
    _, _, run_id, teardown = engine
    import subprocess

    before = subprocess.run(
        ["docker", "ps", "-aq"], capture_output=True, text=True, check=True
    ).stdout.split()
    teardown.run()
    after = subprocess.run(
        ["docker", "ps", "-aq"], capture_output=True, text=True, check=True
    ).stdout.split()
    removed = set(before) - set(after)
    assert len(removed) == 1, f"teardown removed {len(removed)} containers, expected 1"
