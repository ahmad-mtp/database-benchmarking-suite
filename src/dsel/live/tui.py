"""The live TUI (PLAN.md S8a).

D9: build this before Grafana. Grafana is AGPL-3.0 and could stall at legal
review, and the live-observability requirement must not depend on it. This
alone satisfies that requirement.

Rendering is a pure function of `ScreenState`, so the live view and
`--replay` cannot diverge: they share the reducer and the renderer, and differ
only in where the records come from.
"""

from __future__ import annotations

import io
import time
from collections.abc import Iterator
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from dsel.live.merge import find_shards, merge_records, read_merged
from dsel.live.schema import AnyRecord
from dsel.live.state import ScreenState, apply
from dsel.live.tail import LiveTailer

VERDICT_STYLE = {
    "OK": "green",
    "FLAG": "yellow",
    "INVALID": "bold red",
    "INCONCLUSIVE_DRIVER_BOUND": "bold magenta",
}


def _phase_bar(state: ScreenState) -> Text:
    """The phase strip, with the warmup -> measure boundary marked."""
    text = Text()
    for phase in (
        "gate",
        "provision",
        "init",
        "load",
        "warmup",
        "measure",
        "collect",
        "teardown",
    ):
        if phase == "measure" and state.warmup_ended_t_ms is not None:
            # The boundary S8a requires visible in both live and replay.
            text.append(" ┃ ", style="bold cyan")
        done = phase in state.completed_phases
        current = state.phase == phase
        style = "bold reverse cyan" if current else ("green" if done else "dim")
        text.append(f" {phase} ", style=style)
    return text


def _ops_table(state: ScreenState) -> Table:
    table = Table(title="operations", expand=True, title_style="dim")
    for column in ("op", "rate/s", "count", "errors", "p50 µs", "p99 µs"):
        table.add_column(column, justify="right" if column != "op" else "left")
    for op in sorted(state.ops.values(), key=lambda o: o.op):
        table.add_row(
            op.op,
            f"{op.rate_per_s:,.0f}",
            f"{op.count:,}",
            Text(f"{op.errors:,}", style="red" if op.errors else "dim"),
            "-" if op.p50_us is None else f"{op.p50_us:,.0f}",
            "-" if op.p99_us is None else f"{op.p99_us:,.0f}",
        )
    return table


def _containers_table(state: ScreenState) -> Table:
    table = Table(title="containers", expand=True, title_style="dim")
    for column in ("container", "mem", "mem %", "pids", "throttled µs"):
        table.add_column(column, justify="right" if column != "container" else "left")
    for c in sorted(state.containers.values(), key=lambda c: c.container):
        pct = c.memory_pct
        table.add_row(
            c.container[:34],
            "-" if c.memory_current is None else f"{c.memory_current / 1024**2:,.0f}M",
            "-" if pct is None else Text(f"{pct:.1f}", style="red" if pct > 90 else "default"),
            "-" if c.pids_current is None else str(c.pids_current),
            "-" if c.cpu_throttled_usec is None else f"{c.cpu_throttled_usec:,}",
        )
    return table


def _validity_table(state: ScreenState) -> Table:
    table = Table(title="validity gates", expand=True, title_style="dim")
    for column in ("gate", "verdict", "observed", "limit"):
        table.add_column(column, justify="left")
    if not state.validity:
        table.add_row(Text("none fired", style="dim"), "", "", "")
    for gate in sorted(state.validity):
        record = state.validity[gate]
        table.add_row(
            gate,
            Text(record.verdict, style=VERDICT_STYLE.get(record.verdict, "default")),
            "-" if record.observed is None else str(record.observed),
            "-" if record.limit is None else str(record.limit),
        )
    return table


def render(state: ScreenState) -> Panel:
    """A pure function of the state. Holds nothing of its own."""
    header = Text()
    header.append(f"cell {state.cell or '-'}", style="bold")
    header.append(f"   elapsed {state.elapsed_ms / 1000:,.1f}s")
    header.append(f"   records {state.records_seen:,}")
    header.append(f"   rate {state.total_rate:,.0f}/s")
    if state.total_errors:
        header.append(f"   errors {state.total_errors:,}", style="red")
    header.append("   ")
    header.append(state.worst_verdict, style=VERDICT_STYLE.get(state.worst_verdict, "default"))
    if state.in_measurement_window:
        header.append("   ● MEASURING", style="bold cyan")

    note = Text(
        "latency shown is a within-window estimate, not the reported figure "
        "(the .hlog is authoritative)",
        style="dim italic",
    )
    body = Group(
        header,
        _phase_bar(state),
        Text(),
        _ops_table(state),
        _containers_table(state),
        _validity_table(state),
        note,
    )
    return Panel(body, title="dsel watch", border_style="cyan")


def replay_records(run_dir: Path) -> Iterator[AnyRecord]:
    """Records from a finished run: the merged file, or its shards."""
    merged = run_dir / "metrics.ndjson"
    if merged.is_file():
        yield from read_merged(merged)
        return
    shards = find_shards(run_dir / "shards")
    if not shards:
        raise FileNotFoundError(f"no metrics.ndjson and no shards under {run_dir}")
    yield from merge_records(shards)


def watch_replay(run_dir: Path, console: Console | None = None) -> ScreenState:
    """Replay a finished run, returning the final screen state."""
    console = console or Console()
    state = ScreenState()
    with Live(render(state), console=console, refresh_per_second=8, transient=False) as live:
        for record in replay_records(run_dir):
            state = apply(state, record)
            live.update(render(state))
    return state


def screen_text(state: ScreenState, width: int = 100) -> str:
    """The rendered screen as plain text, for comparison and for the record.

    S8a asks for the warmup -> measure boundary to be *visibly* marked. That is
    only checkable against what was actually drawn, so the same renderer is run
    into a recording console rather than a second description of it.
    """
    console = Console(width=width, record=True, file=io.StringIO(), no_color=True)
    console.print(render(state))
    return console.export_text()


def watch_live(
    run_dir: Path,
    poll_s: float = 0.25,
    idle_timeout_s: float | None = None,
    console: Console | None = None,
) -> ScreenState:
    """Tail a run's shards while they are written, returning the final state.

    Records arrive through `LiveTailer`, which applies the same total order the
    offline merge does. That is what makes S8a's criterion hold: live and
    replay share the reducer, the renderer *and* the ordering, so the only
    remaining difference is when each record showed up.

    The metrics stream is the single source of truth (D3) -- the TUI is a
    consumer of it exactly like the Prometheus exporter, never a second
    sampling path, so nothing here touches Docker.
    """
    console = console or Console()
    state = ScreenState()
    tailer = LiveTailer(run_dir / "shards")
    last_change = time.monotonic()
    seen = 0
    with Live(render(state), console=console, refresh_per_second=8) as live:
        while True:
            for record in tailer.poll():
                state = apply(state, record)
            if tailer.ingested_count > seen:
                seen, last_change = tailer.ingested_count, time.monotonic()
                live.update(render(state))
            if idle_timeout_s is not None and time.monotonic() - last_change > idle_timeout_s:
                break
            time.sleep(poll_s)
        # The run is over, so the watermark no longer holds anything back.
        for record in tailer.close():
            state = apply(state, record)
        live.update(render(state))
    return state
