"""The `dsel` command line.

The full subcommand tree is declared from the first phase so the shape of the
harness is visible, but an unimplemented subcommand exits non-zero naming the
phase that will build it. A stub that exits 0 would be a command that lies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from dsel.version import provenance

app = typer.Typer(
    name="dsel",
    help="Datastore selection decision harness: gate, measure, weigh.",
    no_args_is_help=True,
    add_completion=False,
)

# Subcommand -> the phase of PLAN.md that implements it.
_PENDING: dict[str, str] = {
    "gate": "G1",
    "plan": "S6",
    "run": "S10",
    "score": "G2",
    "verify": "G3",
    "report": "G2",
}


def _not_implemented(command: str) -> None:
    phase = _PENDING[command]
    typer.secho(
        f"dsel {command}: not implemented (phase {phase})",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(code=2)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(str(provenance()))
        raise typer.Exit(code=0)


@app.callback()
def root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Print the harness version, git commit and dirty flag.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Datastore selection decision harness."""


@app.command()
def env(
    image: Annotated[
        str,
        typer.Option(help="Probe image; pinned to its index digest before use."),
    ] = "python:3.13-slim",
    probe: Annotated[
        bool,
        typer.Option("--probe/--no-probe", help="Run the vCPU speed probe."),
    ] = True,
    repeats: Annotated[int, typer.Option(help="Probe repeats per vCPU.")] = 6,
    interference: Annotated[
        bool,
        typer.Option(
            "--interference/--no-interference",
            help="Sweep cross-cpuset interference (~2 min). Both directions.",
        ),
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option(help="Write the manifest here instead of stdout."),
    ] = None,
) -> None:
    """Capture the environment and probe per-vCPU speed (PLAN.md S1)."""
    from dsel.audit.environment import build_manifest, resolve_image
    from dsel.audit.interference import (
        DRIVER_CPUSET_SPEC,
        ENGINE_CPUSET_SPEC,
        reasons_for,
        sweep,
        to_record,
    )
    from dsel.audit.vcpu_probe import run_probe

    pin = resolve_image(image)
    reasons: list[str] = []
    probe_result = None
    if probe:
        probe_result, reasons = run_probe(pin, repeats=repeats)

    records = []
    if interference:
        sweeps = [
            sweep(pin, ENGINE_CPUSET_SPEC, DRIVER_CPUSET_SPEC),
            sweep(pin, DRIVER_CPUSET_SPEC, ENGINE_CPUSET_SPEC),
        ]
        reasons += reasons_for(sweeps)
        records = [to_record(s) for s in sweeps]

    manifest = build_manifest(pin, reasons)
    updates: dict[str, object] = {}
    if probe_result is not None:
        updates["vcpu_probe"] = probe_result
    if records:
        updates["cpuset_interference"] = records
    if updates:
        manifest = manifest.model_copy(update=updates)
    payload = manifest.model_dump_json(indent=2)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload + "\n", encoding="utf-8")
        typer.echo(f"wrote {out}")
    else:
        typer.echo(payload)


@app.command()
def budget(
    app_tier: Annotated[bool, typer.Option("--app-tier/--no-app-tier")] = True,
    observability: Annotated[str, typer.Option(help="none | light | deep")] = "light",
) -> None:
    """Check a configuration against the machine before anything starts (S2)."""
    from dsel.audit.environment import capture_daemon
    from dsel.compose.budget import Budget, BudgetError, Observability, plan_local

    daemon = capture_daemon()
    plan = plan_local(
        Budget(total_vcpus=daemon.ncpu, total_memory_bytes=daemon.mem_total_bytes),
        with_app_tier=app_tier,
        observability=Observability(observability),
    )
    try:
        plan.check()
    except BudgetError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(plan.arithmetic())
    typer.secho("\nfits", fg=typer.colors.GREEN)


@app.command()
def gate() -> None:
    """Exclude candidates that fail a hard requirement, before any container starts."""
    _not_implemented("gate")


@app.command()
def plan() -> None:
    """Freeze the run matrix: survivors x scenarios x rates x repeats."""
    _not_implemented("plan")


@app.command()
def run() -> None:
    """Provision, load, measure and tear down each cell of the run matrix."""
    _not_implemented("run")


@app.command()
def score() -> None:
    """Reference-anchored normalisation, vetoes, weighting, sensitivity."""
    _not_implemented("score")


@app.command()
def verify() -> None:
    """Verify an audit bundle; refuse to sign anything stamped reportable=false."""
    _not_implemented("verify")


@app.command()
def report() -> None:
    """Render the decision report, measured and judged criteria kept apart."""
    _not_implemented("report")


@app.command()
def watch(
    replay: Annotated[
        Path | None,
        typer.Option(help="Replay a finished run directory instead of tailing a live one."),
    ] = None,
    run: Annotated[
        Path | None, typer.Option(help="Run directory to tail while it is being written.")
    ] = None,
    idle_timeout: Annotated[
        float | None, typer.Option(help="Exit after this many idle seconds (live mode).")
    ] = None,
    state_out: Annotated[
        Path | None,
        typer.Option(help="Write the final screen state as canonical JSON (S8a check)."),
    ] = None,
    screen_out: Annotated[
        Path | None,
        typer.Option(help="Write the final rendered screen as plain text (S8a check)."),
    ] = None,
    prometheus_port: Annotated[
        int | None,
        typer.Option(help="Also expose the run's metrics for Prometheus on this port."),
    ] = None,
) -> None:
    """Live TUI over the run's metrics stream; --replay re-runs a finished run."""
    import json

    from dsel.live.state import snapshot
    from dsel.live.tui import screen_text, watch_live, watch_replay

    target = replay or run
    if target is None:
        typer.secho("give --replay <run-dir> or --run <run-dir>", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    if not target.is_dir():
        typer.secho(f"{target} is not a directory", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    if prometheus_port is not None:
        # A second consumer of the same stream, never a second sampling path (D3).
        from dsel.live.exporter import serve

        serve(target, prometheus_port)
        typer.echo(f"exporting on http://127.0.0.1:{prometheus_port}/metrics")
    state = watch_replay(target) if replay else watch_live(target, idle_timeout_s=idle_timeout)
    # Both dumps come from the same functions the screen uses, so a diff of two
    # sessions is a diff of what was actually shown (PLAN.md S8a).
    if state_out is not None:
        state_out.write_text(
            json.dumps(snapshot(state), sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
    if screen_out is not None:
        screen_out.write_text(screen_text(state), encoding="utf-8")
    typer.echo(
        f"\nfinal: {state.records_seen:,} records, "
        f"{len(state.ops)} ops, {len(state.containers)} containers, "
        f"worst verdict {state.worst_verdict}"
    )


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
