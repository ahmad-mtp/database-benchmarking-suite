# Phase 1 — Scoping

**Date:** 2026-09-01
**Status:** Complete

The north star: *"Simulate workloads of a prototype greenfield project so the choice of
data storage can be automated given a business use case."*

Phase 1 exists to stop that sentence from being infinitely large. Below are the
boundaries, the fixed candidate set, the driving use cases, and — most importantly —
the operational definition of "auditable", because that word is doing a lot of load
bearing in the ask.

---

## 1. Restating the problem precisely

The system under design is a **decision harness**, not a benchmark suite. The
difference matters:

| A benchmark suite | This decision harness |
|---|---|
| Answers "how fast is X?" | Answers "which of {X, Y, Z} should we build on?" |
| Optimises for absolute peak numbers | Optimises for *fair, comparable, ordinal* numbers |
| Tuned per engine by an expert | Configured identically-in-intent across engines by a machine |
| Published as a marketing artifact | Consumed as evidence in an architecture decision record |
| Success = big number | Success = a defensible, reproducible, *revisable* recommendation |

This reframing drives several downstream choices. In particular it means we care far
more about **comparability and provenance** than about squeezing the last 10% of
throughput out of any single engine. A harness that reports Postgres at 60% of its
achievable peak is fine *provided it also reports MongoDB at ~60% of its peak* and
provided the handicap is documented. A harness that reports one engine at 95% and
another at 40% because of an unexamined driver difference is worthless, however
impressive the absolute numbers.

### The three-layer question

Any "which datastore?" question decomposes into three layers, and only one of them is
benchmarkable:

1. **Feasibility (boolean, not benchmarkable).** Can the engine express the required
   access patterns at all? Does it offer the required transactional guarantees? Does
   its licence permit our deployment model? This is a *gate*, evaluated by rules, and
   it must run **before** any benchmark. Benchmarking an engine that cannot satisfy a
   hard requirement is wasted compute and, worse, produces a seductive number that
   invites someone to relax the requirement.
2. **Performance & cost under our workload (measurable).** Throughput, latency
   distribution, storage amplification, resource envelope at target load. This is the
   part the harness measures.
3. **Fit & burden (judgement, elicitable but not measurable).** Operational
   complexity, team familiarity, hiring pool, ecosystem maturity, migration cost,
   licence risk, vendor concentration, upgrade pain. These enter as explicitly
   human-set weighted criteria and must be **visually and structurally separated**
   from measured criteria in the report so nobody mistakes an opinion for a
   measurement.

**Design consequence:** the pipeline is `GATE → MEASURE → WEIGH`, and the final report
must show all three, with the gate results first. A system that only does layer 2 will
confidently recommend the wrong database.

---

## 2. Candidate datastore set for the POC

Selection criteria for the Tier-1 set: (a) covers a *distinct* data-model family,
(b) OSI-approved or otherwise benchmark-publishable licence, (c) an official
single-node container image exists, (d) reaches steady state in minutes not hours,
(e) has a credible client driver in the harness language.

### Tier 1 — must be supported by end of POC (5 engines)

| Family | Engine | Licence (verified) | Why this one |
|---|---|---|---|
| Relational / OLTP | **PostgreSQL 18.x** | PostgreSQL Licence (BSD-ish, permissive) | The default answer that every alternative must beat. If the harness cannot handle Postgres it cannot handle anything. Also gives us the JSONB and full-text paths, so it competes in the document and search lanes too — which is exactly the comparison a greenfield team needs. |
| Document | **MongoDB 8.0 LTS** | SSPL v1 (source-available, *not* OSI) | The canonical document store; its licence is a live example of a **gate** criterion, not a benchmark criterion. Included deliberately because it forces the licence gate to be real. |
| KV / cache | **Valkey 9.x** | BSD-3-Clause | Chosen over Redis. Redis went RSALv2/SSPL at 7.4 (Mar 2024) and added AGPLv3 at 8.0 (May 2025), so it is now tri-licensed; Valkey is the Linux Foundation BSD-3 fork of Redis 7.2.4 and is wire-compatible. For a *greenfield* project the permissive licence and neutral governance are the safer default, and wire compatibility means the measurement transfers. Redis stays in Tier 2 so the licence-vs-performance trade can actually be shown. |
| Columnar / OLAP | **ClickHouse 26.x** | Apache-2.0 | The strongest permissively-licensed analytical engine; single-node ClickHouse is genuinely usable, which keeps the POC single-node. |
| Search | **OpenSearch 3.x** | Apache-2.0 | Chosen over Elasticsearch. Elasticsearch has been tri-licensed AGPLv3/ELv2/SSPL since Aug 2024 — AGPL is OSI-approved so it is *not* excluded, but OpenSearch under the Linux Foundation's OpenSearch Software Foundation is the cleaner Apache-2.0 baseline. Elasticsearch goes in Tier 2 for the same "show the trade" reason as Redis. |

### Tier 2 — adapter contract must not preclude them; implement if time permits

| Family | Engine | Note |
|---|---|---|
| Time-series | QuestDB (Apache-2.0), TimescaleDB (Apache-2.0 core + source-available TSL for advanced features), InfluxDB 3 Core (MIT/Apache-2.0) | Time-series is the single most likely *fourth* family a greenfield team needs. TimescaleDB is attractive because it is a Postgres extension — it lets the harness demonstrate "same engine, different configuration" as a candidate, which is an important and commonly-ignored option. |
| Embedded / analytical | DuckDB (MIT) | The "you may not need a server at all" candidate. Cheap to add, and a genuinely useful null hypothesis. |
| Wide-column | Apache Cassandra (Apache-2.0), ScyllaDB | Real value only appears multi-node, which is out of v1 scope. |
| Graph | Neo4j (GPLv3 Community), Memgraph | Deferred: graph workload specification is a substantially different modelling problem (traversal depth, supernode skew) and would double Phase-2 spec work. |
| KV (licence contrast) | Redis 8.8 (tri-licence AGPLv3 / RSALv2 / SSPLv1) | Included specifically to exercise the licence gate against a near-identical performer. |
| Search (licence contrast) | Elasticsearch 9.x (AGPLv3 / ELv2 / SSPL) | Same. |

### Explicitly out of the candidate set for v1

- **Commercial engines** (Oracle, SQL Server, Db2). Not merely inconvenient — their
  licences contain **DeWitt clauses** prohibiting publication of benchmark results
  without vendor approval. Including them turns a technical artifact into a legal
  review. See §5.
- **Cloud-managed services** (DynamoDB, Aurora, Spanner, Cosmos DB, BigQuery). These
  cannot be provisioned reproducibly from a container image, cost real money per run,
  introduce network distance as an uncontrolled variable, and their performance is a
  function of a service tier we would be choosing arbitrarily. They are the *most
  commercially relevant* omission and must be called out honestly in the limitations
  section, with a sketch of how a v2 could reach them.

---

## 3. Driving business use cases

Three cases, chosen so that they *disagree with each other* about the right answer.
A harness validated only on cases that all point at Postgres has proven nothing.

### UC-1 — "Orders & inventory for a mid-market commerce platform" *(primary worked example)*

A greenfield replacement for a spreadsheet-and-Shopify setup. ~2,000 SKUs growing to
~50,000; 40k orders/month growing 8%/month; order placement must decrement stock
atomically and must never oversell; customers see order history; ops runs
low-stock and revenue-by-day reports on the same data.

*Why this case:* it has a genuine **hard transactional constraint** (no overselling)
that is a feasibility gate, not a performance question, and a mixed OLTP+light-OLAP
access pattern that tempts people toward polyglot persistence prematurely. Expected
outcome is that Postgres wins, but the harness must *demonstrate* that rather than
assume it, and must quantify how much headroom is left.

### UC-2 — "Fleet telemetry ingest and dashboards"

50,000 IoT sensors emitting one reading every 10s (~5,000 writes/s sustained,
peaking 3x during firmware rollouts). Append-only. Queries are almost entirely
"last 24h for device X" and "hourly rollup across a fleet segment for the last 30
days". 90-day retention then drop. No updates, no transactions.

*Why this case:* the constraint set is nearly the inverse of UC-1 — write throughput
and range-scan efficiency dominate, transactional guarantees are irrelevant, and
**storage amplification and compaction behaviour** become first-class metrics. This is
the case where run duration matters most (see §6) and where the naive "Postgres is
fine" answer is genuinely contestable.

### UC-3 — "Marketplace catalogue with faceted search"

500k listings, heterogeneous attributes per category (a sofa and a laptop share
almost no fields). 95% read. Queries are free-text + 3-6 facet filters + sort, with
a p95 budget of 200ms end-to-end. Listings change a few thousand times a day.
Attributes are added by category managers without a schema migration.

*Why this case:* it is the case where **data-model expressiveness** and performance
pull in different directions, and where three plausible architectures exist
(Postgres+JSONB+GIN, MongoDB, OpenSearch, or Postgres-as-source-of-truth plus
OpenSearch-as-index). It forces the harness to represent *composite* candidates —
"two engines together" — which is the design detail most single-engine benchmark tools
get wrong.

**Scope decision:** UC-1 is worked end-to-end and appears fully in `findings.md`.
UC-2 and UC-3 are specified well enough to prove the schema generalises, and UC-3's
composite-candidate requirement is treated as a *design constraint on the spec*, with
implementation deferred to a later milestone.

---

## 4. Scope boundaries

| Dimension | In scope (v1) | Out of scope (v1) | Rationale |
|---|---|---|---|
| Topology | Single-node containers | Clusters, replication, sharding, failover | Multi-node multiplies provisioning complexity and adds network variance that swamps engine differences at POC scale. Ordinal single-node results are still informative; the harness must simply *say* it is not measuring scale-out. |
| Hosting | Local containers (dev machine + Linux CI) | Cloud-managed services, bare metal, k8s clusters | Reproducibility and cost. |
| Failure behaviour | Not measured | Chaos/partition/failover testing | Genuinely important for selection; genuinely a different tool. Flagged as a limitation. |
| Data volume | Up to ~10-50 GB per engine per run | TB-scale | Must fit on a dev laptop and a CI runner. Above this, the harness should *refuse and say so* rather than silently thrash. |
| Cost | Parameterised **cost model card** per engine, computed not measured, with sensitivity analysis | Real cloud billing integration, licence negotiation | A measured cost number would require cloud deployment, which is out of scope. The model card makes assumptions explicit and challengeable. |
| Security/compliance | Recorded as gate criteria (encryption-at-rest availability, auth model, audit-log support) | Penetration testing, certification review | Boolean capability checks are cheap and honest; anything deeper is consultancy. |
| Migration | Not measured | Schema evolution cost, ETL from legacy | Flagged as a human-judgement criterion. |
| Operability | Elicited as weighted qualitative criteria with a documented rubric | Measured MTTR, on-call load | Cannot be simulated in a 20-minute container run. Saying so plainly is part of the deliverable. |

### Hard non-goals

- **The harness never picks the winner unilaterally.** It produces a ranked,
  explained, sensitivity-tested recommendation with the weights it used visible and
  editable. A human signs the ADR. Anything else launders judgement as computation.
- **The harness does not tune engines to their maximum.** It applies a documented,
  reviewable, *equal-effort* configuration policy (see Phase 3). Per-engine expert
  tuning is an explicit, out-of-scope follow-up whose absence must be stated in every
  report.
- **The harness does not produce publishable competitive benchmarks.** Internal
  decision support only. This distinction is legally load-bearing (§5).

---

## 5. Legal & licensing constraints (a real gate, not boilerplate)

Two constraints shape the candidate set and must be encoded as automated gates:

1. **DeWitt clauses.** Several commercial DBMS licences forbid publishing benchmark
   results without vendor consent. This is why the v1 candidate set is OSS-only and
   why the report is scoped as internal decision support. If a commercial engine is
   ever added, the harness must refuse to emit a comparative report without a recorded
   approval token.
2. **Deployment-model licence risk.** SSPL (MongoDB), RSALv2, and Elastic License v2
   are source-available, not OSI-approved. AGPLv3 (Redis 8+, Elasticsearch option) is
   OSI-approved but has network-copyleft implications depending on how the product is
   distributed. For a greenfield product these are *architectural* facts with real
   consequences, so the workload spec carries a `licence_policy` field (e.g.
   `osi_approved_only`, `permissive_only`, `any`) and the gate filters candidates
   against it **before** any container is started.

**Design consequence:** the candidate registry stores an SPDX identifier per engine
version, and the gate is a pure function of `(spdx_id, licence_policy)`. Licences
change — Redis changed twice in 14 months — so the SPDX id is pinned per *image
digest*, re-verified each run, and a change is a loud failure, not a silent one.

---

## 6. What "auditable" must mean here

This is the definition the rest of the design is built on. "Reproducible" applied
naively to a performance benchmark is incoherent — you cannot get bit-identical
latency numbers from any two runs on any two machines, ever. So the bar is split.

### Reproducibility tiers

| Tier | Claim | Achievable? | Enforcement |
|---|---|---|---|
| **R1 — Input reproducibility** | From the bundle alone, a third party can reconstruct **byte-identical inputs**: the same image digests, the same generated dataset, the same workload spec, the same harness commit, the same engine configuration. | **Yes, fully.** This is a hard requirement. | Every input is content-addressed. The bundle records a SHA-256 for each. A verifier re-derives the dataset from the seed and compares hashes. A mismatch fails the audit. |
| **R2 — Result reproducibility (same hardware class)** | Re-running the bundle on equivalent hardware yields metrics within a **declared tolerance band** — e.g. throughput medians whose bootstrap CIs overlap, p99 within a stated ratio. | **Yes, with caveats.** Requires the tolerance band to be measured, not assumed — the harness must characterise its own noise floor. | The bundle stores a `repeatability` block: the observed run-to-run variance from the N repeats that produced the result. A verifier compares against that band, not against a point estimate. |
| **R3 — Cross-hardware reproducibility** | The same run on *different* hardware yields the same **ranking**, not the same numbers. | **Only sometimes, and it must be tested, not assumed.** Ordinal stability can break when engines have different sensitivity to core count, page cache size, or storage latency. | The harness reports rank stability across whatever environments it has seen, and states plainly when it has only one. Absolute numbers are never claimed to transfer. |

**The headline rule: byte-identical inputs, statistically-equivalent outputs,
rank-stable conclusions — and the bundle must state which of the three it is
claiming.** A report that claims more than R1 without the evidence to back it is the
primary failure mode of this whole category of tool.

### Minimum contents of an audit bundle

A bundle is worthless if it merely *asserts*. It must contain enough to *re-derive*:

- The workload spec, canonicalised and hashed (the hash is the spec's identity).
- The resolved run matrix — every (engine, config, scenario, repeat) cell, enumerated.
- Full environment capture: host CPU model/count/frequency-governor, RAM, kernel,
  container runtime version, storage backing, and — critically — whether it was a
  virtualised filesystem (macOS/Windows Docker) or native Linux, because that single
  fact can invalidate cross-environment comparison.
- Image identity **by digest**, not tag. Tags are mutable; `postgres:18` today is not
  `postgres:18` next month.
- Every engine configuration file *as actually applied*, read back from the running
  container rather than as intended.
- The RNG seed and the generator version, sufficient to regenerate the dataset
  byte-for-byte, plus the dataset's own hash so regeneration is verifiable.
- Raw metric series, not just summaries — including the full latency histogram, so a
  third party can recompute percentiles rather than trusting ours.
- The scoring model: weights, normalisation method, and the arithmetic, such that the
  final ranking can be recomputed from the raw metrics without re-running anything.
- A manifest hash covering all of the above, and a record of *who ran it, when, and on
  what*.

### Explicit audit anti-goals

- We are **not** trying to defeat a motivated forger. Signing (§ later phases) raises
  the cost of undetected tampering and gives non-repudiation for honest actors; it
  does not make results trustworthy in an adversarial setting where the runner
  controls the machine.
- We are **not** claiming the numbers generalise to production. The bundle records the
  gap between the simulated workload and the real one as an explicit, human-written
  field. An unfilled field is a report defect.

---

## 7. Success criteria for the POC (restated as acceptance tests)

The POC is done when all of these are true:

1. `harness run spec.yaml` takes a workload spec and produces a scored, ranked
   recommendation across ≥3 Tier-1 engines, unattended, on both macOS/arm64 and Linux
   CI/amd64.
2. Every engine is provisioned from a **digest-pinned** image, health-gated before
   load, and torn down idempotently — including after a crash or SIGINT.
3. Two runs of the same spec on the same machine produce rankings that agree, and the
   harness reports its own measured run-to-run variance rather than asserting
   stability.
4. A third party, given only the audit bundle and the repo, can reconstruct
   byte-identical inputs (verified by hash comparison) and re-execute.
5. The scoring output can be recomputed from the raw metrics by an independent script,
   and changing a weight visibly changes the ranking (sensitivity analysis works).
6. The licence gate demonstrably excludes a candidate when `licence_policy` is
   tightened, *before* any container starts.
7. Every recommended tool in the component inventory has a pinned version and a
   verified licence.
8. The report states, in the report itself, what it did not measure.

---

## 8. Open questions carried into Phase 2

- Which benchmark driver can span relational + document + KV + columnar + search
  without us writing five unrelated load generators? Is there one, or is a thin
  first-party driver over official client libraries actually the honest answer?
- Is Docker Desktop on macOS's virtualised filesystem so distorting that dev-machine
  numbers are meaningless, and if so, is the dev machine only for *pipeline*
  validation with Linux CI as the only source of *results*?
- Does anything in 2026 already do this? If a mature product exists, the POC should
  become an evaluation, not a build. (This must be checked seriously, not waved at.)
- Can the workload spec express UC-3's composite candidate (Postgres + OpenSearch as
  one candidate) without becoming a general-purpose architecture DSL?
- What is the smallest honest statistical treatment — how many repeats, which
  interval estimator — given that each repeat costs real minutes?
