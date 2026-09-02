# Phase 3 — Deep Analysis

Companion documents: `experiments.md` (what was executed and measured on this
machine), `workload-spec.example.yaml` (the spec, filled in for UC-1),
`adapter-contract.py` (the per-engine interface), `exp0*.sh` / `exp06-determinism.py`
(runnable scripts), `*.log` (raw output).

---

## 3.1 Harness deep-dive: buy, adapt, or build?

Three candidates survived Phase 2. The question is not "which is best" but "which
minimises the work we must do *and* the lies we must tell".

### NoSQLBench 5.25.12 (Apache-2.0)

The only mature harness that spans our engine families. Its adapter SPI is loadable
from an **external JAR**, so adding an engine does not mean forking. It corrects for
coordinated omission properly, splitting `servicetime` / `waittime` / `responsetime`
under a `cyclerate` limiter, and it ships CSV, Prometheus, SQLite and HdrHistogram
reporters plus an official container image.

Against it: it is a JVM, and QuestDB measured a **~13% score swing** from changing JIT
warmup from 3 to 10 iterations — enough to reorder rankings. That is a real,
quantified threat to a tool whose entire output is a ranking. It also has no adapter
for PostgreSQL-as-relational beyond generic JDBC, so the *interesting* part of UC-1
(multi-entity joins, transactional stock decrement) would be hand-written SQL anyway.
And its YAML DSL is a second spec language competing with ours.

### k6 v2.2.0 + xk6-sql (AGPL-3.0)

Genuinely good open-loop model (`constant-arrival-rate` is a first-class executor, and
the docs name coordinated omission explicitly). `paradedb/benchmarker` demonstrates the
exact architecture we want on top of it. Against it: each new driver requires
**recompiling the k6 binary** via `xk6 build`, which turns "add an engine" into a build
step; xk6-sql has no Cassandra or MongoDB extension under a recognised org; and the
sub-module licences are reportedly a mix of Apache-2.0 and AGPL-3.0 (unverified) which
is exactly the kind of thing that stalls a project at legal review.

### A thin first-party driver over official client libraries

Against it: we write the load generator, including the CO-correct scheduler, the
histogram handling, and the concurrency model — all things that are easy to get subtly
wrong. For it: the workload spec already describes multi-step transactional patterns
that neither of the above expresses natively; we would be generating their config from
our spec regardless; and every engine already has a first-class, maintained client
library, which is a far better integration surface than a bespoke plugin API.

### Decision: a first-party driver, with two hard constraints borrowed rather than invented

Build the driver, but **do not build the two things that are genuinely hard**:

1. **Latency recording → HdrHistogram** (dual CC0-1.0 / BSD-2-Clause, ports in every
   language). It provides `recordValueWithExpectedInterval` and
   `copyCorrectedForCoordinatedOmission` — the CO correction, already correct, already
   reviewed. Serialise the histogram into the bundle so third parties recompute
   percentiles rather than trusting our summary.
2. **The open-loop scheduler → the pgbench model**, which was verified working in
   Phase 3 (E5.1): maintain a schedule of intended start times, measure from the
   *intended* start, and report schedule lag separately. pgbench's implementation is
   the reference and its output (`rate limit schedule lag: avg … max …`) is the
   behaviour to reproduce.

Keep **pgbench, valkey-benchmark and ClickBench-style SQL runners as cross-checks**,
not as the primary path: if our driver reports numbers that disagree with pgbench on
Postgres by more than the noise floor, our driver is wrong. This calibration harness is
cheap and catches the category of bug that otherwise silently invalidates everything.

**Rejected outright:** YCSB (no seeding at all — `ThreadLocalRandom`, so datasets are
not reproducible; last release 2019), TSBS (no p95/p99 whatsoever; dormant since 2021),
sysbench (MySQL/Postgres only; last release 2020), wrk2 (dormant since 2019).

---

## 3.2 The workload specification: what the schema must survive

The schema is in `workload-spec.example.yaml`. The design decisions worth defending:

**Requirements are separated from measurements, and evaluated first.** `requirements:`
is a gate; nothing in it is benchmarked. This is what stops the system from producing a
fast, wrong answer — ClickHouse will post excellent numbers on UC-1's read patterns and
is disqualified before it starts because it cannot do the transactional stock
decrement. Encoding that as a *gate* rather than a low score is the difference between
a decision tool and a leaderboard.

**Durability is a spec-level requirement, not an engine default.** Phase 3 measured
Postgres writing 6.44 GB and Valkey writing 0 B for comparable work (E5.4). Engine
defaults span three orders of magnitude of fsync frequency. If durability is not
normalised from the spec, the harness is comparing incomparable things and will
reliably recommend whichever engine has the weakest defaults.

**Skew is expressed as `(hot_fraction, hot_traffic)`, not a Zipf θ.** Both are
expressible; only one can be validated against production telemetry by someone who is
not a statistician. "20% of SKUs get 80% of the traffic" is a sentence a product
manager can confirm or correct. "θ = 0.99" is not. Verified in exp06 to produce 80.1%
against an 80% target.

**Composite candidates are first-class.** UC-3's realistic answer is "Postgres as
source of truth + OpenSearch as index", and a harness that can only evaluate single
engines will systematically under-recommend the correct architecture. The consequence
is `resources.composite_split`: a composite candidate must **share** the resource
envelope, split explicitly. Otherwise "Postgres + Valkey" quietly gets twice the
hardware and wins on an artefact.

**Two mandatory prose fields.** `business_case` and `fidelity_gap`. The second is the
honesty field — what the simulation does *not* model. An unfilled `fidelity_gap` is a
report defect and blocks report emission. This is the cheapest possible defence against
the failure mode where a 20-minute container run is quoted in a board deck as though it
were production evidence.

**Identity by canonical hash.** The spec's identity is the SHA-256 of its
RFC 8785-canonicalised, defaults-materialised JSON form. Verified in exp06: reordering
keys preserves the hash; changing `target_rps` from 800 to 900 changes it. Note a
portability trap found while validating: YAML 1.1 parsers accept `50_000` as an integer
while strict YAML 1.2 parsers read it as a string. The example spec was rewritten to
use plain numerals; the loader must reject underscore numerals rather than depend on
parser behaviour.

---

## 3.3 The adapter contract

Six phases plus a gate — `gate → provision → init → load → run → collect → teardown` —
sketched in `adapter-contract.py` with PostgreSQL worked through. Each phase returns an
`Evidence` record carrying verbatim commands, engine **readback**, artifact hashes, and
a duration. The design principle is *verification, not assertion*: an adapter never
reports what it asked for, only what the engine confirmed.

Phase 3 measurements that are encoded directly into the contract:

| Contract rule | Evidence |
|---|---|
| Pin the **index** digest; record the resolved **platform** digest | E1.1 — they differ per architecture |
| Set affinity **and** quota **and** explicit engine knobs | E2, E8 — three CPU-detection APIs disagree in one container |
| Read every knob back from the running engine | E5.6 (Valkey reports a fictitious `redis_version:7.2.4`), E5.7 (`pg_settings` unit handling) |
| Named volumes only; assert the backing filesystem | E4.1, E4.2, E4.4 |
| Health-gate on TCP, not the Unix socket | E3.1 |
| Time each lifecycle phase separately | E3.2 — `compose up --wait` bundles pull+create+start+health |
| Settle the engine after load, and record how long it took | LSM compaction debt / B-tree checkpointing |
| Flag untrustworthy metrics rather than reporting them | E4.3 — BlockIO reads 8.19 kB instead of 6.44 GB off block storage |
| "Cannot run here" is a result, not a crash | E7 — MongoDB 8 on kernel ≥ 6.19 |
| Teardown is idempotent and runs on SIGINT | E3.5 |

The gate is a pure function over a **per-version** capability table. Capabilities
change between majors, and so do licences — Redis changed licence twice in 14 months —
so the SPDX identifier is pinned per image digest and re-verified each run, with any
change treated as a loud failure.

---

## 3.4 Fair comparison: what parity actually requires

Parity is not one setting; it is a stack of them, and Phase 3 showed several of the
obvious approaches do not work.

**CPU.** `--cpus` (quota) *and* `--cpuset-cpus` (affinity, identical core count per
candidate, disjoint from the load generator's cores), *and* the engine's own thread
knob set explicitly. Avoid `--cpu-shares`: it is non-linear on cgroup v2 (512 shares
yields weight 59, not half of 1024's 100) and only binds under contention.

**Memory.** `--memory` with `--memory-swap` set equal to it — this is the only way to
actually disable swap, since `--memory-swappiness=0` is silently discarded on this
kernel. Then set each engine's cache size explicitly, because `/proc/meminfo` reports
the host's RAM regardless of the limit (E8).

**I/O.** Weighting is unavailable (no BFQ → `--blkio-weight` errors,
`--blkio-weight-device` is a silent no-op). Throttling via `--device-read-bps` /
`--device-write-bps` works. Named volumes are mandatory; bind mounts inflate the noise
floor ~14× and tmpfs steals the memory budget.

**The THP problem has no clean solution.** Transparent huge pages are host-global and
not namespaced — Docker's `--sysctl` allowlist covers only IPC and `net.*`, no `vm.*`.
Meanwhile MongoDB 8+ now asks for THP `always`, Redis asks for `never`, and ClickHouse
asks for `madvise`. **You cannot satisfy them simultaneously on one host.** The options
are (a) one setting per host run, documented as a confound; (b) separate host pools per
engine family; (c) fix one value for everything and record it. **(c) is the only
defensible choice for a comparison whose purpose is fairness** — per-engine host tuning
would bias the very comparison it is meant to make fair. The manifest must record the
THP state, and the report must name it as a known limitation with a stated direction of
bias where one is known.

**Warmup and steady state.** Warmup must exceed at least one checkpoint (B-tree) or
compaction (LSM) cycle, or the run measures a system that has not yet started paying
for its writes. Phase 3's Postgres runs wrote 6.44 GB against a 320 MB dataset — a ~20×
write amplification that a 15-second run only partially exposes. The spec defaults to
120 s warmup / 600 s measurement for this reason, against the 8–20 s used in the
exploratory runs.

**Repeats and reporting.** Minimum 3, default 5. Never average percentiles across runs
— percentiles are not linear and the mean of p99s is not a p99. Merge the raw
histograms and recompute, or report the distribution of per-run p99s explicitly. The
justification is empirical: Valkey's throughput varied **1.87×** across three
consecutive identical repeats (E5.3) while Postgres on a named volume held CV ≈ 1.3%.
The noise floor is per-cell and must be measured, never assumed.

**Validity gates that invalidate a run rather than reporting it.** Driver CPU > 70%
(the load generator became the bottleneck); error rate above a threshold; the backing
filesystem not being a named volume; any engine readback disagreeing with the requested
envelope. A harness that reports an invalid run as a number is worse than one that
reports nothing.

---

## 3.5 The audit trail, end to end

The reproducibility tiers from Phase 1 (R1 inputs / R2 same-hardware results / R3
cross-hardware ranking) map onto concrete mechanisms, all of which were prototyped in
exp06.

**R1 is fully achievable and is the hard requirement.** The dataset is generated from
`blake2b(seed | table | row_id | column)` rather than from a sequential PRNG stream.
This matters more than it first appears: NumPy's NEP 19 explicitly refuses
stream-compatibility guarantees across versions, and Faker's own documentation says
results are *"not guaranteed to be consistent across patch versions"*. Index-derived
generation sidesteps the whole problem — output depends only on coordinates, not on
call order. Verified: byte-identical SHA-256 across forward order, reverse order, 8-way
threading, and a fresh subprocess; a different seed produces a different hash. This is
the same pattern LDBC uses (`blockId` *is* the seed) precisely so that Spark partition
count cannot change the dataset.

**The bundle is content-addressed with a two-level hash.** Leaf hashes per artifact,
then a root over the sorted `name:hash` list. Verified in exp06 that altering a single
metric value changes the root. Signing is a separate, later concern: it raises the cost
of undetected tampering and gives non-repudiation among honest parties, but it does not
make a result trustworthy when the runner controls the machine. Claiming otherwise
would be the kind of security theatre this design should avoid.

**Environment capture is not bureaucracy.** E7 is the proof: the same spec produces a
different *candidate set* on macOS (MongoDB 8 fatally refuses kernel ≥ 6.19) than on
Linux CI. Without kernel version in the manifest, two bundles with identical spec hashes
and different results are inexplicable. The capture list is therefore: kernel, cgroup
version and delegated controllers, CPU model and count, frequency governor, THP state,
swappiness, storage driver, the `rotational` flag, NUMA topology, container runtime
version, shm size, every image index+platform digest, and every engine's
self-reported version taken from its *authoritative* field.

**What a third party actually does with the bundle.** Read the manifest → check out the
harness at the recorded commit → pull the recorded image digests → regenerate the
dataset from the seed and compare its hash → re-run → compare their result against the
recorded `repeatability` band rather than against a point estimate. That last step is
the one most designs get wrong: comparing a fresh run against a single stored number
guarantees a false mismatch, because the stored number was itself a draw from a
distribution.

**What cannot be reproduced, honestly.** Wall-clock scheduling, cloud-neighbour
behaviour, disk block-allocation state (Docker Desktop's sparse `Docker.raw` means a
fresh volume's first writes force APFS allocation, so cold and warm runs differ for
reasons unrelated to the database), and thermal state. These are named in the report
rather than papered over.

---

## 3.6 Runtime choice for the harness

| | Python 3.13 + uv | Go 1.25 | Node/TS |
|---|---|---|---|
| Spec validation & JSON Schema export | **Pydantic v2.13.5 — best in class** | struct tags, weaker errors | zod/TypeSpec, good |
| DB client coverage | psycopg, pymongo, valkey-py, clickhouse-connect, opensearch-py — all first-party | good, less uniform | uneven |
| Distribution | needs a runtime | **single static binary** | needs a runtime |
| Load-generation concurrency | GIL; needs multiprocessing for high rates | **goroutines, native** | event loop, single core |
| Ecosystem for stats/reporting | pandas, scipy, matplotlib | thin | thin |

**Decision: Python for the orchestrator, and keep the high-rate load generation out of
Python.** The orchestrator's work is I/O-bound (docker, config, collection) and
schema-heavy, where Pydantic's defaulting, validation errors and 2020-12 JSON Schema
export are a decisive advantage — it is the only option that does defaulting,
validation, schema export and structured errors in one library. The GIL objection is
real but applies to the load generator, not the orchestrator; the load path delegates
to per-engine native drivers (pgbench, valkey-benchmark, clickhouse-benchmark) or a
small Go binary for the generic case, both invoked as subprocesses and both reporting
HdrHistogram-format output. This also keeps the calibration cross-check (§3.1) honest,
since the native tools are the reference.

Toolchain pinning: **Nix flakes or devbox**, which are the only options producing one
lockfile with real content hashes covering macOS-arm64 + Linux-x86_64 + Linux-arm64 and
all the client binaries we need. Homebrew is disqualified — its docs state plainly that
a Brewfile lock file will never exist. `uv` pins the Python interpreter and PyPI
dependencies but cannot provision any database or benchmark binary, so it is a
component of the answer, not the answer.

---

## 3.7 Gaps, contradictions, and what cannot be automated

**Contradictions found in the sources.** Several widely-repeated facts are stale or
wrong, and the harness's own component inventory must record last-*commit* dates rather
than GitHub's `pushed_at`, which misled assessments of TSBS, wrk2 and sysbench.
Specific corrections: MongoDB reversed its THP advice at 8.0 (now *enable*);
Elasticsearch's `vm.max_map_count` guidance is 1048576, not the widely-quoted 262144;
YCSB's wiki contradicts its own source on both `measurement.interval` and default
percentiles; TPC-C's `C_LAST` NURand constant is 255, not 1023.

**What benchmark-driven selection genuinely cannot decide.**

- *Operational burden.* Upgrade pain, backup/restore ergonomics, failure-mode
  diagnosability, the quality of the error messages your on-call engineer reads at
  3 a.m. None of this appears in a 20-minute container run, and it frequently dominates
  the real cost of a datastore over five years.
- *Scale-out behaviour.* v1 is single-node by design. Engines differ enormously in how
  gracefully they shard and how much that costs, and single-node results give no signal
  about it — occasionally they give *inverted* signal, since some engines pay a
  single-node penalty for distribution machinery they only benefit from later.
- *Failure behaviour.* Partition tolerance, failover time, split-brain handling,
  recovery-time objectives. Different tool, different discipline.
- *The workload you have not thought of yet.* The spec captures today's access
  patterns. The most expensive datastore mistakes come from the query nobody
  anticipated. A benchmark cannot price optionality; a human weighing data-model
  expressiveness can.
- *Team and organisational fit.* Familiarity, hiring pool, existing operational
  tooling, the political cost of introducing a fourth datastore. Real, decisive, and
  not measurable here.
- *Cost.* v1 computes a parameterised model card, not a measurement. Real TCO requires
  a real deployment.

**The honest framing.** This system narrows a field and quantifies the part that is
quantifiable, with an auditable record of how. It does not make the decision. The
scoring model's weights are the seam where human judgement enters, and the design
choice that matters most is making that seam **visible** — weights are authored,
attributed, justified in prose, and sensitivity-tested, so a reader can see exactly
where opinion was applied and how much the answer depends on it.

**The failure mode to guard against above all others** is not a wrong number. It is a
right number, correctly measured, presented without its caveats, and quoted six months
later as though it settled a question it never addressed.
