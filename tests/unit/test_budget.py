"""Pre-flight budget arithmetic (PLAN.md S2)."""

from __future__ import annotations

import pytest

from dsel.compose.budget import (
    GIB,
    Budget,
    BudgetError,
    BudgetPlan,
    Component,
    Observability,
    plan_local,
)

# This machine: docker info reports 7.75 GiB and 10 vCPU.
VM = Budget(total_vcpus=10, total_memory_bytes=8319504384)


def test_allocatable_reserves_headroom_for_the_vm() -> None:
    assert VM.allocatable_bytes < VM.total_memory_bytes
    assert VM.allocatable_bytes / GIB == pytest.approx(6.75, abs=0.02)


def test_engine_and_driver_alone_fit() -> None:
    plan_local(VM, with_app_tier=False, observability=Observability.NONE).check()


def test_app_tier_plus_light_observability_fits() -> None:
    """PLAN.md's slice table: 3 + 1 + 1 + 1.5 = 6.5 GiB.

    It fits, but only just. The VM reports 7.748 GiB rather than the 7.75 the
    plan assumes, so the table has ~0.25 GiB of headroom, not 1.25.
    """
    plan = plan_local(VM, with_app_tier=True, observability=Observability.LIGHT)
    plan.check()
    assert plan.memory_claimed / GIB == pytest.approx(6.5, abs=0.02)


def test_app_tier_plus_deep_observability_is_refused() -> None:
    """The S2 acceptance case."""
    plan = plan_local(VM, with_app_tier=True, observability=Observability.DEEP)
    with pytest.raises(BudgetError) as exc:
        plan.check()
    message = str(exc.value)
    assert "memory" in message
    assert "over by" in message


def test_refusal_shows_the_arithmetic() -> None:
    """A refusal that does not show its working is not actionable."""
    plan = plan_local(VM, with_app_tier=True, observability=Observability.DEEP)
    with pytest.raises(BudgetError) as exc:
        plan.check()
    message = str(exc.value)
    for expected in ("component", "engine", "driver", "claimed", "allocatable", "headroom"):
        assert expected in message, f"arithmetic missing {expected!r}"


def test_cpuset_beyond_the_vm_is_refused() -> None:
    plan = BudgetPlan(VM, [Component("engine", (8, 9, 10, 11), 4.0, GIB)])
    with pytest.raises(BudgetError, match="but the VM has 10"):
        plan.check()


def test_quota_wider_than_its_cpuset_is_refused() -> None:
    """--cpus 8 on a 4-vCPU cpuset is a quota that can never be met."""
    plan = BudgetPlan(VM, [Component("engine", (2, 3, 4, 5), 8.0, GIB)])
    with pytest.raises(BudgetError, match="exceeds its cpuset width"):
        plan.check()


def test_measurement_vcpus_may_not_be_oversubscribed() -> None:
    plan = BudgetPlan(
        VM,
        [
            Component("engine", (2, 3, 4, 5), 4.0, GIB),
            Component("intruder", (2, 3, 4, 5), 4.0, GIB),
        ],
    )
    with pytest.raises(BudgetError, match="measurement vCPUs oversubscribed"):
        plan.check()


def test_host_vcpus_may_be_oversubscribed_by_design() -> None:
    """0-1 carry host, app tier and observability. That is the design, not a fault."""
    plan = BudgetPlan(
        VM,
        [
            Component("app-tier", (0, 1), 2.0, GIB),
            Component("observability", (0, 1), 2.0, GIB),
        ],
    )
    plan.check()
    assert plan.cpu_claimed_on(0) > 1.0


def test_arithmetic_names_oversubscribed_vcpus() -> None:
    plan = BudgetPlan(
        VM,
        [
            Component("app-tier", (0, 1), 2.0, GIB),
            Component("observability", (0, 1), 2.0, GIB),
        ],
    )
    assert "oversubscribed vCPUs" in plan.arithmetic()
    assert "vcpu 0" in plan.arithmetic()


def test_a_bigger_machine_would_fit_deep_observability() -> None:
    """The envelope is a spec value, not a constant: larger hardware is a config change."""
    big = Budget(total_vcpus=16, total_memory_bytes=32 * GIB)
    plan_local(big, with_app_tier=True, observability=Observability.DEEP).check()


def test_plan_md_slice_table_has_little_headroom() -> None:
    """Recorded deliberately: the locked table is close to the line.

    `docker info` reports 7.748 GiB, not the 7.75 PLAN.md assumes. Anything that
    grows the engine, driver or app allocation needs this recomputed rather than
    eyeballed.
    """
    plan = plan_local(VM, with_app_tier=True, observability=Observability.LIGHT)
    headroom_gib = (VM.allocatable_bytes - plan.memory_claimed) / GIB
    assert 0 < headroom_gib < 0.5, f"headroom is {headroom_gib:.3f} GiB"
