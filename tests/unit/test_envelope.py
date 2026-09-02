"""Resource envelope and readback verification (PLAN.md S3-S5)."""

from __future__ import annotations

import pytest

from dsel.runtime.envelope import GIB, Readback, ReadbackError, ResourceEnvelope

ENV = ResourceEnvelope(cpuset=(2, 3, 4, 5), cpus=4.0, memory_bytes=3 * GIB, pids_limit=512)


def matching_readback(**overrides: object) -> Readback:
    base: dict[str, object] = {
        "cpuset_effective": (2, 3, 4, 5),
        "cpu_max": "400000 100000",
        "memory_max": 3 * GIB,
        "pids_max": 512,
        "host_cpuset": "2-5",
        "host_nano_cpus": 4_000_000_000,
        "host_memory": 3 * GIB,
        "host_memory_swap": 3 * GIB,
    }
    base.update(overrides)
    return Readback(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("cpuset", "expected"),
    [
        ((2, 3, 4, 5), "2-5"),
        ((0, 1), "0-1"),
        ((3,), "3"),
        ((0, 2, 4), "0,2,4"),
        ((0, 1, 4, 5), "0-1,4-5"),
    ],
)
def test_cpuset_spec_formatting(cpuset: tuple[int, ...], expected: str) -> None:
    assert ResourceEnvelope(cpuset, 1.0, GIB).cpuset_spec == expected


def test_both_cpuset_and_cpus_are_always_set() -> None:
    """Quota alone leaves the container seeing every host core (findings.md 6.3)."""
    flags = ENV.docker_flags()
    assert "--cpuset-cpus" in flags and "--cpus" in flags
    assert "--memory" in flags and "--memory-swap" in flags


def test_swap_is_pinned_equal_to_memory() -> None:
    """A container allowed to swap reports the host's page cache as latency."""
    assert ENV.memory_swap_bytes == ENV.memory_bytes


def test_storage_has_exactly_one_legal_value() -> None:
    assert ENV.storage == "named_volume"


def test_matching_readback_passes() -> None:
    ENV.verify(matching_readback())


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("cpuset_effective", (0, 1, 2, 3), "cpuset"),
        ("cpu_max", "200000 100000", "cpu quota"),
        ("cpu_max", "max 100000", "cpu quota"),
        ("memory_max", 2 * GIB, "memory"),
        ("pids_max", -1, "pids"),
        ("host_cpuset", "0-9", "HostConfig.CpusetCpus"),
        ("host_nano_cpus", 2_000_000_000, "HostConfig.NanoCpus"),
        ("host_memory", GIB, "HostConfig.Memory"),
        ("host_memory_swap", -1, "HostConfig.MemorySwap"),
    ],
)
def test_every_knob_mismatch_is_refused(field: str, value: object, fragment: str) -> None:
    """Invalidate rather than report with a caveat: any disagreement stops the run."""
    with pytest.raises(ReadbackError, match=fragment):
        ENV.verify(matching_readback(**{field: value}))


def test_readback_error_names_both_sides() -> None:
    with pytest.raises(ReadbackError) as exc:
        ENV.verify(matching_readback(memory_max=GIB))
    message = str(exc.value)
    assert str(3 * GIB) in message and str(GIB) in message


def test_multiple_mismatches_are_all_reported() -> None:
    """Fixing one knob at a time across restarts is not a workflow."""
    with pytest.raises(ReadbackError) as exc:
        ENV.verify(matching_readback(memory_max=GIB, pids_max=-1, host_cpuset="0-9"))
    assert str(exc.value).count("\n  - ") == 3
