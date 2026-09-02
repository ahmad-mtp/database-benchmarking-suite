"""Harness provenance: version, git commit, dirty flag.

findings.md M0 requires `dsel --version` to print the harness git commit and
dirty flag. A checkout without a `.git` directory (an exported tree, a copied
working directory) is a legitimate way to run the harness, so the commit
degrades to `None` rather than raising -- but a run made from such a tree is
not reproducible, and the manifest records that as `commit=None`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from dsel import __version__

_GIT_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where this harness build came from."""

    version: str
    commit: str | None
    dirty: bool

    def __str__(self) -> str:
        if self.commit is None:
            return f"dsel {self.version} (commit unknown)"
        state = ", dirty" if self.dirty else ""
        return f"dsel {self.version} (commit {self.commit}{state})"


def _git(args: list[str], cwd: Path) -> str | None:
    """Run a git command, returning stripped stdout or None if git cannot answer."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def repo_root() -> Path:
    """The directory the installed package lives under."""
    return Path(__file__).resolve().parent.parent.parent


def provenance(cwd: Path | None = None) -> Provenance:
    """Resolve harness provenance, never raising for a missing or broken repo."""
    root = cwd if cwd is not None else repo_root()
    inside = _git(["rev-parse", "--is-inside-work-tree"], root)
    if inside != "true":
        return Provenance(version=__version__, commit=None, dirty=False)
    commit = _git(["rev-parse", "--short=7", "HEAD"], root)
    if not commit:
        return Provenance(version=__version__, commit=None, dirty=False)
    status = _git(["status", "--porcelain"], root)
    return Provenance(version=__version__, commit=commit, dirty=bool(status))
