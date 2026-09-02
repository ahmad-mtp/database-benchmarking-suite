"""S1 end to end against the live daemon (PLAN.md S1 Accept)."""

from __future__ import annotations

import pytest

from dsel.audit.environment import build_manifest, capture_daemon, capture_vm, resolve_image
from dsel.audit.models import Manifest
from dsel.audit.vcpu_probe import (
    REASON_HETEROGENEOUS,
    REASON_INDISTINGUISHABLE,
    run_probe,
)
from tests.conftest import requires_docker

PROBE_IMAGE = "python:3.13-slim"


@pytest.fixture(scope="module")
def pin():
    return resolve_image(PROBE_IMAGE)


@requires_docker
def test_index_and_platform_digests_differ(pin) -> None:
    """The invariant: pin the index digest, record the platform digest.

    They are different values; conflating them is what makes one spec
    unrunnable across a dev Mac and amd64 CI.
    """
    assert pin.index_digest.startswith("sha256:")
    assert pin.platform_digest.startswith("sha256:")
    assert pin.index_digest != pin.platform_digest
    assert pin.platform.startswith("linux/")


@requires_docker
def test_attestation_manifests_are_skipped(pin) -> None:
    """`unknown/unknown` entries are attestations, not runnable images."""
    assert "unknown" not in pin.platform


@requires_docker
def test_daemon_capture_has_the_fields_that_change_the_candidate_set() -> None:
    d = capture_daemon()
    assert d.kernel_version, "kernel version decides whether mongo:8 can run at all"
    assert d.ncpu > 0
    assert d.mem_total_bytes > 0


@requires_docker
def test_vm_capture_reads_from_inside_the_container(pin) -> None:
    vm = capture_vm(pin)
    assert vm.kernel_release != "unknown"
    assert "cpuset" in vm.cgroup_controllers
    assert "memory" in vm.cgroup_controllers
    assert vm.cpuinfo_processors is not None and vm.cpuinfo_processors > 0


@requires_docker
def test_probe_produces_a_vector_covering_every_vcpu(pin) -> None:
    probe, reasons = run_probe(pin, repeats=3)
    daemon = capture_daemon()
    assert len(probe.per_vcpu) == daemon.ncpu
    assert len(probe.relative_speed) == daemon.ncpu
    assert max(probe.relative_speed) == 1.0
    assert all(0.0 < v <= 1.0 for v in probe.relative_speed)
    assert set(reasons) <= {REASON_HETEROGENEOUS, REASON_INDISTINGUISHABLE}


@requires_docker
def test_manifest_carries_the_stamps_and_round_trips(pin) -> None:
    m = build_manifest(pin, [REASON_INDISTINGUISHABLE])
    assert m.profile == "local"
    assert m.reportable is False
    assert m.envelope_deviation is True
    assert REASON_INDISTINGUISHABLE in m.envelope_deviation_reasons
    assert Manifest.model_validate_json(m.model_dump_json()) == m
