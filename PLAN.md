# Build the datastore decision harness

## Context

`/Users/mac/Desktop/code/mtp/database-benchmarking-suite` contains research only — a
completed five-phase study whose deliverable, `datastore-selection-automation/findings.md`,
specifies a system nobody has built. No `pyproject.toml`, no tests, no harness code.
`phase-3/` holds verification evidence, not an implementation.

Goal: pick a database engine, a server configuration, and a Python workload, and watch
the pair being pushed to its limits live. The driving question is how a **database and
an app server scale together** — connection counts, throughput cliffs, join behaviour
under sustained load — on a single Docker Compose project.

Two things make this more than re-implementing the research:

1. **Live observability is the primary feature, and the research is silent on it.**
   `plan.md` marks the observability survey complete but `discovery.md` has zero content
   on Prometheus, Grafana, OpenTelemetry or cAdvisor. Every phase-3 capture is
   `docker stats --no-stream` — one sample, after the run. No sampling cadence exists
   anywhere. `metrics.ndjson` is named as a bundle artifact and hashed, but **its record
   schema is never defined**. Defining it is a primary deliverable.
2. **There is no app tier anywhere in the research.** The driver talks straight to the
   DB. The app tier, its metrics, its saturation gate and its per-engine data layers are
   new — as is the connection-ramp axis and the connection-scaling instrumentation.

Outcome: mechanisms and scaling curves learned here, transferable to real hardware by
changing a resource envelope rather than rewriting anything.

---

## Locked decisions

| Decision | Choice |
|---|---|
| Scope | Full decision harness, findings.md M0–M7: gate → measure → weigh → audit bundle |
| Live view | Prometheus + Grafana in the same Compose project |
| Latency truth | **HdrHistogram `.hlog` is authoritative.** Prometheus carries derived series for watching only — a scrape bucket must never become a reported percentile |
| Topology | One Compose project, engine + standardised app tier, programmatically selected |
| App tier | Python (gunicorn/uvicorn + real pool; asyncpg for Postgres) |
| Driver paths | Both, switchable. A: driver→DB. B: driver→app→DB. The delta is the tier's cost |
| Ramp axes | Both. Rate ramp (max sustainable, knee, collapse) and **connection ramp** (hold rate, staircase connections, find the cliff) |
| Workload | `@op`-decorated ops as primary path, plus raw-script escape hatch stamped `latency_validity=closed_loop_uncorrected` and refused by the scorer |
| Engines | All five, breadth first: Postgres 18, Valkey 9, ClickHouse 26, OpenSearch 3, MongoDB 8 |
| Build order | Postgres + app tier end to end, then widen, then gate/scoring/bundle |
| Target machine | **This Mac only.** No Linux runner, now or later |
| Licensing | **No GPL in the toolchain.** Rules out `memtier_benchmark` |

### Hardware slices — locked

```
cpu 0-1   host + docker daemon + app tier + observability stack
cpu 2-5   ENGINE under test              mem 3 GiB
cpu 6-9   load driver                    mem 1 GiB
          app tier                       mem 1 GiB
          prometheus/grafana/exporters   mem 1.5 GiB
                                          = 6.5 of 7.75 GiB
```

**Measured 2026-09-02 — the slices do not isolate.** `--cpuset-cpus` partitions the
Docker VM's ten *guest* vCPUs, and those are not host cores: Virtualization.framework
schedules all ten guest vCPU threads across the host's 4 performance + 6 efficiency
cores, so load on one slice takes host cores from another. Contention swept on one
slice while measuring the other, 6 randomised blocks with a bootstrap CI over blocks
(`dsel env --interference`):

| Load on the neighbouring slice | Engine 2-5 retains | Driver 6-9 retains |
|---|---|---|
| 1 worker  | 0.93 [0.85, 0.97] | 0.95 [0.88, 0.99] |
| 2 workers | 0.79 [0.77, 0.87] | 0.87 [0.81, 0.98] |
| 4 workers | 0.72 [0.67, 0.75] | 0.80 [0.69, 0.86] |

Replicated; the first sweep read 0.69 and 0.75 at four workers, so the honest figure is
a **20–30% loss** under full load on the neighbouring slice. It is symmetric within
error — an interleaved A/B put the two slices at 1.003, so neither is intrinsically
faster. Runs stamp `cpuset_isolation_ineffective`.

The slice table therefore buys *scheduling separation* inside the guest, not performance
isolation. Every engine measurement is depressed by whatever the driver is doing at the
time, and the `driver_cpu_pct` and `app_tier_cpu_pct` denominators are each a function
of the other tier's load. This is the same objection as risk 5, one tier further out,
and it is not fixable by rearranging the slices — only by not running the driver on the
same machine, which this build has ruled out.

**Thermal drift is first-order, measured 2026-09-02.** The first capacity measurement on
a cool machine read 51.1M ops/s and settled to ~41M within seconds: a 20% swing from
temperature alone, larger than several effects the harness is meant to resolve.
findings.md §8.8 lists thermal state among the things that cannot be reproduced; on this
machine it is not a footnote, and any two cells compared across minutes must assume it.

Every run stamps `profile=local`, `envelope_deviation=true`, `reportable=false`. There is
no second profile to build — but the envelope stays a spec value rather than a constant,
so larger hardware would be a YAML change if that ever becomes true.

### What this build will and will not claim

This machine is the only target, which sets the honest scope. **The harness produces
mechanisms, relationships and scaling curves. It will refuse to sign a capacity claim**
("this serves 40,000 orders/sec"), because such a number would not be true anywhere else.
That refusal is enforced by `dsel verify`, not left to discipline, and it goes in the
README at S0 rather than being discovered at G3.

Three consequences to accept up front:

- **MongoDB 8 never runs here** (kernel ≥6.19, SERVER-121912). It stays in the candidate
  set precisely because it exercises the environment gate — "cannot run here" as a
  first-class result is a feature being built, not a gap.
- **Valkey's latency ceiling is permanently unmeasurable.** With GPL ruled out, the
  Python driver saturates first (~80–160k ops/s across four workers, against exp02's
  measured 150–290k). Valkey reports `INCONCLUSIVE_DRIVER_BOUND` with the driver-side
  ceiling recorded. `valkey-benchmark` (BSD-3, already in the image) bounds the ramp and
  proves driver-boundness, but is closed-loop so its latency is never claimed.
- **The A/B delta between driver→DB and driver→app→DB stays `ab_delta_valid=false`**
  locally, because the app tier shares cores 0-1 with the observability stack.

So the ranking machinery will, on this hardware, rank a candidate set of roughly three
engines and decline to emit a signed comparative report. That is the machinery working
correctly. **The machinery is the deliverable.**

---

## Design decisions taken during planning

- **D1 — The driver runs in a container**, never on the macOS host. Host→VM traffic
  crosses VirtioFS-grade overhead and pollutes exactly the latency being measured.
  Container-to-container over a Compose bridge stays inside the VM.
- **D2 — Render `compose.rendered.yaml` from Jinja2**, don't use Compose profiles. The
  envelope, cpusets and digests come from the spec; a rendered file is hashable into the
  bundle and a profile is not.
- **D3 — Prometheus gets data by tailing `metrics.ndjson`** through a custom
  `prometheus_client` Collector. One write path, one source of truth; the exporter is a
  consumer like any other. No pushgateway, no remote-write.
- **D5 — Calibration tools (`pgbench`, `valkey-benchmark`) run inside the engine image**
  on cpuset 6-9, not from the host. They are already in the pinned images.
- **D6 — The driver is multi-process from the start.** A saturated single Python process
  reads ~25% of a 4-core quota, so the 70% gate silently never fires. Per-worker CPU gate
  plus an implementation-independent schedule-lag gate.
- **D7 — No VirtioFS mount inside a measurement window**, ever.
- **D9 — Build the TUI first, Grafana second.** Grafana is AGPL-3.0 and could stall at
  legal review; the live-observability requirement must not depend on it.

### New gate

**App-tier CPU > 70% ⇒ INVALID for database-level claims**, mirroring the driver gate one
tier up. Without it, app-tier limits saturate first and every engine looks identical.

---

## Milestones

**Rule for every phase below: the `Accept:` line is the definition of done.** It must be
decidable by a command — an exit code, a diff, a count, a round-trip comparison, or a
deliberately-tripped failure path. A phase is not finished, and nothing from it is
committed, until its criterion runs and passes. If it fails, that gets reported rather
than worked around.

### Slice 1 — Postgres + app tier, end to end

**S0 Skeleton.** uv project on Python 3.13, `dsel` CLI, run directory layout. README
states plainly what this build can and cannot claim on this hardware.
*Accept:* `uv sync --frozen` succeeds from a clean checkout and `uv run dsel --version`
exits 0; a CI test greps the README for the no-capacity-claims paragraph and fails if it
is absent.

**S1 Environment capture + vCPU speed probe.** *De-risk early.* The M5 is 4 performance +
6 efficiency cores, so `cpuset 2-5` and `6-9` may straddle core classes — findings.md
chose those sets without examining it. Probe: 200 ms of fixed integer work per vCPU,
recorded as `vcpu_relative_speed[0..9]`. If engine-set and driver-set aggregate speed
differ >10%, append `heterogeneous_cores` to `envelope_deviation_reasons`.
*Accept:* the manifest carries a per-vCPU speed vector and the deviation flag is correct.
*Done 2026-09-02.* The premise does not hold, and cannot be tested from inside the VM:
the between-vCPU span was 7.1% against within-vCPU noise of 42.7%, a signal-to-noise of
0.17, because the hypervisor reschedules guest vCPUs across host cores continuously. So
`heterogeneous_cores` cannot fire on merit here; it is implemented and was demonstrated
firing under deliberate contention (12.1% > 10%). A run also stamps
`vcpu_speed_indistinguishable`, so a bare `false` is not misread as a positive finding
of uniformity. The capture also records two facts that bear on cross-bundle comparison:
the VM reports `rotational=1` for `vda`/`vdb` over APFS on NVMe, and THP is `[always]`
and is host-global, so not settable per container.

**S2 Budget assertion.** *De-risk early.* `compose/budget.py` computes cpuset and memory
totals before any container starts and refuses impossible combinations.
*Accept:* `profile=local` + app tier + deep observability is refused with the arithmetic
shown.

**S3–S5 Provisioning.** Digest resolution (index pinned, platform recorded, `unknown/unknown`
attestation entries skipped), resource envelope with `--cpuset-cpus` AND `--cpus` AND
`--memory`/`--memory-swap`, named-volume enforcement via `stat -f -c "%T"`, TCP health
gate, idempotent teardown on SIGINT/SIGTERM/crash.
*Accept:* every knob read back from the running engine matches the envelope; teardown
exits 0 twice.

**S6 `metrics.ndjson` schema.** *De-risk early — every emitter depends on it.* Pydantic
models per record kind → committed `schema/metrics.schema.json`, CI drift-checked.
Sharded writers with `seq`, deterministic merge by `(t_ms, w, seq)`.
Record kinds: `phase`, `latency_window`, `container`, `engine`, `backend`, `pool`, `app`,
`net`, `validity`.
Cadence: containers 1 Hz · engine light 1 Hz / heavy 5 s · per-backend sampling drops to
10 s above 256 connections and stamps `sampler.backpressure=true`.
*Accept:* 100 shuffled shard merges produce a byte-identical file.

**S7 Samplers.** Docker stats stream, cgroup readers (`cpu.stat`, `cpu.max`,
`cpuset.cpus.effective`, `memory.max|current|events`, `pids.current`), `docker events`
for `oom`/`die`/`health_status`.
*Accept:* against a live container, every sampler emits records that validate against
`schema/metrics.schema.json`, and cgroup readings equal the corresponding
`docker inspect .HostConfig` values exactly; killing the container with `--memory` exceeded
produces an `oom` record.

**S8a Live TUI.** `dsel watch` tails the event stream. **This alone satisfies the
live-observability requirement.**
*Accept:* `dsel watch --replay <run-id>` on a finished run reaches the same final screen
state as the live session did, and the warmup→measure boundary is visibly marked in both.

**S8b Prometheus + Grafana.** Tail-and-expose collector, provisioned dashboards
(`now`, `connections`, `joins`, `validity`). Series budget ≤500 active, ≤5000 per run;
`step`/`repeat` join from `dsbench_cell_info` rather than becoming labels.
*Accept:* `count({__name__=~"dsbench_.*"})` stays ≤500 for a full run; every panel in all
four dashboards resolves against a completed run with no "No data"; every latency panel
carries a visible "within-window estimate, not the reported figure" annotation.

**S10 Open-loop driver.** Poisson arrival, latency from scheduled start, HdrHistogram
per worker, multi-process supervisor, per-worker CPU gate.
*Accept, and verify here not at M7:* a `.hlog` written from Python is read by the Java
`HistogramLogProcessor` in a pinned JDK container with p50/p99/p99.9 matching within one
bucket. The entire "third party recomputes percentiles" claim rests on this.

**S11–S12 Rate ramp + pgbench calibration.** If the first-party driver disagrees with
pgbench beyond the noise floor, the driver is wrong.
*Accept:* against identical Postgres and workload, driver and `pgbench -R` agree on
achieved rate within 1% and on mean latency within the measured noise floor; the ramp
recovers a knee and collapse point from a synthetic target whose limits are known by
construction.

**S13 App tier + its ceiling.** *De-risk early.* FastAPI, per-worker asyncpg pool,
span timing (`t_app_recv`/`t_db_start`/`t_db_end`/`t_app_send`). Measure the `/noop`
ceiling before wiring PATH B, so app-tier saturation is a known number, not a surprise.
*Accept:* `/noop` ceiling is measured and written to the manifest; driving past it makes
the `app_tier_cpu_pct` gate fire and stamp `INVALID(app_tier_saturated)` — the gate is
demonstrated tripping, not merely implemented.

**S14 PATH B.** Driver→app→DB, scheduled against the measured app ceiling (capped at 60%
of it locally).
*Accept:* for the same workload, PATH B's `t_db_end − t_db_start` distribution overlaps
PATH A's latency distribution; `ab_delta_valid=false` is stamped on every local run.

**S15 Phenomena derivation.** `phenomena/*` reads `metrics.ndjson` and never touches
Docker or the engine; `live/sampler/*` writes records and never derives a phenomenon.
*Accept:* an independent script re-derives knee and collapse from `metrics.ndjson` alone.

**S16–S18 Connection ramp + the three phenomena.**
- (a) **Cliff** — hold rate, staircase connections; per-connection rate, aggregate
  turnover, wait-event shift, `pids.current` and `nofile` walls.
- (b) **Backend growth** — per-backend `VmRSS` against connection age, RSS slope with CI.
  Ground truth is `VmRSS`, which works on every version;
  `pg_log_backend_memory_contexts()` (PG14+) is the guaranteed fallback for the cache
  breakdown. Do not depend on `pg_get_process_memory_contexts()` — UNVERIFIED on PG18.
- (c) **Lifecycle** — refusal/reset attribution by mechanism: `max_connections`,
  `idle_in_transaction_session_timeout`, keepalive, pool reaper, TIME_WAIT and ephemeral
  ports from `/proc/net/netstat`+`sockstat`, listen-backlog overflow.
  Note `max_connections=32` provocation needs its own provision cycle — it is not a
  runtime knob.
*Accept:* (a) a ramp past the knee shows aggregate throughput falling while connection
count rises, with knee and collapse re-derivable from `metrics.ndjson` alone; (b) over a
≥1 h soak the per-backend RSS slope has a bootstrap CI excluding zero; (c) a
`max_connections=32` run attributes every refusal to a named mechanism with zero
`unknown` causes.

**S19 Joins.** Plausibility pre-flight (`EXPLAIN ANALYZE` assertions, run *before* the
cell starts), plan fingerprinting and flip detection, `work_mem` × nodes × connections
projection asserted against `memory.max`, temp spill from `pg_stat_database`.
`auto_explain` preloaded at provision but held at `log_min_duration=-1` during measure;
plans captured in a separate pass so the instrument does not confound the measurement.
*Accept:* a query that would be optimised away (the ClickHouse `numbers_mt` shape) is
rejected by plausibility pre-flight and the cell never starts; a `work_mem` × nodes ×
connections projection exceeding `memory.max` is refused before provisioning; a plan flip
induced by lowering `work_mem` is detected and recorded with both fingerprints.

### Widening — W1–W4
Valkey, ClickHouse, OpenSearch, MongoDB. Each needs a harness adapter **and** an app-tier
data-access layer. MongoDB 8 exercises `cannot run here` as a first-class outcome.
*Accept, per engine:* the contract test proves the adapter satisfies the Protocol; against
a live container the health gate passes, every knob reads back equal to the envelope, and
the authoritative version field is the correct one (`valkey_version`, not `redis_version`).
*Accept, W4 specifically:* MongoDB 8 returns a `cannot_run_here` **result** carrying the
SERVER-121912 reason — not an exception, not a crash — and the run continues with the
candidate excluded rather than aborting.

### Closing — G1–G3
Gate (capability tables per engine+version, SPDX per digest, environment feasibility),
scoring (reference-anchored, vetoes, sensitivity — `exp09-scoring.py` becomes the
regression fixture), audit bundle + third-party verify.
*Accept G1:* tightening `licence_policy` to `osi_approved_only` excludes MongoDB before
any container starts (assert zero `docker run` calls); ClickHouse fails UC-1's
`multi_document` transaction requirement.
*Accept G2:* the ranking recomputes from `results` + `scoring` with no re-run and matches
`exp09-scoring.py`'s fixture output; perturbing one weight visibly changes the ranking; a
repo-wide grep proves min-max normalisation exists nowhere in `src/`; an
`INCONCLUSIVE_DRIVER_BOUND` candidate is excluded from scoring rather than scored low.
*Accept G3:* `bundle_root` verifies; the dataset regenerates byte-identically from
`seed` + `generator_version`; `dsel verify` refuses to sign a report given
`reportable=false`, and tampering with one metric value changes the root and is detected.

---

## Validity gates after this plan

findings.md §7.5 has six. This adds six and splits one.

| Gate | Limit | Verdict | New |
|---|---|---|---|
| `driver_cpu_pct` | 70, p95 of quota | INVALID | |
| `driver_worker_cpu_pct` | 70, **max over workers** | `INCONCLUSIVE_DRIVER_BOUND` | ★ |
| `schedule_lag_runaway` | lag p99 >20% of mean inter-arrival, or deficit >0 for 10 s | `INCONCLUSIVE_DRIVER_BOUND` | ★ |
| `app_tier_cpu_pct` | 70, p95 of quota | `INVALID(app_tier_saturated)` for DB claims; kept as an app result | ★ |
| `storage_backend` | must be `ext2/ext3` named volume | INVALID | |
| `readback_matches_envelope` | exact | INVALID | |
| `error_rate` | per class, from spec | INVALID | |
| `unknown_error_rate` | 0.1% | INVALID | ★ |
| `steady_state_reached` | within warmup | INVALID | |
| `plausibility` | per `expect` | **cell never starts** | moved to pre-flight |
| `latency_validity` | must be `open_loop_co_corrected` | refused by scorer | ★ |
| `work_mem_projection` | ≤ `memory.max` | **cell never starts** | ★ |
| `cpu_throttling` | `throttled_usec` >5% of window | flag, surface, not INVALID | ★ |

`INCONCLUSIVE_DRIVER_BOUND` is deliberately distinct from `INVALID`. "We could not load
this engine hard enough to find its limit" is a different fact from "this measurement is
broken", and the report must say the first rather than silently penalising the engine.

---

## Files

Maps onto findings.md §10. `=` as specified · `~` extended · `★` new.

```
src/dsel/
├── spec/{models,canonical,loader}.py    =
├── gate/{capabilities,licences,environment}.py   =
├── data/{generator,distributions,hashing}.py     =
├── adapters/base.py        ~ + plausibility_probe, sample_internal(tier),
│                             connection_state, provoke(kind), authoritative_version
├── adapters/postgres.py    ~ reference impl, the slice target
├── adapters/{valkey,mongodb,clickhouse,opensearch}.py   =  (W1–W4)
├── runtime/{docker,envelope,storage,teardown}.py  =
├── runtime/cgroup.py       ★ cpu.stat / cpu.max / memory.* / pids.current readers
├── runtime/events.py       ★ docker events: oom | die | health_status
├── compose/                ★ NEW PACKAGE
│   ├── render.py               Jinja2 → compose.rendered.yaml            (D2)
│   ├── budget.py               cpuset/memory assertion                   (S2)
│   └── templates/, grafana/provisioning/, grafana/dashboards/
├── live/                   ★ NEW PACKAGE
│   ├── schema.py ndjson.py merge.py
│   ├── sampler/{containers,engine_pg,backend_pg,net,pool}.py
│   ├── exporter.py             tail → prometheus_client Collector        (D3)
│   └── tui.py                  dsel watch                                (S8a)
├── driver/{scheduler,histogram,patterns,calibrate}.py  ~
├── driver/worker.py pool.py transport.py ramp.py probe.py workload.py  ★
├── app/                    ★ NEW PACKAGE — the app tier
│   ├── main.py pools.py spans.py metrics.py
│   └── dal/{postgres,valkey,clickhouse,opensearch,mongodb}.py
├── metrics/{container,engine,validity}.py  ~
├── metrics/plausibility.py plans.py       ★
├── phenomena/              ★ NEW PACKAGE — derivation, kept apart from sampling
│   └── {conn_cliff,backend_growth,conn_lifecycle,joins}.py
├── scoring/*.py            =
├── audit/manifest.py       ~ + app_envelope, ab_delta_valid, vcpu_relative_speed,
│                             heterogeneous_cores, obs_component_inventory
└── audit/{environment,bundle,verify}.py   ~
schema/{metrics,environment}.schema.json   ★ committed, CI drift-checked
```

**Two structural rules, enforced in CI:** `live/sampler/*` only writes records and never
derives a phenomenon; `phenomena/*` only reads `metrics.ndjson` and never touches Docker
or the engine. That separation is what makes S15's acceptance criterion achievable.

---

## Risks, in the order they will bite

1. **Cores 0-1 are oversubscribed and the app tier pays.** Mitigation is honesty:
   `ab_delta_valid=false` on `profile=local`, deep observability forbidden in
   `mode=full`, PATH B capped at 60% of the measured app ceiling. Build S2 first so the
   arithmetic is visible on day one instead of discovered at S14.
2. **Heterogeneous cores.** ~~See S1.~~ **Answered 2026-09-02, and the answer was not
   the expected one:** guest vCPUs are indistinguishable from inside the VM, so the
   assumption cannot be examined here at all. What the probe found instead is that the
   slices do not isolate — see *Hardware slices* above, and risk 5.
3. **The GIL silently defeats the driver-CPU gate.** Never ship a single-process driver.
4. **Valkey out-runs the Python driver, permanently.** Settled, not open: GPL is ruled
   out, so there is no admissible open-loop tool that can saturate it here. Report
   `INCONCLUSIVE_DRIVER_BOUND` with the driver-side ceiling recorded — **never as a low
   score.** The scorer must treat this verdict as "not measured", not as a bad result;
   getting that wrong would silently penalise the fastest engine in the set.
5. **Driver and app tier cannot share cores.** Both have a 70%-of-quota gate; sharing
   makes each gate's denominator a function of the other's load and neither means
   anything. The app tier shares 0-1 with observability only because there is nowhere
   else — which is exactly why `ab_delta_valid=false` locally.
   **Measured 2026-09-02 and worse than written:** tiers on *disjoint* cpusets do not
   isolate either, costing 20–30% under full neighbouring load. The gates remain worth
   having as tripwires, but a passing `driver_cpu_pct` no longer means the engine had
   its slice to itself. Treat every local engine number as depressed by the driver.
6. **`.hlog` interop is an unverified single point of failure.** Verify at S10.
7. **Sampling `pg_stat_activity` at 1 Hz becomes the load it measures** at high
   connection counts. Backpressure to 10 s above 256 connections, recorded not silent.
8. **OpenSearch may be a second "cannot run here"** — its `vm.max_map_count` bootstrap
   check is host-global and `vm.*` is not in Docker's `--sysctl` allowlist. Verify the
   linuxkit default at W3. If it also fails, two of five candidates are environmentally
   excluded and the measurable set drops to Postgres, ClickHouse and a driver-bound
   Valkey. Worth knowing before investing in the W3 adapter.
9. **Prometheus series churn**, clock drift across long soaks, and `metrics.ndjson` merge
   determinism — all have specific guards in S6/S8b.
10. **Docker Hub anonymous pull limits**, hit on 2026-09-02 during S1. Digest resolution
   is now local-first, caching the index → platform mapping under the index digest — a
   mapping between two content addresses, so it cannot go stale. S3–S5 still need to pull
   five engine images at least once, so `docker login` before provisioning.

**Build early to de-risk:** S2 budget · S1 vCPU probe · S6 schema · S10 hlog interop ·
S13 app ceiling. Discovered late, these five force rework of everything downstream.

---

## Verification

- **Per-step acceptance criteria** above are mechanically checkable, not prose.
- **Cross-check the driver against `pgbench`** (S12) and `valkey-benchmark` (W1). If the
  first-party driver disagrees beyond the noise floor, the driver is wrong.
- **`.hlog` round-trip** against the Java reference implementation (S10).
- **Merge determinism**: 100 shuffled shard merges, byte-identical output (S6).
- **Independent re-derivation**: a separate script recomputes knee and collapse from
  `metrics.ndjson` alone (S15), and recomputes the ranking from `results` + `scoring`
  without re-running anything (G2, with `exp09-scoring.py` as the fixture).
- **End to end**: `dsel run specs/uc1-orders-inventory.yaml --smoke` provisions a
  digest-pinned Postgres, runs both ramps against both paths, streams live to the TUI and
  Grafana, trips at least one validity gate deliberately, and emits a bundle whose
  `bundle_root` verifies.

- **README honesty check** (S0): the first paragraph states that this harness produces
  mechanisms and scaling curves, not reportable capacity numbers, and that `dsel verify`
  enforces it. If that sentence ever becomes untrue, the build has drifted.
