"""Measuring the app tier's own ceiling (PLAN.md S13).

*"Measure the `/noop` ceiling before wiring PATH B, so app-tier saturation is a
known number, not a surprise."*

The surprise it prevents is the expensive one. If the tier saturates before the
engine does, every candidate returns the tier's ceiling, the differences between
engines vanish, and the harness reports with total confidence that the choice
does not matter. That failure is silent -- the numbers look fine, they are just
all the same -- so the ceiling has to be a number in the manifest before any
PATH B run is scheduled, and PATH B is then scheduled at a fraction of it.

The ceiling is measured against `/noop`: no pool, no engine, a constant body.
Anything else would fold the engine into a figure that is supposed to be about
the tier alone.

Two numbers come out, and they are not the same:

* **`saturation_rate_per_s`** -- where the tier's CPU crosses the 70% gate.
  This is the ceiling PATH B is scheduled against.
* **`max_delivered_rate_per_s`** -- the highest rate the tier actually served.
  Usually higher, and useless as a planning number: it is measured *past* the
  gate, so any result taken there is already invalid.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from dsel.driver.ramp import RampPlan, RampStep, run_ramp
from dsel.driver.transport import HttpFactory
from dsel.metrics.validity import (
    APP_CPU_LIMIT_PCT,
    LOCAL_CEILING_FRACTION,
    GateResult,
    app_cpu_gate,
)


class CeilingError(RuntimeError):
    """The ceiling could not be measured. Never guessed instead."""


def read_cpu_pct(host: str, port: int, timeout_s: float = 5.0) -> float | None:
    """The tier's own cgroup CPU reading, straight from `/cpu`."""
    url = f"http://{host}:{port}/cpu"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            payload = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise CeilingError(f"could not read {url}: {exc}") from exc
    value = payload.get("cpu_pct")
    return float(value) if value is not None else None


def wait_ready(host: str, port: int, timeout_s: float = 60.0) -> None:
    """Block until `/healthz` answers, so a probe does not race start-up."""
    import time

    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=2) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError) as exc:
            last = str(exc)
        time.sleep(0.25)
    raise CeilingError(f"app tier on {host}:{port} never became ready ({last})")


SAMPLE_INTERVAL_S = 0.4


@dataclass
class CpuSampler:
    """Polls the tier's `/cpu` on a thread while a step is under load.

    Reading once *after* a step is over measures the wrong thing: the ramp
    tears down its worker processes between steps, so the tier's most recent
    one-second window is part load and part idle. Measured that way the CPU
    curve came out non-monotonic -- 93.7% at 1400/s and 65.1% at 2800/s -- and
    the gate closed again above the ceiling it had already crossed.

    The statistic per step is the **peak**, not the mean. A tier that spends
    part of a step above the limit is saturated during that part, and the
    requests served then are not measuring the engine.
    """

    host: str
    port: int
    interval_s: float = SAMPLE_INTERVAL_S
    samples: list[tuple[float, float]] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="dsel-cpu-probe", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                value = read_cpu_pct(self.host, self.port)
            except CeilingError:
                continue
            if value is not None:
                self.samples.append((time.monotonic(), value))

    def peak_between(self, start: float, end: float) -> float:
        window = [cpu for at, cpu in self.samples if start <= at <= end]
        return max(window) if window else 0.0


@dataclass(frozen=True, slots=True)
class AppCeiling:
    """The tier's measured limit, and the gate reading that defines it."""

    steps: tuple[RampStep, ...]
    cpu_pct_by_rate: tuple[tuple[float, float], ...]
    limit_pct: float = APP_CPU_LIMIT_PCT

    @property
    def saturation_rate_per_s(self) -> float | None:
        """The lowest offered rate at which the CPU gate fires."""
        for rate, cpu_pct in self.cpu_pct_by_rate:
            if cpu_pct > self.limit_pct:
                return rate
        return None

    @property
    def max_delivered_rate_per_s(self) -> float:
        return max((step.achieved_rate_per_s for step in self.steps), default=0.0)

    @property
    def headroom_rate_per_s(self) -> float | None:
        """The highest rate measured with the gate still closed."""
        under = [rate for rate, cpu in self.cpu_pct_by_rate if cpu <= self.limit_pct]
        return max(under) if under else None

    @property
    def path_b_rate_per_s(self) -> float | None:
        """What S14 may schedule PATH B at: a fraction of the saturation point."""
        ceiling = self.saturation_rate_per_s or self.headroom_rate_per_s
        return ceiling * LOCAL_CEILING_FRACTION if ceiling else None

    def gate_at(self, rate_per_s: float) -> GateResult:
        """The gate as it read at one offered rate."""
        for rate, cpu_pct in self.cpu_pct_by_rate:
            if rate == rate_per_s:
                return app_cpu_gate(cpu_pct, self.limit_pct)
        raise CeilingError(f"no CPU reading at {rate_per_s}/s")

    def table(self) -> str:
        lines = [
            f"{'offered':>9} {'achieved':>9} {'p50 us':>9} {'app cpu %':>10}  gate",
            f"{'-' * 9} {'-' * 9} {'-' * 9} {'-' * 10}  {'-' * 24}",
        ]
        by_rate = dict(self.cpu_pct_by_rate)
        for step in self.steps:
            cpu_pct = by_rate.get(step.offered_rate_per_s, 0.0)
            gate = app_cpu_gate(cpu_pct, self.limit_pct)
            lines.append(
                f"{step.offered_rate_per_s:>9.0f} {step.achieved_rate_per_s:>9.0f} "
                f"{step.p50_us:>9.0f} {cpu_pct:>10.1f}  "
                f"{gate.verdict}{'(' + gate.reason + ')' if gate.reason else ''}"
            )
        lines += [
            f"{'saturates at':>15}: "
            + (
                "not reached"
                if self.saturation_rate_per_s is None
                else f"{self.saturation_rate_per_s:.0f}/s"
            ),
            f"{'PATH B rate':>15}: "
            + (
                "unknown"
                if self.path_b_rate_per_s is None
                else f"{self.path_b_rate_per_s:.0f}/s "
                f"({LOCAL_CEILING_FRACTION:.0%} of the ceiling)"
            ),
        ]
        return "\n".join(lines)


def measure_ceiling(
    run_dir: Path,
    host: str,
    port: int,
    rates_per_s: tuple[float, ...],
    *,
    duration_s: float = 4.0,
    warmup_s: float = 1.0,
    workers: int = 4,
    scenario: str = "noop",
) -> AppCeiling:
    """Ramp `/noop` and read the tier's CPU at each step.

    The CPU reading is taken at the *end* of each step, while the load is still
    the load that produced it: a reading taken after teardown would be the
    tier's idle CPU, and the gate would never fire.
    """
    wait_ready(host, port)
    readings: list[tuple[float, float]] = []
    factory = HttpFactory(host=host, port=port, path_template="/noop")
    sampler = CpuSampler(host=host, port=port)
    boundary = time.monotonic()

    def after_step(step: RampStep) -> None:
        nonlocal boundary
        now = time.monotonic()
        readings.append((step.offered_rate_per_s, sampler.peak_between(boundary, now)))
        boundary = now

    plan = RampPlan(
        run_dir=run_dir,
        engine="app",
        scenario=scenario,
        ops=("noop",),
        rates_per_s=rates_per_s,
        duration_s=duration_s,
        warmup_s=warmup_s,
        workers=workers,
    )
    sampler.start()
    try:
        ramp = run_ramp(plan, factory, on_step=after_step)
    finally:
        sampler.stop()
    return AppCeiling(steps=ramp.steps, cpu_pct_by_rate=tuple(readings))
