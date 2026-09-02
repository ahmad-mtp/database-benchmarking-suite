"""Pre-flight resource budget (PLAN.md S2).

Build this first so the arithmetic is visible on day one rather than discovered
at S14. Nothing here starts a container: it adds up what a configuration would
demand and refuses combinations the machine cannot honour, showing the sums.

The budget is the VM's, not the host's. `docker info` reports 7.75 GiB on this
machine while the host has 16 GiB, and it is the VM that has to fit everything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

GIB = 1024**3

# PLAN.md's locked slice table.
HOST_CPUSET = (0, 1)
ENGINE_CPUSET = (2, 3, 4, 5)
DRIVER_CPUSET = (6, 7, 8, 9)

# The VM keeps its own kernel, the daemon and containerd out of the allocatable
# pool. PLAN.md budgets "6.5 of 7.75 GiB", implying a 1.25 GiB reserve -- but
# `docker info` reports 7.748 GiB, not 7.75, so a literal 1.25 GiB reserve puts
# PLAN.md's own slice table 2 MiB over the line. 1 GiB is the round number that
# fits the table with 0.25 GiB to spare and still refuses deep observability.
VM_RESERVE_BYTES = 1024 * 1024 * 1024  # 1 GiB


class Observability(StrEnum):
    """How much of the observability stack is running."""

    NONE = "none"
    LIGHT = "light"  # prometheus + grafana, scraping metrics.ndjson only
    DEEP = "deep"  # + cAdvisor, node-exporter, per-engine exporters


@dataclass(frozen=True, slots=True)
class Component:
    """One container's claim on the machine."""

    name: str
    cpuset: tuple[int, ...]
    cpus: float
    memory_bytes: int

    @property
    def memory_gib(self) -> float:
        return self.memory_bytes / GIB


class BudgetError(RuntimeError):
    """A configuration the machine cannot honour. Carries the arithmetic."""


@dataclass(frozen=True, slots=True)
class Budget:
    """What the machine has to give."""

    total_vcpus: int
    total_memory_bytes: int
    reserve_bytes: int = VM_RESERVE_BYTES

    @property
    def allocatable_bytes(self) -> int:
        return self.total_memory_bytes - self.reserve_bytes


@dataclass(frozen=True, slots=True)
class BudgetPlan:
    """A proposed set of components, checkable before anything starts."""

    budget: Budget
    components: list[Component] = field(default_factory=list)

    @property
    def memory_claimed(self) -> int:
        return sum(c.memory_bytes for c in self.components)

    def cpu_claimed_on(self, vcpu: int) -> float:
        """Total CPU quota claimed by components whose cpuset includes `vcpu`.

        A component's quota is spread across its cpuset, so a 4.0-quota
        container on a 4-vCPU set claims 1.0 per vCPU.
        """
        total = 0.0
        for component in self.components:
            if vcpu in component.cpuset and component.cpuset:
                total += component.cpus / len(component.cpuset)
        return total

    def arithmetic(self) -> str:
        """The sums, rendered. Shown whenever the plan is refused."""
        lines = [
            f"{'component':<26} {'cpuset':>10} {'cpus':>6} {'memory':>10}",
            f"{'-' * 26} {'-' * 10} {'-' * 6} {'-' * 10}",
        ]
        for c in sorted(self.components, key=lambda c: c.name):
            cpuset = f"{min(c.cpuset)}-{max(c.cpuset)}" if c.cpuset else "-"
            lines.append(f"{c.name:<26} {cpuset:>10} {c.cpus:>6.1f} {c.memory_gib:>9.2f}G")
        claimed = self.memory_claimed / GIB
        allocatable = self.budget.allocatable_bytes / GIB
        total = self.budget.total_memory_bytes / GIB
        reserve = self.budget.reserve_bytes / GIB
        lines += [
            f"{'-' * 26} {'-' * 10} {'-' * 6} {'-' * 10}",
            f"{'claimed':<26} {'':>10} {'':>6} {claimed:>9.2f}G",
            f"{'allocatable':<26} {'':>10} {'':>6} {allocatable:>9.2f}G"
            f"   (VM {total:.2f}G - {reserve:.2f}G reserve)",
            f"{'headroom':<26} {'':>10} {'':>6} {allocatable - claimed:>9.2f}G",
        ]
        over = [
            (v, self.cpu_claimed_on(v))
            for v in range(self.budget.total_vcpus)
            if self.cpu_claimed_on(v) > 1.0
        ]
        if over:
            lines.append("oversubscribed vCPUs:")
            lines += [
                f"  vcpu {v}: {claim:.2f} quota claimed (1.00 available)" for v, claim in over
            ]
        return "\n".join(lines)

    def check(self) -> None:
        """Raise `BudgetError` with the arithmetic if this cannot be honoured."""
        problems: list[str] = []

        if self.memory_claimed > self.budget.allocatable_bytes:
            over = (self.memory_claimed - self.budget.allocatable_bytes) / GIB
            problems.append(
                f"memory: claims {self.memory_claimed / GIB:.2f}G against "
                f"{self.budget.allocatable_bytes / GIB:.2f}G allocatable, over by {over:.2f}G"
            )

        for component in self.components:
            outside = [v for v in component.cpuset if v >= self.budget.total_vcpus]
            if outside:
                problems.append(
                    f"{component.name}: cpuset references vCPU {outside} but the VM has "
                    f"{self.budget.total_vcpus} (0-{self.budget.total_vcpus - 1})"
                )
            if component.cpuset and component.cpus > len(component.cpuset):
                problems.append(
                    f"{component.name}: --cpus {component.cpus} exceeds its cpuset width "
                    f"{len(component.cpuset)}; quota can never be met"
                )

        oversubscribed = [
            (v, self.cpu_claimed_on(v))
            for v in range(self.budget.total_vcpus)
            if self.cpu_claimed_on(v) > 1.0
        ]
        # vCPUs 0-1 are shared by design (host, app tier, observability), so
        # oversubscription there is expected and recorded rather than refused.
        hard = [(v, c) for v, c in oversubscribed if v not in HOST_CPUSET]
        if hard:
            detail = ", ".join(f"vcpu {v} at {c:.2f}" for v, c in hard)
            problems.append(f"cpu: measurement vCPUs oversubscribed ({detail})")

        if problems:
            raise BudgetError(
                "this configuration does not fit:\n  - "
                + "\n  - ".join(problems)
                + "\n\n"
                + self.arithmetic()
            )


def local_profile(
    *,
    with_app_tier: bool,
    observability: Observability,
    engine_memory_bytes: int = 3 * GIB,
    driver_memory_bytes: int = 1 * GIB,
    app_memory_bytes: int = 1 * GIB,
) -> list[Component]:
    """The `profile=local` component set from PLAN.md's slice table."""
    components = [
        Component("engine", ENGINE_CPUSET, 4.0, engine_memory_bytes),
        Component("driver", DRIVER_CPUSET, 4.0, driver_memory_bytes),
    ]
    if with_app_tier:
        components.append(Component("app-tier", HOST_CPUSET, 2.0, app_memory_bytes))
    if observability is Observability.LIGHT:
        # prometheus + grafana, consuming metrics.ndjson through the exporter.
        components.append(Component("observability-light", HOST_CPUSET, 1.0, 1536 * 1024**2))
    elif observability is Observability.DEEP:
        # + cAdvisor, node-exporter and a per-engine exporter per candidate.
        components.append(Component("observability-deep", HOST_CPUSET, 2.0, 3584 * 1024**2))
    return components


def plan_local(
    budget: Budget, *, with_app_tier: bool, observability: Observability
) -> BudgetPlan:
    """Assemble and return the `profile=local` plan (unchecked)."""
    return BudgetPlan(
        budget=budget,
        components=local_profile(with_app_tier=with_app_tier, observability=observability),
    )
