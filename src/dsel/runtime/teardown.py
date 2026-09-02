"""Idempotent teardown (PLAN.md S3-S5, findings.md 6.5).

Teardown must run on the happy path, on an exception, on SIGINT and on SIGTERM,
and must exit 0 when there is nothing left to remove -- a second teardown is
not an error.

Everything the harness creates is labelled. Removal is always label-scoped and
never matches on a name pattern: this machine already carries containers and
volumes belonging to other work, and a teardown that swept by name would eat
them.
"""

from __future__ import annotations

import atexit
import signal
import subprocess
import types
from collections.abc import Callable
from dataclasses import dataclass, field

LABEL_KEY = "com.dsel.run"
MANAGED_LABEL = "com.dsel.managed"


def label_flags(run_id: str) -> list[str]:
    """Labels stamped on every resource the harness creates."""
    return ["--label", f"{MANAGED_LABEL}=true", "--label", f"{LABEL_KEY}={run_id}"]


def _docker(args: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def list_managed(run_id: str, kind: str) -> list[str]:
    """Ids of managed resources of `kind` ("container" or "volume") for a run."""
    if kind == "container":
        args = ["ps", "-a", "--filter", f"label={LABEL_KEY}={run_id}", "--quiet"]
    elif kind == "volume":
        args = ["volume", "ls", "--filter", f"label={LABEL_KEY}={run_id}", "--quiet"]
    else:
        raise ValueError(f"unknown resource kind {kind!r}")
    result = _docker(args)
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


@dataclass
class Teardown:
    """Removes everything labelled for one run, idempotently.

    Registered with atexit and the termination signals on construction, so a
    crash between provision and teardown still cleans up.
    """

    run_id: str
    remove_volumes: bool = True
    _registered: bool = field(default=False, repr=False)
    _done: bool = field(default=False, repr=False)

    def register(self) -> Teardown:
        """Arm teardown for exit, SIGINT and SIGTERM."""
        if self._registered:
            return self
        atexit.register(self.run)
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous = signal.getsignal(sig)
            signal.signal(sig, self._handler(sig, previous))
        self._registered = True
        return self

    def _handler(
        self, sig: int, previous: object
    ) -> Callable[[int, types.FrameType | None], None]:
        def handle(signum: int, frame: types.FrameType | None) -> None:
            self.run()
            if callable(previous):
                previous(signum, frame)
            else:
                raise KeyboardInterrupt if sig == signal.SIGINT else SystemExit(128 + sig)

        return handle

    def run(self) -> int:
        """Remove this run's resources. Returns the number removed.

        Safe to call repeatedly: a run with nothing left removes nothing and
        succeeds. Only labelled resources are touched.
        """
        removed = 0
        for container in list_managed(self.run_id, "container"):
            if _docker(["rm", "--force", "--volumes", container]).returncode == 0:
                removed += 1
        if self.remove_volumes:
            for volume in list_managed(self.run_id, "volume"):
                if _docker(["volume", "rm", "--force", volume]).returncode == 0:
                    removed += 1
        self._done = True
        return removed

    def __enter__(self) -> Teardown:
        return self.register()

    def __exit__(self, *exc: object) -> None:
        self.run()
