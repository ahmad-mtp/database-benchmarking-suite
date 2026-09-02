# Research Plan: Automated Datastore Selection via Workload Simulation

**Created:** 2026-09-01
**Status:** Complete
**Completed:** 2026-09-01

## Research Questions

1. How do you translate a plain-language business use case into a machine-readable
   workload specification (entities, access patterns, read/write mix, cardinality,
   consistency needs, latency SLOs, growth curve)?
2. What tooling exists for generating synthetic datasets and driving benchmark
   workloads against heterogeneous datastores (YCSB, TSBS, nosqlbench, pgbench,
   sysbench, Benchbase, Locust, k6, dbgen/TPC-*, Faker/Synthea/SDV)?
3. How are candidate datastores provisioned reproducibly — containerisation
   (Docker/Compose, Testcontainers, Podman), image pinning, resource limits, and
   fair-comparison methodology (warmup, cgroup parity, noisy-neighbour control)?
4. How is initialization automated end to end — binary/executable acquisition,
   schema/DDL generation per engine, index strategy, seed data load, health gating,
   idempotent teardown?
5. What runtimes and orchestration layers fit the harness (Python vs Go vs Node,
   task runner, config schema, parallel vs serialised runs, CI execution)?
6. How do you produce an auditable report — immutable run manifests, environment
   capture, metric provenance, deterministic seeds, signed/hashed artifacts,
   reproducibility from the manifest alone?
7. What scoring/decision model turns raw benchmark metrics + qualitative fit into a
   defensible datastore recommendation (weighted scoring, Pareto fronts, TCO,
   operational-burden factors), and how are its assumptions made explicit?

## Success Criteria

- A complete, buildable POC plan: architecture, component inventory, tech choices
  with rationale, milestone breakdown, and acceptance tests.
- Every recommended tool is verified to exist, with current version and license.
- Auditability design is concrete: named artifacts, schemas, hashing strategy.
- A reviewer could hand the plan to an engineer and they could start on day one.

---

## Phase 1: Scoping

**Goal:** Define boundaries, clarify questions, identify what's in/out of scope.

**Tasks:**
- [x] Fix the candidate datastore set for the POC (relational, document, KV/cache,
      columnar/OLAP, search, graph, time-series — pick a defensible subset)
- [x] Define 2-3 representative business use cases to drive the POC end to end
- [x] Decide scope boundaries: single-node containers vs clusters; cloud-managed
      services in or out; cost modelling depth
- [x] Define what "auditable" must mean for this system (reproducibility bar)
- [x] Create phase-1/ directory with notes

**Output:** `phase-1/scope.md`

---

## Phase 2: Discovery

**Goal:** Broad search and source gathering across all seven question areas.

**Tasks:**
- [x] Survey benchmark harnesses: YCSB, nosqlbench, TSBS, Benchbase, pgbench,
      sysbench, HammerDB, k6, Locust — maturity, coverage, license, activity
- [x] Survey synthetic data generation: SDV, Faker, Mimesis, dbgen/TPC toolkits
- [x] Survey container orchestration for benchmarks: Testcontainers, Docker
      Compose, Podman, Dev Containers, Kind/K8s; image provenance and pinning
- [x] Survey binary/runtime provisioning: mise/asdf, uv, Nix, devbox, Homebrew,
      official install scripts — determinism and lockfile support
- [x] Survey observability & metric capture: OpenTelemetry, Prometheus,
      cAdvisor, container stats, per-engine internal metrics
- [x] Survey audit/provenance patterns: SLSA, in-toto, sigstore/cosign, SBOM
      (Syft/CycloneDX), content-addressed artifacts, run manifests, MLflow-style
      experiment tracking
- [x] Survey prior art on automated datastore/technology selection (academic +
      industry decision frameworks, DB selection matrices)
- [x] Catalog findings with brief summaries; log every source with URL
- [x] Create phase-2/ directory

**Output:** `phase-2/discovery.md`

---

## Phase 3: Deep Analysis

**Goal:** Focused investigation of the most promising options; validate by doing.

**Tasks:**
- [x] Deep-dive the top 2-3 benchmark harnesses: workload definition format,
      extensibility to new engines, output format, known methodology pitfalls
- [x] Design the workload specification schema (JSON/YAML) that sits between the
      business case and the harness — draft it concretely
- [x] Design the per-engine adapter interface: provision → init → load → run →
      collect → teardown
- [x] Prototype-verify the mechanics locally where cheap: pull one or two images,
      confirm container startup + health gating + a trivial load actually works,
      and record exact commands and versions in phase-3/
- [x] Analyse fair-comparison methodology: resource parity, warmup, run count,
      percentile reporting, statistical significance, coordinated omission
- [x] Analyse the audit trail end to end: what is captured, where it is stored,
      how a third party re-runs it and gets the same answer
- [x] Compare candidate runtimes/languages for the harness with trade-offs
- [x] Note gaps, contradictions, and things that cannot be automated

**Output:** `phase-3/analysis.md`, plus any working scripts under `phase-3/`

---

## Phase 4: Synthesis

**Goal:** Cross-reference and form the recommended architecture.

**Tasks:**
- [x] Choose the stack: runtime, harness, containerisation, provisioning,
      metrics, reporting — each with explicit rationale and rejected alternatives
- [x] Draw the end-to-end architecture (business case → spec → matrix of runs →
      metrics → scored recommendation → audit bundle)
- [x] Define the scoring model, its inputs, weights, and how weights are justified
- [x] Identify risks, failure modes, and the honest limits of benchmark-driven
      selection (what a benchmark cannot tell you)
- [x] Draft the structure of the final POC plan

**Output:** `phase-4/synthesis.md`

---

## Phase 5: Output

**Goal:** Produce the deliverable POC plan.

**Tasks:**
- [x] Write `findings.md` — the complete POC plan: architecture, component
      inventory with pinned versions, repo layout, data/config schemas, the
      per-engine adapter contract, the audit bundle spec, the scoring model,
      milestones (M0..Mn) with acceptance criteria, effort estimate, and risks
- [x] Include concrete examples: one filled-in workload spec, one sample run
      manifest, one sample report skeleton
- [x] Write `sources.md` — all references with URLs and one-line relevance notes
- [x] Archive scripts/artifacts produced along the way in the topic directory
- [x] Update plan.md status to Complete

**Output:** `findings.md`, `sources.md`


---

## Completion record

**Completed 2026-09-01.** All five phases executed.

| Phase | Output | Notes |
|---|---|---|
| 1 Scoping | `phase-1/scope.md` | Tier-1 engine set fixed (Postgres, MongoDB, Valkey, ClickHouse, OpenSearch); 3 business use cases; R1/R2/R3 reproducibility tiers defined |
| 2 Discovery | `phase-2/discovery.md` | ~40 tools surveyed with verified versions and licences; licence landmines found (SDV BUSL-1.1, ClickBench CC BY-NC-SA, TPC EULA, k6 AGPL) |
| 3 Deep Analysis | `phase-3/analysis.md`, `phase-3/experiments.md`, 11 experiments + logs, `workload-spec.example.yaml`, `adapter-contract.py` | Everything mechanical verified by execution on Docker Desktop 4.88.1 / Apple M5 |
| 4 Synthesis | `phase-4/synthesis.md` | Architecture, stack with rejected alternatives, risk register |
| 5 Output | `findings.md`, `sources.md` | Complete POC plan, 12 sections, M0–M7 |

### Findings that changed the design mid-research

1. **Min-max normalisation reversed the winner** when an irrelevant candidate was added
   (exp10). Replaced with reference-anchored normalisation.
2. **MongoDB 8 cannot start on Docker Desktop for Mac** (kernel ≥ 6.19, SERVER-121912,
   exp07). "Cannot run here" became a first-class adapter outcome.
3. **No Docker flag combination makes CPU/memory detection consistent** (exp08 — five
   APIs, three answers). Explicit knobs plus readback verification became mandatory.
4. **Bind-mounted storage inflates the noise floor 14×** and blinds `docker stats`
   BlockIO (exp05). Named volumes became a hard requirement, not a recommendation.
