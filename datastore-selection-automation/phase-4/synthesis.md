# Phase 4 — Synthesis

## 4.1 The shape of the answer

Phase 3 settled the argument that mattered most: **there is no off-the-shelf product
that does this.** There are excellent measurement tools, and there is one very young
project (`paradedb/benchmarker`) that has independently arrived at a similar
architecture for a narrower purpose. What does not exist is the thing the ask
describes — a system that takes a *business case* and returns an *auditable
recommendation*.

The gap is not in measurement. It is in the three things wrapped around measurement:

1. **A gate before the benchmark.** Every existing tool measures whatever you point it
   at. None of them refuse to benchmark a candidate that cannot satisfy a hard
   requirement. That refusal is the single most valuable behaviour in the system,
   because a fast wrong answer is more dangerous than no answer.
2. **Parity enforcement with verification.** Phase 3 showed that fair comparison is
   not a matter of passing the same flags. Three CPU-detection APIs disagree inside one
   container (E8); one engine reads the cgroup and another reads `/proc/cpuinfo`
   (ClickHouse vs MongoDB, same host, same flags); a storage backend choice inflates
   the noise floor 14× (E4.1); a metric silently reports 8.19 kB instead of 6.44 GB
   (E4.3). Parity has to be *asserted and read back*, per engine, per run.
3. **An audit bundle that a stranger can act on.** Not a PDF of charts — a
   content-addressed set of inputs from which the inputs can be reconstructed
   byte-identically and the arithmetic recomputed without re-running anything.

So the POC builds the wrapper and borrows the measurement.

## 4.2 Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. BUSINESS CASE (prose, human)                                             │
│    "Orders must never oversell. 40k orders/mo, +8%/mo. Ops reports daily."   │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 │  human authoring, assisted by a template
                                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 2. WORKLOAD SPEC  (YAML → Pydantic → JSON Schema 2020-12)                    │
│    requirements(GATES) · data(entities,cardinality,skew) · access_patterns   │
│    load(open-loop,rates,repeats) · candidates · resources · scoring(weights) │
│    identity = SHA-256( RFC-8785-canonical( defaults-materialised JSON ) )    │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 3. GATE   — pure function, NO containers started                             │
│    capability table (per engine, per VERSION) × requirements                 │
│    licence policy × SPDX-pinned-per-digest                                   │
│    environment feasibility (e.g. MongoDB 8 ✗ on kernel ≥6.19)                │
│    ── excluded candidates are NEVER scored; exclusion ≠ low score ──          │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 4. RUN MATRIX  = survivors × scenarios × offered_rates × repeats             │
│    resolved & frozen up front, so the bundle enumerates what SHOULD run      │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 5. DATASET  — generated ONCE, shared by every candidate                      │
│    value = f(blake2b(seed│table│row_id│column))   ← index-derived, not a      │
│    PRNG stream. Order-, parallelism- and process-independent. Hashed.        │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 6. EXECUTION — per cell, serialised (never parallel: cells would contend)    │
│                                                                              │
│   ADAPTER: gate → provision → init → load → run → collect → teardown         │
│                     │          │      │      │       │         │             │
│      digest-pinned ─┘          │      │      │       │         └─ idempotent │
│      cpuset+quota+mem          │      │      │       └─ trust-flagged metrics│
│      named volume ONLY         │      │      └─ open-loop, HdrHistogram      │
│      TCP health gate           │      └─ deterministic load + settle         │
│                                └─ explicit knobs, then READ BACK             │
│                                                                              │
│   every phase emits Evidence{commands[], readback{}, artifacts{sha256}, dur} │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 7. METRICS — raw histograms (not summaries) + container + engine-internal    │
│    VALIDITY GATES: driver CPU >70% ⇒ INVALID · wrong FS ⇒ INVALID            │
│                    readback ≠ envelope ⇒ INVALID · error rate ⇒ INVALID      │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 8. SCORING — reference-anchored normalisation (NOT min-max: see §4.4)        │
│    vetoes (non-compensatory) → weights (measured ∥ judged, shown apart)      │
│    → rank → sensitivity sweep → Pareto front                                 │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 9. AUDIT BUNDLE (content-addressed, merkle root)                             │
│    manifest.json · spec · env capture · per-cell evidence · raw histograms   │
│    · dataset hash + seed · scoring inputs & arithmetic · fidelity_gap prose  │
│    ── third party: re-derive inputs, verify hashes, re-run, compare to the   │
│       recorded REPEATABILITY BAND (not to a point estimate) ──               │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 4.3 Stack decisions and rejected alternatives

| Layer | Chosen | Rejected, and why |
|---|---|---|
| **Orchestrator language** | **Python 3.13** | *Go*: better concurrency and a static binary, but loses Pydantic — the only library doing defaulting + validation + JSON Schema 2020-12 export + structured errors together. The orchestrator is I/O-bound and schema-heavy; the GIL objection applies to load generation, which is delegated. *Node/TS*: weaker DB client coverage and no stats ecosystem. |
| **Spec schema** | **Pydantic v2 → JSON Schema 2020-12**, hashed via RFC 8785 JCS | *CUE*: JSON Schema export is self-declared experimental (*"may, and probably will, change from release to release"*) — fatal for a hashable artifact; Go-only; pre-1.0 after 8 years. *Dhall*: the only language with semantic hashing built in, and genuinely tempting, but a stale Python binding and a tiny talent pool. *Protobuf*: Google's own docs say serialisation "is not (and cannot be) canonical" — disqualifying. *OpenAPI/TypeSpec*: HTTP-shaped; TypeSpec adds a JS build step. |
| **Load generation** | **First-party driver** over official client libs, with **HdrHistogram** for recording and the **pgbench scheduling model** for open-loop | *NoSQLBench*: the strongest off-the-shelf option — real adapter SPI loadable from external JARs, proper CO correction — but JVM warmup moves scores ~13% (QuestDB's measurement), enough to reorder rankings, and its YAML DSL competes with our spec. *k6+xk6-sql*: excellent open-loop model, but every new driver means recompiling the binary, no Mongo/Cassandra extension under a recognised org, and mixed Apache/AGPL sub-modules. *YCSB*: no seeding at all (`ThreadLocalRandom`) so datasets are irreproducible; last release 2019. *TSBS*: no p95/p99 whatsoever; dormant since 2021. *sysbench*: two engines only; last release 2020. |
| **Cross-check** | **pgbench, valkey-benchmark, clickhouse-benchmark** as calibration | Not used as the primary path, but if our driver disagrees with pgbench beyond the noise floor, our driver is wrong. Cheap insurance against the bug class that invalidates everything silently. |
| **Containerisation** | **Docker Engine + Compose v2 (v5.x)**, `deploy.resources.limits` + top-level `cpuset` | *Kubernetes (kind/k3d)*: the one feature that would justify it — CPU Manager static policy for exclusive pinning — does not work in nested containers, and k3d's `--servers-memory` only fakes `/proc/meminfo` with no cgroup enforcement. You pay control-plane overhead for weaker guarantees than `docker run` gives directly. *Testcontainers*: excellent for tests, wrong shape here — reuse semantics are experimental (and default **on** in Node), and rootless Podman disables Ryuk cleanup entirely. We need explicit lifecycle control, not implicit. *Podman*: viable and Apache-2.0 (worth supporting later), but rootless silently ignores `--cpus`/`--cpuset-cpus` without systemd cgroup delegation — a silent fairness hole. |
| **Image pinning** | **OCI index digest** in the spec; resolved **platform digest** recorded per run | *Tags*: mutable. *Platform manifest digests*: reproducible but architecture-locked, so the same spec cannot run on both a dev Mac and amd64 CI. |
| **Toolchain provisioning** | **Nix flakes** (or devbox) | *Homebrew*: its docs state a Brewfile lock file will never exist; bottles are macOS-version-specific. *asdf*: no hashes, ever. *uv*: pins Python and PyPI only — cannot provision `psql`, `mongosh`, `redis-cli` or `clickhouse-client`; it is a component, not the answer. *pixi*: close second, but `mongosh` is absent from conda-forge. |
| **Dataset generation** | **First-party, index-derived** (`blake2b(seed│table│row_id│column)`) | *Faker/Mimesis*: documented as not stable across versions — "pin down to the patch number". *NumPy*: NEP 19 explicitly refuses stream-compatibility guarantees. *SDV*: **BUSL-1.1**, not open source, with a "Synthetic Data Service" carve-out that becomes a live legal question if the harness is ever hosted. *Snowfakery*: no seed parameter at all. Index-derived generation sidesteps the version-pinning problem entirely — verified byte-identical across ordering, threading and process boundaries. |
| **Skew model** | **`(hot_fraction, hot_traffic)`** | *Zipf θ*: equally expressive, but not checkable by the person who knows the business. "20% of SKUs get 80% of traffic" can be confirmed or corrected by a product manager; θ=0.99 cannot. Both must be decoupled from key locality (YCSB scrambles with FNV; PG17 added `permute()`) or you benchmark sequential locality instead of skew. |
| **Normalisation** | **Reference-anchored** against spec-declared anchors | *Min-max within the candidate set*: **rejected on evidence**. exp10 demonstrated it literally reversing the winner when an irrelevant candidate was added, and collapsing a 0.97-vs-0.99 photo finish into 0.0-vs-1.0. See §4.4. |
| **Aggregation** | **Weighted sum + non-compensatory vetoes + Pareto front + sensitivity sweep** | Pure weighted sum is compensatory: a fatal weakness hides behind good averages. The veto fixes that. TOPSIS was considered and rejected — it reintroduces candidate-set dependence through its ideal/anti-ideal points, the same defect as min-max. |

## 4.4 The normalisation finding

This deserves its own section because it changed the design.

The obvious approach — min-max normalise each criterion across the candidate set —
fails in two ways that were demonstrated live in `exp10`:

- **Magnitude destruction.** SLO attainment of 0.97 vs 0.99 normalises to 0.000 vs
  1.000, identical to what 0.10 vs 0.99 would produce. The model cannot distinguish a
  photo finish from a landslide, which is precisely the distinction a decision-maker
  needs.
- **Rank reversal.** Adding a third candidate that wins on nothing changed the ranking
  from `B > A` to `A > B`. Not the scores — the *ranking*. A recommendation that
  depends on which losers happened to be included is not a recommendation.

Reference-anchored normalisation fixes both. Each criterion carries a
spec-declared `[value scoring 0.0, value scoring 1.0]` pair drawn from business facts —
"1× headroom is the floor, 5× is plenty", "the SLO veto floor is 0.80". Scores then
become candidate-set-independent (A scored 0.7251 with and without C present) and the
A-vs-B gap shows as 0.0040, correctly reading as a photo finish.

The cost is that someone must author the anchors. That is a feature: it forces the
business assumptions into the reviewed spec rather than letting them emerge from an
accident of which candidates were tested.

## 4.5 Risks and failure modes

| Risk | Severity | Mitigation |
|---|---|---|
| **False authority.** A correctly-measured number, stripped of caveats, quoted months later as settling a question it never addressed. | **Highest** | Mandatory `fidelity_gap` prose reproduced in every report; measured and judged criteria rendered separately; every report states what it did not measure; refuse to emit a report when candidates were excluded for environmental reasons. |
| Dev-machine numbers escaping into a report | High | Harness refuses to emit a signed report from a virtualised-FS host. macOS is for pipeline validation only. Justified by E4.1 (14× noise), E5.3 (1.87× spread), E9 (`rotational=1` mis-tunes engines *unequally*), E7 (a candidate silently missing). |
| Unfair comparison from an unnoticed asymmetry | High | Readback verification of every knob; validity gates that invalidate rather than report; the pgbench/valkey-benchmark calibration cross-check. |
| The workload spec doesn't match reality | High | It cannot be fixed technically. Mitigated by requiring the `fidelity_gap`, by driving cardinality/skew from production telemetry where it exists, and by re-running the spec against real traffic once the system ships. |
| Weights are arbitrary | Medium | Attributed to a named author, justified in prose, sensitivity-swept, breaking-point reported per weight, Pareto front shown so readers can see when weights are doing the real work. |
| THP cannot be satisfied for all engines simultaneously | Medium | Unresolvable — it is host-global and not namespaced, and MongoDB 8+ wants `always` while Redis wants `never` and ClickHouse wants `madvise`. Fix one value for all engines, record it, name it as a confound with the direction of bias where known. |
| Engine licence changes underneath us | Medium | SPDX pinned per image digest, re-verified each run; a change is a loud failure. Redis changed licence twice in 14 months. |
| Benchmark-publication legal exposure (DeWitt clauses) | Medium | OSS-only candidate set in v1; scoped as internal decision support; refuse comparative reports involving a commercial engine without a recorded approval token. Note ClickBench's assets are CC BY-NC-SA 4.0 — NonCommercial. |
| Run matrix too slow to be useful | Medium | Measured ~2 min per cell ⇒ 1–2 h for 5×4×3. `--smoke` mode for the inner loop; `--full` results are the only reportable ones. |
| Composite candidates get double the hardware | Medium | `resources.composite_split` is mandatory for multi-engine candidates. |
| Over-fitting to the harness's own bugs | Medium | Cross-check against native tools; publish the noise floor per cell; treat any result inside the noise band as "no measured difference" rather than a ranking. |

## 4.6 Honest limits

The system quantifies the quantifiable part of a decision and records how. It does not
make the decision. It cannot speak to operational burden over five years, scale-out
behaviour, failure and recovery, migration cost, team fit, or the query nobody has
thought of yet — and that last one is where the most expensive datastore mistakes
actually come from. Those enter as human-weighted criteria, visibly separated from
measurement, and the report's job is to make the seam between evidence and judgement
impossible to miss.
