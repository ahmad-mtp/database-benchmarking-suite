# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository currently is

A **completed research deliverable**, not an implementation. `datastore-selection-automation/`
holds a five-phase research project that produced a buildable POC plan for a system that
takes a plain-language business use case and returns an auditable datastore
recommendation. **No harness code exists yet** — there is no `pyproject.toml`, no
`package.json`, no build system, no test suite. Everything under `phase-3/` is
verification evidence, not a codebase.

If asked to *build* the system, `datastore-selection-automation/findings.md` is the
specification: §10 gives the target repo layout, §11 gives milestones M0–M7 with
mechanically-checkable acceptance criteria.

## Layout and reading order

| Path | What it is |
|---|---|
| `datastore-selection-automation/plan.md` | The research plan, all phases marked complete, plus a completion record listing the four findings that changed the design |
| `phase-1/scope.md` | Boundaries, the Tier-1 engine set, the three driving use cases (UC-1/2/3), and the operational definition of "auditable" (R1/R2/R3 tiers) |
| `phase-2/discovery.md` | ~40 tools surveyed with verified versions and licences |
| `phase-3/analysis.md` | Deep analysis; `experiments.md` is the lab notebook for experiments E1–E11 |
| `phase-4/synthesis.md` | Chosen stack with rejected alternatives, risk register |
| `findings.md` | **The deliverable.** 12 sections: architecture, component inventory, workload spec, adapter contract, containerisation, methodology, auditability, scoring model, repo layout, milestones, risks |
| `sources.md` | Every reference with URL and relevance note |

Read `findings.md` §1 (executive summary) before touching anything else — it compresses
the whole result.

## Running the phase-3 artifacts

These are archival evidence produced on 2026-09-01 (Apple M5 / macOS 26.6.2 / Docker
Desktop 4.88.1). Each `.sh` has a `.log` beside it holding its recorded output.

```bash
# Pure-stdlib Python 3, no dependencies, safe and fast to re-run:
python3 datastore-selection-automation/phase-3/exp06-determinism.py    # index-derived data generation
python3 datastore-selection-automation/phase-3/exp09-scoring.py        # GATE→NORMALISE→VETO→WEIGHT→RANK→SENSITIVITY→PARETO on UC-1
python3 datastore-selection-automation/phase-3/exp10-normalisation.py  # demonstrates min-max reversing the winner
python3 datastore-selection-automation/phase-3/exp11-anchored-sensitivity.py

# Docker-dependent; each pulls digest-pinned images, creates named volumes, and
# self-cleans via a trap. They mutate local Docker state — re-run deliberately:
datastore-selection-automation/phase-3/exp01-postgres.sh   # provision→health-gate→load→collect→teardown
datastore-selection-automation/phase-3/exp02-valkey-compose.sh
datastore-selection-automation/phase-3/exp03-autotune.sh   # cpuset vs quota vs both
datastore-selection-automation/phase-3/exp04-storage.sh    # bind mount vs named volume noise floor
datastore-selection-automation/phase-3/exp05-storage-db.sh
```

`adapter-contract.py` is a Protocol sketch, not executable evidence — it defines
`EngineAdapter`, `Evidence`, `ImagePin`, `ResourceEnvelope`, and is the reference for
`src/dsel/adapters/base.py` when implementation starts.

Verify a claim before restating it: `experiments.md` says outright that where a claim is
not backed by a log line, it says so. Do not soften a claim that logs support, and do not
harden one marked **UNVERIFIED**.

## The architecture in one line

`GATE → MEASURE → WEIGH`. This is a **decision harness, not a benchmark suite** — it
answers "which of {X, Y, Z} should we build on?", so it optimises for fair, comparable,
*ordinal* numbers rather than absolute peaks. That distinction drives nearly every
design choice; see `phase-1/scope.md` §1.

The pipeline: business case → workload spec (YAML → Pydantic v2, identity =
SHA-256 of RFC-8785-canonical JSON) → gate (pure function, no containers) → run matrix
(survivors × scenarios × rates × repeats, frozen up front) → dataset (generated once,
shared byte-identically) → execution (adapter: `gate → provision → init → load → run →
collect → teardown`, each phase emitting `Evidence`) → metrics with validity gates →
scoring → content-addressed audit bundle.

## Non-negotiable invariants

Each was demonstrated by experiment, not argued. Violating one silently invalidates
results, so do not "simplify" any of them away.

- **Gate before measuring.** A candidate that fails a hard requirement (transactional
  scope, durability, query capability, licence policy, environment feasibility) is
  excluded and *never benchmarked*. Exclusion is not a low score.
- **Named volumes only.** Bind-mounted storage raised run-to-run variance from 1.3% to
  18% and blinded `docker stats` BlockIO (reported 8.19 kB for 6.44 GB of writes).
  `ResourceEnvelope.storage` has exactly one legal value.
- **Pin the OCI *index* digest in the spec; record the resolved *platform* manifest
  digest in the run manifest.** Tags are mutable; platform digests are
  architecture-locked and make one spec unrunnable across a dev Mac and amd64 CI.
- **Set every engine knob explicitly, then read it back from the running engine.** No
  Docker flag combination makes CPU/memory detection agree across APIs — five APIs gave
  three answers in exp08; ClickHouse reads the cgroup while MongoDB reads
  `/proc/cpuinfo` on the same host with the same flags. Also set both `--cpuset-cpus`
  *and* `--cpus`: quota alone leaves the container seeing all host cores.
- **Reference-anchored normalisation, never min-max.** Min-max reversed the winner when
  an irrelevant candidate was added (exp10) and collapsed a 0.97-vs-0.99 finish into
  0.0-vs-1.0. `src/dsel/scoring/normalise.py` should have min-max deliberately absent.
- **Open loop, always, for any latency claim**, with HdrHistogram coordinated-omission
  correction. Store raw histograms in the bundle, never just summaries.
- **Cells run serialised.** Parallel cells contend and the load driver must be pinned to
  different cores from the engine.
- **Invalidate rather than report with a caveat** — driver CPU > 70%, wrong filesystem,
  readback ≠ envelope, error-rate breach, steady state not reached, or an
  implausible-by-order-of-magnitude result all mark a cell INVALID.
- **Deterministic data is index-derived**, `blake2b(seed│table│row_id│column)`, not a
  PRNG stream — order-, parallelism- and process-independent. Faker and NumPy both
  refuse cross-version stream stability; SDV is BUSL-1.1.
- **The dev machine cannot produce reportable numbers.** macOS Docker Desktop is for
  pipeline validation only; Linux CI is the sole source of results. MongoDB 8 fatally
  refuses to start on Docker Desktop's kernel (≥ 6.19, SERVER-121912), which silently
  changes the candidate set — "cannot run here" is a first-class adapter outcome.
- **The harness never picks the winner unilaterally.** Measured and human-judged
  criteria are scored separately and rendered apart; weights are attributed, justified,
  and sensitivity-swept; a human signs the ADR.

## Legal constraints that are actually load-bearing

- Reports are scoped as **internal decision support**. Commercial DBMS licences carry
  **DeWitt clauses** forbidding publication of benchmark results without vendor consent
  — this is why the v1 candidate set is OSS-only.
- Licence policy is a gate, not a score input. SPDX id is pinned **per image digest** and
  re-verified each run; a change is a loud failure. Redis changed licence twice in 14
  months.
- Known landmines already found: SDV is BUSL-1.1, ClickBench assets are CC BY-NC-SA
  (NonCommercial), TPC kits are under the TPC EULA, k6 is AGPL-3.0, MongoDB is SSPL
  (source-available, not OSI).

## Writing conventions in these documents

- **British spelling** throughout: normalisation, licence, containerisation, serialised.
- Absolute dates, never relative. Every version and licence claim carries the date it
  was verified.
- Claims are backed by a log line or explicitly marked **UNVERIFIED**. Rejected
  alternatives are named with the specific reason they lost — keep that pattern when
  extending any comparison table.
- Findings are numbered per experiment (E1.1, E4.3, …) and cross-referenced from the
  design docs; preserve those references when editing.
