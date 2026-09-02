"""The run directory layout, declared once.

Every artifact a run produces has exactly one home. S6 writes `metrics.ndjson`
via sharded writers under `shards/`, S8a replays it, S10 writes HdrHistogram
`.hlog` files under `histograms/`, and G3 hashes the whole tree into a
content-addressed bundle. Scattering these paths as string literals across
those phases is how a bundle ends up missing a leaf, so they live here.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

RUNS_DIRNAME = "runs"

# Directories created for every run.
RUN_SUBDIRS: tuple[str, ...] = (
    "shards",
    "evidence",
    "histograms",
    "logs",
)

# Files a run is expected to produce. Declared, not created -- their absence at
# the end of a run is a real signal, so `create()` must not fabricate them.
RUN_FILES: tuple[str, ...] = (
    "manifest.json",
    "metrics.ndjson",
    "compose.rendered.yaml",
    "spec.canonical.json",
)


def new_run_id(now: datetime | None = None) -> str:
    """A sortable run identifier: UTC timestamp plus a short random suffix.

    Lexicographic order equals chronological order, so `ls runs/` is sorted by
    time without parsing. The suffix keeps two runs started within the same
    second distinct.
    """
    moment = now if now is not None else datetime.now(UTC)
    stamp = moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


@dataclass(frozen=True, slots=True)
class RunLayout:
    """Every path belonging to a single run."""

    run_id: str
    root: Path

    @classmethod
    def for_run(cls, run_id: str, base: Path | None = None) -> RunLayout:
        """Describe the layout for `run_id` without touching the filesystem."""
        parent = base if base is not None else Path.cwd() / RUNS_DIRNAME
        return cls(run_id=run_id, root=parent / run_id)

    # --- directories -------------------------------------------------------

    @property
    def shards(self) -> Path:
        """Per-writer `metrics.ndjson` shards, merged deterministically (S6)."""
        return self.root / "shards"

    @property
    def evidence(self) -> Path:
        """Per-phase `Evidence` records from the adapter lifecycle."""
        return self.root / "evidence"

    @property
    def histograms(self) -> Path:
        """Raw HdrHistogram `.hlog` files -- the authoritative latency record."""
        return self.root / "histograms"

    @property
    def logs(self) -> Path:
        """Container and harness logs."""
        return self.root / "logs"

    # --- files -------------------------------------------------------------

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def metrics(self) -> Path:
        return self.root / "metrics.ndjson"

    @property
    def compose_rendered(self) -> Path:
        return self.root / "compose.rendered.yaml"

    @property
    def spec_canonical(self) -> Path:
        return self.root / "spec.canonical.json"

    # --- creation ----------------------------------------------------------

    def create(self, exist_ok: bool = False) -> RunLayout:
        """Create the run root and its subdirectories. Files are not created."""
        self.root.mkdir(parents=True, exist_ok=exist_ok)
        for name in RUN_SUBDIRS:
            (self.root / name).mkdir(exist_ok=True)
        return self


def new_run(base: Path | None = None, now: datetime | None = None) -> RunLayout:
    """Allocate a fresh run id and create its directory tree."""
    return RunLayout.for_run(new_run_id(now), base=base).create()
