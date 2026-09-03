"""PLAN.md's two structural rules, enforced in tests (S15 depends on them).

* `live/sampler/*` only writes records and never derives a phenomenon.
* `phenomena/*` only reads metrics.ndjson and never touches Docker or the engine.

Checked against the parsed module, not the file's text: a docstring that
mentions the rule is not a violation of it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "dsel"
SAMPLERS = SRC / "live" / "sampler"
PHENOMENA = SRC / "phenomena"


def python_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*.py")) if directory.is_dir() else []


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def called_names(path: Path) -> set[str]:
    """Every attribute/name used in a call, e.g. `subprocess.run`."""
    tree = ast.parse(path.read_text(), filename=str(path))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            out.add(ast.unparse(node.func))
    return out


def test_there_are_samplers_to_check() -> None:
    assert python_files(SAMPLERS), "no samplers found; this rule would vacuously pass"


@pytest.mark.parametrize("path", python_files(SAMPLERS), ids=lambda p: p.name)
def test_samplers_do_not_import_phenomena(path: Path) -> None:
    offenders = {m for m in imported_modules(path) if "phenomena" in m}
    assert not offenders, f"{path.name} imports {offenders}; samplers only write records"


@pytest.mark.parametrize("path", python_files(SAMPLERS), ids=lambda p: p.name)
def test_samplers_write_records_only(path: Path) -> None:
    """A sampler may read the system and write records. It must not compute a
    knee, a slope or a verdict -- those live in phenomena/, from the file."""
    source = path.read_text()
    for banned in ("def derive", "def detect_knee", "def collapse", "bootstrap"):
        assert banned not in source, f"{path.name} contains {banned!r}: that is derivation"


def test_phenomena_never_touch_docker() -> None:
    files = python_files(PHENOMENA)
    if not files:
        pytest.skip("phenomena package not built yet (S15)")
    for path in files:
        modules = imported_modules(path)
        forbidden = modules & {
            "subprocess",
            "docker",
            "dsel.runtime.docker",
            "dsel.runtime.cgroup",
        }
        assert not forbidden, (
            f"{path.name} imports {forbidden}; phenomena must read metrics.ndjson "
            "alone (PLAN.md S15)"
        )
        assert not any(c.startswith("subprocess") for c in called_names(path))


def test_there_are_phenomena_to_check() -> None:
    assert python_files(PHENOMENA), "no phenomena found; the rule would vacuously pass"


@pytest.mark.parametrize("path", python_files(PHENOMENA), ids=lambda p: p.name)
def test_phenomena_read_only_the_metrics_stream(path: Path) -> None:
    """A derivation may depend on the record schema and nothing else.

    Not a style rule. S15's criterion is that an independent script re-derives
    the landmarks from `metrics.ndjson` alone; a derivation that reached into
    `runtime` for a container, or into `driver` to run a load, could not be
    replayed against an audit bundle a year later, and a number nobody can
    re-derive is not evidence.
    """
    internal = {m for m in imported_modules(path) if m.startswith("dsel.")}
    allowed = {"dsel.live.schema", "dsel.live.cell", "dsel.live.merge", "dsel.live.ndjson"}
    offenders = internal - allowed
    assert not offenders, (
        f"{path.name} imports {sorted(offenders)}; phenomena may depend on the "
        f"record schema and nothing else (allowed: {sorted(allowed)})"
    )


def test_the_ramp_takes_its_rules_from_phenomena_rather_than_restating_them() -> None:
    """One definition of a knee, applied to two data paths.

    A second copy in the driver would drift from this one, and S15's
    re-derivation would then be two definitions agreeing with themselves.
    """
    ramp = SRC / "driver" / "ramp.py"
    assert "dsel.phenomena.conn_cliff" in imported_modules(ramp)

    tree = ast.parse(ramp.read_text(), filename=str(ramp))
    # The thresholds are imported, never assigned here: a second value for
    # KNEE_FACTOR is a second definition of a knee.
    assigned = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    thresholds = {
        "KNEE_FACTOR",
        "COLLAPSE_DROP",
        "COLLAPSE_ERRORS",
        "DELIVERY_TOLERANCE",
        "ERROR_TOLERANCE",
    }
    assert not (assigned & thresholds), (
        f"driver/ramp.py assigns {sorted(assigned & thresholds)}; the thresholds "
        "belong to phenomena and must be imported from it"
    )

    # And each landmark delegates rather than deciding.
    landmarks = {"max_sustainable_rate_per_s", "knee_rate_per_s", "collapse_rate_per_s"}
    found = {
        node.name: ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in landmarks
    }
    assert set(found) == landmarks, f"missing landmarks in ramp.py: {landmarks - set(found)}"
    for name, body in found.items():
        assert "self.curve" in body, (
            f"ramp.{name} computes its own answer instead of delegating to phenomena"
        )
