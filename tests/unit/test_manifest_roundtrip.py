"""The manifest must round-trip exactly: a third party re-reads it (findings.md 8.6)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from dsel.audit.models import (
    DaemonCapture,
    HostCapture,
    ImagePin,
    Manifest,
    VcpuProbe,
    VcpuSpeed,
    VmCapture,
)


def _manifest(reasons: list[str] | None = None) -> Manifest:
    pin = ImagePin(
        reference="python:3.13-slim",
        index_digest="sha256:" + "9" * 64,
        platform_digest="sha256:" + "c" * 64,
        platform="linux/arm64/v8",
    )
    speeds = [
        VcpuSpeed(vcpu=i, median_ips=100.0, min_ips=95.0, max_ips=105.0, samples=5)
        for i in range(10)
    ]
    return Manifest(
        captured_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        envelope_deviation_reasons=reasons or [],
        harness_version="0.1.0",
        harness_commit="abc1234",
        harness_dirty=False,
        host=HostCapture(os_name="Darwin", os_version="26.6.2", arch="arm64"),
        daemon=DaemonCapture(
            server_version="29.7.2",
            kernel_version="7.0.12-linuxkit",
            operating_system="Docker Desktop",
            storage_driver="overlayfs",
            ncpu=10,
            mem_total_bytes=8319504384,
        ),
        vm=VmCapture(kernel_release="7.0.12-linuxkit"),
        vcpu_probe=VcpuProbe(
            work_ms=200,
            repeats=6,
            discarded_warmup=1,
            per_vcpu=speeds,
            relative_speed=[1.0] * 10,
            engine_cpuset=[2, 3, 4, 5],
            driver_cpuset=[6, 7, 8, 9],
            engine_aggregate_ips=400.0,
            driver_aggregate_ips=400.0,
            set_difference_pct=0.0,
            between_vcpu_span_pct=0.0,
            max_within_vcpu_spread_pct=10.0,
            image=pin,
        ),
    )


def test_manifest_round_trips_through_json() -> None:
    original = _manifest(["vcpu_speed_indistinguishable"])
    restored = Manifest.model_validate_json(original.model_dump_json())
    assert restored == original


def test_local_profile_is_never_reportable_by_default() -> None:
    m = _manifest()
    assert m.profile == "local"
    assert m.reportable is False
    assert m.envelope_deviation is True


def test_speed_vector_has_one_entry_per_vcpu() -> None:
    m = _manifest()
    assert m.vcpu_probe is not None
    assert len(m.vcpu_probe.relative_speed) == len(m.vcpu_probe.per_vcpu) == 10


def test_undeclared_fields_are_refused() -> None:
    """An unmodelled field must not ride silently into the bundle."""
    payload = _manifest().model_dump(mode="json")
    payload["surprise"] = "should not be accepted"
    with pytest.raises(ValidationError):
        Manifest.model_validate(payload)


def test_manifest_is_immutable() -> None:
    m = _manifest()
    with pytest.raises(ValidationError):
        m.reportable = True  # type: ignore[misc]
