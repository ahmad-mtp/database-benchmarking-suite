# database-benchmarking-suite

A decision harness that takes a plain-language business use case and returns an auditable
datastore recommendation. `GATE → MEASURE → WEIGH`.

<!-- honesty:begin -->
## What this harness will and will not claim

`dsel` produces **mechanisms, relationships and scaling curves** — where the knee is, how
throughput moves as connections rise, what saturates first and why. It does **not** produce
reportable capacity numbers. Every run on this machine is stamped `profile=local`,
`envelope_deviation=true` and `reportable=false`, and `dsel verify` refuses to sign a report
carrying `reportable=false`. A claim of the form "this serves N orders per second" is not
something this build will emit, because that number would not be true on any other hardware.

That refusal is enforced by code, not by discipline. The target machine is a single Apple
M5 laptop running Docker Desktop: cores are shared between the engine, the load driver, the
app tier and the observability stack, the filesystem is a VM's, and at least one candidate
engine cannot start here at all. Those are the conditions under which mechanisms remain
learnable and absolute numbers do not. What transfers to real hardware is the shape of the
curve and the identity of the bottleneck — carried across by changing a resource envelope,
not by rewriting the harness.
<!-- honesty:end -->

## Layout

| Path | What it is |
|---|---|
| `src/dsel/` | The harness |
| `tests/` | `unit/`, `contract/` (adapters satisfy the Protocol), `e2e/` (real containers) |
| `PLAN.md` | The build plan: locked decisions, phases S0–S19 and W1–W4, per-phase acceptance criteria |
| `datastore-selection-automation/` | The completed research this is built from. `findings.md` is the specification; `phase-3/` is verification evidence |

Read `datastore-selection-automation/findings.md` §1 before changing anything: it compresses
the whole research result. `CLAUDE.md` lists the invariants that must not be simplified away.

## Requirements

- [uv](https://docs.astral.sh/uv/) — the only prerequisite; it fetches Python 3.13 itself
- Docker Desktop, for every phase that provisions an engine

## Usage

```bash
uv sync --frozen        # create the environment from the committed lock
uv run dsel --version   # harness version, git commit, dirty flag
uv run dsel --help      # the subcommand tree
uv run pytest tests/unit -q
```

The subcommand tree is declared in full from the first phase. A subcommand that has not been
built yet exits non-zero and names the phase that will build it, rather than exiting 0 and
doing nothing.

## Status

Under construction, phase by phase against `PLAN.md`. A phase is finished only when its
`Accept:` criterion has been run as a command and passed.
