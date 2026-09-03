"""Run-matrix cell identity (PLAN.md S6/S8b).

A cell is one point of the frozen run matrix: survivor x scenario x rate x
repeat, optionally a step within a ramp. Every record carries its cell id as a
string, and S8b needs the *parts* -- `step` and `repeat` in particular, which
must join from `dsbench_cell_info` rather than become labels on every series.

Parsing is strict. A cell id that does not match is refused rather than
half-parsed into labels that would silently mislabel a whole run's series.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# uc1/postgres/oltp-mixed/r400/rep1  or  .../rep1/step3
CELL_PATTERN = re.compile(
    r"^(?P<use_case>[a-z0-9][a-z0-9-]*)"
    r"/(?P<engine>[a-z0-9][a-z0-9-]*)"
    r"/(?P<scenario>[a-z0-9][a-z0-9-]*)"
    r"/r(?P<rate>\d+)"
    r"/rep(?P<repeat>\d+)"
    r"(?:/step(?P<step>\d+))?$"
)


class CellError(ValueError):
    """A cell id that does not match the canonical form."""


@dataclass(frozen=True, slots=True)
class Cell:
    """The parts of a cell id, for the `dsbench_cell_info` join series."""

    use_case: str
    engine: str
    scenario: str
    rate: int
    repeat: int
    step: int | None = None

    @classmethod
    def parse(cls, cell_id: str) -> Cell:
        match = CELL_PATTERN.match(cell_id)
        if match is None:
            raise CellError(
                f"{cell_id!r} is not a cell id; expected "
                "<use-case>/<engine>/<scenario>/r<rate>/rep<n>[/step<n>]"
            )
        step = match["step"]
        return cls(
            use_case=match["use_case"],
            engine=match["engine"],
            scenario=match["scenario"],
            rate=int(match["rate"]),
            repeat=int(match["repeat"]),
            step=int(step) if step is not None else None,
        )

    @property
    def id(self) -> str:
        base = f"{self.use_case}/{self.engine}/{self.scenario}/r{self.rate}/rep{self.repeat}"
        return base if self.step is None else f"{base}/step{self.step}"

    def info_labels(self) -> dict[str, str]:
        """Labels for `dsbench_cell_info`.

        These live here and nowhere else. Putting `step` or `repeat` on every
        series would multiply the whole metric set by the length of a ramp --
        a 12-step connection ramp with 3 repeats is a 36x blow-up -- so they
        join from this one series instead.
        """
        return {
            "cell": self.id,
            "use_case": self.use_case,
            "engine": self.engine,
            "scenario": self.scenario,
            "rate": str(self.rate),
            "repeat": str(self.repeat),
            "step": "" if self.step is None else str(self.step),
        }
