"""`docker events` watcher (PLAN.md S7).

An OOM kill or an unexpected `die` invalidates a cell, and neither shows up in
a 1 Hz stats poll -- the container is simply gone by the next tick. The event
stream is the only place these are observable at the moment they happen.
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass

WATCHED_ACTIONS = ("oom", "die", "kill", "health_status", "stop")


@dataclass(frozen=True, slots=True)
class DockerEvent:
    """One container lifecycle event."""

    t_ms: int
    action: str
    container: str
    exit_code: int | None = None
    health_status: str | None = None

    @property
    def is_fatal(self) -> bool:
        """An event that invalidates whatever was being measured."""
        return self.action == "oom" or (
            self.action == "die" and self.exit_code not in (0, None)
        )


def _parse(line: str) -> DockerEvent | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    action = payload.get("Action", "")
    attrs = (payload.get("Actor") or {}).get("Attributes") or {}
    health: str | None = None
    if action.startswith("health_status"):
        _, _, status = action.partition(": ")
        health, action = status or None, "health_status"
    exit_code = attrs.get("exitCode")
    return DockerEvent(
        t_ms=int(payload.get("timeNano", 0)) // 1_000_000,
        action=action,
        container=attrs.get("name", payload.get("id", "")[:12]),
        exit_code=int(exit_code) if exit_code not in (None, "") else None,
        health_status=health,
    )


def stream(run_id: str, label_key: str = "com.dsel.run") -> Iterator[DockerEvent]:
    """Yield events for one run's containers until the process is stopped."""
    args = [
        "docker",
        "events",
        "--format",
        "{{json .}}",
        "--filter",
        f"label={label_key}={run_id}",
        "--filter",
        "type=container",
    ]
    for action in WATCHED_ACTIONS:
        args += ["--filter", f"event={action}"]
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None
        for line in process.stdout:
            event = _parse(line.strip())
            if event is not None:
                yield event
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


class EventWatcher:
    """Runs `stream` on a background thread, collecting events."""

    def __init__(
        self, run_id: str, on_event: Callable[[DockerEvent], None] | None = None
    ) -> None:
        self.run_id = run_id
        self.events: list[DockerEvent] = []
        self._on_event = on_event
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _pump(self) -> None:
        for event in stream(self.run_id):
            self.events.append(event)
            if self._on_event is not None:
                self._on_event(event)
            if self._stop.is_set():
                break

    def start(self) -> EventWatcher:
        self._thread = threading.Thread(target=self._pump, daemon=True, name="dsel-events")
        self._thread.start()
        return self

    def stop(self) -> list[DockerEvent]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        return self.events

    @property
    def fatal(self) -> list[DockerEvent]:
        return [e for e in self.events if e.is_fatal]

    def __enter__(self) -> EventWatcher:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
