"""Validity gates (PLAN.md S13, findings.md).

**Invalidate rather than report with a caveat.** A cell that breaches a gate is
marked and excluded, not published with a footnote nobody reads. The verdicts
are deliberately few and deliberately distinct:

* `OK` -- nothing fired.
* `FLAG` -- worth knowing, does not invalidate. Reported alongside the number.
* `INVALID` -- the measurement is wrong. It does not enter a score.
* `INCONCLUSIVE_DRIVER_BOUND` -- the measurement is not wrong, it is *absent*:
  the load offered was not the load asked for, so the engine was never put to
  the question. Distinct from `INVALID` because the fix is different -- a bigger
  driver, not a different engine.

The new gate at this phase is the app tier's, and it mirrors the driver's one
tier up: **app-tier CPU above 70% invalidates database-level claims.** Without
it the tier saturates first, every engine returns the same number, and the
harness confidently reports that the choice does not matter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Verdict = Literal["OK", "FLAG", "INVALID", "INCONCLUSIVE_DRIVER_BOUND"]

# PLAN.md's "New gate". The same 70% as the driver's, one tier up.
APP_CPU_LIMIT_PCT = 70.0
# Locally, PATH B is scheduled at this fraction of the measured ceiling. Not a
# safety margin for its own sake: the ceiling is where the tier *saturates*, and
# a run scheduled at it measures the queue in front of the app, not the engine.
LOCAL_CEILING_FRACTION = 0.60

GATE_APP_CPU = "app_tier_cpu_pct"
REASON_APP_SATURATED = "app_tier_saturated"


@dataclass(frozen=True, slots=True)
class GateResult:
    """One gate's outcome, with the arithmetic that produced it."""

    gate: str
    verdict: Verdict
    observed: float
    limit: float
    reason: str | None = None
    detail: str = ""

    @property
    def fired(self) -> bool:
        return self.verdict != "OK"


def app_cpu_gate(observed_pct: float, limit_pct: float = APP_CPU_LIMIT_PCT) -> GateResult:
    """App-tier CPU above the limit invalidates database-level claims.

    Not a flag. Past saturation the app tier is the bottleneck, every candidate
    engine returns the tier's ceiling, and the comparison the harness exists to
    make has quietly stopped being about the engines.
    """
    breached = observed_pct > limit_pct
    return GateResult(
        gate=GATE_APP_CPU,
        verdict="INVALID" if breached else "OK",
        observed=round(observed_pct, 2),
        limit=limit_pct,
        reason=REASON_APP_SATURATED if breached else None,
        detail=(
            f"app tier used {observed_pct:.1f}% of its quota against a {limit_pct:.0f}% "
            "limit; past it the tier saturates first and every engine looks identical"
            if breached
            else f"app tier used {observed_pct:.1f}% of its quota"
        ),
    )


def scheduled_rate_for(ceiling_per_s: float, fraction: float = LOCAL_CEILING_FRACTION) -> float:
    """The rate PATH B may be driven at, given a measured ceiling (S14)."""
    return ceiling_per_s * fraction


def worst(verdicts: list[Verdict]) -> Verdict:
    """The most serious verdict in a set. Order is deliberate."""
    order: tuple[Verdict, ...] = ("INVALID", "INCONCLUSIVE_DRIVER_BOUND", "FLAG")
    for verdict in order:
        if verdict in verdicts:
            return verdict
    return "OK"
