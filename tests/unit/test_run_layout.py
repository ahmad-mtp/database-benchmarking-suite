"""The run directory layout (PLAN.md S0)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dsel.runtime.paths import (
    RUN_FILES,
    RUN_SUBDIRS,
    RunLayout,
    new_run,
    new_run_id,
)


def test_run_id_is_sortable_by_time() -> None:
    earlier = new_run_id(datetime(2026, 9, 2, 10, 0, 0, tzinfo=UTC))
    later = new_run_id(datetime(2026, 9, 2, 10, 0, 1, tzinfo=UTC))
    assert earlier < later, "lexicographic order must equal chronological order"
    assert earlier.startswith("20260902T100000Z-")


def test_run_ids_are_distinct_within_one_second() -> None:
    moment = datetime(2026, 9, 2, 10, 0, 0, tzinfo=UTC)
    ids = {new_run_id(moment) for _ in range(200)}
    assert len(ids) == 200


def test_create_makes_exactly_the_declared_subdirs(tmp_path: Path) -> None:
    layout = new_run(base=tmp_path)
    children = sorted(p.name for p in layout.root.iterdir())
    assert children == sorted(RUN_SUBDIRS)
    for name in RUN_SUBDIRS:
        assert (layout.root / name).is_dir()


def test_create_does_not_fabricate_files(tmp_path: Path) -> None:
    """An absent artifact at the end of a run is a signal; do not pre-create it."""
    layout = new_run(base=tmp_path)
    for name in RUN_FILES:
        assert not (layout.root / name).exists()


@pytest.mark.parametrize(
    ("attribute", "filename"),
    [
        ("manifest", "manifest.json"),
        ("metrics", "metrics.ndjson"),
        ("compose_rendered", "compose.rendered.yaml"),
        ("spec_canonical", "spec.canonical.json"),
    ],
)
def test_declared_files_have_accessors(tmp_path: Path, attribute: str, filename: str) -> None:
    layout = RunLayout.for_run("r1", base=tmp_path)
    assert getattr(layout, attribute) == layout.root / filename
    assert filename in RUN_FILES


def test_for_run_touches_nothing(tmp_path: Path) -> None:
    layout = RunLayout.for_run("r1", base=tmp_path)
    assert not layout.root.exists()


def test_create_refuses_to_reuse_a_run_directory(tmp_path: Path) -> None:
    """Two runs must never share a directory and interleave their evidence."""
    layout = RunLayout.for_run("r1", base=tmp_path).create()
    with pytest.raises(FileExistsError):
        RunLayout.for_run("r1", base=tmp_path).create()
    assert layout.root.is_dir()
