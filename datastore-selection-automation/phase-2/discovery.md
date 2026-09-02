# Phase 2 — Discovery

**Date:** 2026-09-01. Version, licence and maintenance claims here were verified
against GitHub releases/tags (via the API), package registries, and official docs on
that date. Items that could not be pinned to a primary source are marked
**UNVERIFIED** and must not be relied on.

A recurring theme in this survey: **`pushed_at` on a GitHub repo is not a maintenance
signal.** Three of the tools initially assessed as "active" turned out to be dormant
once the commit histogram was read (TSBS, wrk2, sysbench). The harness's own component
inventory should record last-*commit* dates, not last-push.

---

## 2.1 Benchmark harnesses and load drivers

| Tool | Latest | Date | Licence | Alive? | Engines | Workload format | Percentiles | Corrects coordinated omission |
|---|---|---|---|---|---|---|---|---|
| **NoSQLBench** | 5.25.12-release | 2026-06-01 | Apache-2.0 | **Very** | 22 adapters incl. CQL, Mongo, DynamoDB, JDBC, OpenSearch, 4 vector DBs | YAML/JSON/Jsonnet op templates + bindings | HdrHistogram | **Yes** — `cyclerate` → `servicetime`/`waittime`/`responsetime` |
| **k6** | v2.2.0 | 2026-08-10 | **AGPL-3.0** | **Very** (Grafana) | HTTP; DB via xk6 | JavaScript | yes | **Yes** — `constant-arrival-rate` |
| **pgbench** | in PG 18.6 | 2026-08-13 | PostgreSQL | Alive | PostgreSQL only | `-f` script + `\set` | **none** | **Yes** — `--rate`, reports schedule lag |
| **BenchBase** | no releases (tags v2021/v2023) | commit 2025-12-13 | Apache-2.0 (LICENSE text; API says NOASSERTION) | Slowing | 11 JDBC targets, 19 benchmarks | **XML** | p0/25/50/75/90/95/99/100 | Partial — `<rate>` + `<arrival>POISSON` |
| **YCSB** | 0.17.0 | **2019-10-06** | Apache-2.0 | Maintenance only | ~48 bindings | `.properties` | p50/95/99 (HdrHistogram) | **Optional, off by default** — `measurement.interval=both` |
| **go-ycsb** | v1.0.3 | 2025-12-31 | Apache-2.0 | Slowing | 20 drivers | `.properties` | yes | No |
| **Locust** | 2.46.4 | 2026-08-23 | MIT | **Very** | anything (Python) | Python | p50…p99.99 | Partial — `constant_pacing` degrades under saturation |
| **HammerDB** | v6.0 | 2026-06-26 | GPL-3.0 | **Very** (TPC-Council) | Oracle, MSSQL, Db2, MySQL, MariaDB, PG | **TCL** | P25/50/75/95/99 (new in v6.0) | No |
| **sysbench** | 1.0.20 | **2020-04-24** | GPL-2.0 | Semi-dormant (commit 2025-03-09) | MySQL + PostgreSQL only | **Lua** | `--percentile` (def. 95) | No |
| **TSBS** | **never released** | commit 2026-05-27 (CI only) | MIT | **Dormant since 2021** | 11 TSDBs | Go CLI flags | **no p95/p99 at all** | No |
| **memtier_benchmark** | 2.5.1 | 2026-07-16 | GPL-2.0 | Alive | RESP + memcache | CLI flags | HdrHistogram | rate-limiting only |
| **valkey-benchmark** | in Valkey 9.1.2 | 2026-09-01 | BSD-3-Clause | **Very** | RESP3, TLS, RDMA | CLI flags | cumulative dist. | No |
| **wrk2** | **never released** | commit **2019-09-24** | Apache-2.0 | **Dormant ~7 yrs** | HTTP | Lua | p50…p99.99 | **Yes** — its whole purpose |
| **oha** | v1.16.0 | 2026-08-23 | MIT | Alive | HTTP | CLI flags | yes | **Yes** — `--latency-correction` |
| **ClickBench** | rolling | pushed 2026-09-01 | **CC BY-NC-SA 4.0** ⚠ | **Very** | 182 engine dirs | `create.sql` + `queries.sql` + shell | wall-clock ×3 | N/A (sequential) |
| **DuckDB tpch/tpcds** | DuckDB 1.5.5 | 2026-07-22 | MIT | **Very** | in-process | `CALL dbgen(sf=N)` | N/A | N/A |
| **tpcgen-rs** | v3.0.0 | 2026-06-29 | Apache-2.0 | **Very** | TPC-H → Parquet | CLI | N/A | N/A |

### Key discoveries

**NoSQLBench is the only mature general-purpose cross-engine harness.** It has a real
adapter SPI loadable from **external JARs** (no fork required), genuine CO correction,
CSV/Prometheus/SQLite reporters, and an official container image. Cost: a JVM, and a
YAML DSL with a learning curve. The `adapter-example` module is a complete working
template.

**paradedb/benchmarker (MIT, created 2026-02-03, pushed 2026-09-01) is almost exactly
the architecture this project needs** — a k6 xk6 extension with a `backends/` directory
per engine (clickhouse, elasticsearch, mongodb, opensearch, paradedb, postgres), Docker
Compose profiles, a data loader, a dashboard with P50/P90/P95/P99, per-container
CPU/memory capture, and a **phase timer that staggers backends so they do not compete
for host resources**. It is young (29★) and small — best used as a reference design,
not a dependency. Its existence is the strongest evidence that this design is tractable.

**Nothing on the market does automated *selection*.** The benchmark tools measure; none
of them gate on feasibility, weight qualitative criteria, or emit a recommendation.
This gap is the actual product.

**Licence landmines found:**
- **ClickBench is CC BY-NC-SA 4.0** — NonCommercial. Its queries and data are not
  usable in a commercial product without counsel. Easy to miss; the GitHub API reports
  only `NOASSERTION`.
- **k6 is AGPL-3.0.** Fine as an invoked binary; a consideration if linked.
- **xk6-sql core is Apache-2.0 but several driver sub-modules are reported AGPL-3.0.**
  **UNVERIFIED** — raw LICENSE text not read. Check before relying on it.
- **TPC kits** (dbgen/dsdgen) are under the TPC EULA, not an OSS licence. §9 permits
  redistribution *only* if the full EULA travels with it, a 12pt-caps notice is
  included, and no fee is charged. §4c permits publishing non-audited results only if
  labelled "Derived from" and marked not comparable to official TPC results. TPC
  enforces this publicly.
- **py-tpcc has no LICENSE file** — treat as all rights reserved.

### Methodology pitfalls catalogued

1. **Coordinated omission.** Gil Tene, *How NOT to Measure Latency*
   (infoq.com/presentations/latency-response-time). Closed-loop generators stop
   issuing requests when the target stalls, under-reporting p99 by an order of
   magnitude. Marc Brooker's *Open and Closed, Omission and Collapse* is the best
   modern companion. **Corrects by default:** wrk2, oha, pgbench, NoSQLBench,
   BenchmarkSQL. **Opt-in:** k6, YCSB, BenchBase. **Does not:** sysbench, TSBS,
   HammerDB, memtier, redis-benchmark, go-tpc, Locust under saturation.
2. **Client-side bottleneck.** pgbench documents it outright; YCSB saturates around
   ~16 client threads/node. **Rule: record load-generator CPU and invalidate any run
   where the driver exceeded ~70%.**
3. **JVM/JIT warmup.** QuestDB measured a **~13% score change** from moving 3 → 10
   warmup iterations — enough to reorder rankings. This is the strongest argument for
   preferring non-JVM drivers, or for a mandatory discarded warmup phase.
4. **Durability defaults are not comparable.** PostgreSQL `fsync=on` +
   `synchronous_commit=on`; MySQL/InnoDB `innodb_flush_log_at_trx_commit=1`; MongoDB
   implicit `w:majority`; **Redis `appendfsync everysec`**; Cassandra
   `commitlog_sync: periodic` at **10000 ms**. Redis and Postgres differ by three
   orders of magnitude in fsync frequency. Must be normalised or loudly recorded.
5. **Page cache.** ClickBench requires both `drop_caches` *and* a DB restart for a true
   cold run, and calls page-cache-only clearing a "lukewarm cold run".
6. **Burstable cloud storage.** EBS `gp2` bursts to 3000 IOPS for ~30 min from a full
   credit balance; `gp3` has no burst. A short run inside the burst window reports
   fiction.
7. **Academic primary source:** Raasveldt, Holanda, Gubner, Mühleisen, *Fair
   Benchmarking Considered Difficult: Common Pitfalls In Database Performance Testing*,
   DBTest'18, doi:10.1145/3209950.3209955.

---

## 2.2 Synthetic data generation and skew modelling

| Tool | Latest (date) | Licence | Deterministic given a seed? | Schema-aware / RI |
|---|---|---|---|---|
| **SDV** | 1.38.2 (2026-08-28) | **BUSL-1.1** ⚠ not OSS | partial, session-scoped | Yes (FK metadata) |
| **Faker (py)** | 40.37.0 (2026-08-21) | MIT | per-version only; **explicitly not across versions** | No |
| **@faker-js/faker** | 10.6.0 (2026-08-14) | MIT | same caveat | No |
| **Mimesis** | 21.0.0 (2026-07-16) | MIT | yes; cross-version unstated | Yes (`SchemaBuilder`, `sb.ref()`) |
| **Datafaker** (JVM) | 2.7.0 (2026-06-24) | Apache-2.0 | yes | No |
| **Snowfakery** | 4.2.1 (2026-01-09) | BSD-3 | **No — no `--seed` exists** | Yes (YAML recipes) |
| **Synthea** | v4.0.0 (2026-03-05) | Apache-2.0 | **Yes** — same seed + same version | Healthcare (FHIR R4) |
| **json-schema-faker** | 0.6.3 (2026-08-01) | MIT | **Yes** — seeded Mulberry32, `--seed` | **Yes** — JSON Schema 2020-12 |
| **tpcgen-rs** | v3.0.0 (2026-06-29) | Apache-2.0 | **Yes — MD5 byte-verified vs reference dbgen** | Fixed TPC-H schema |
| **LDBC SNB datagen** | HEAD (2026-04-06) | Apache-2.0 | **Yes — blockId *is* the seed** | Graph, RI by construction |
| **YCSB generators** | — | Apache-2.0 | **No — `ThreadLocalRandom`, no seed anywhere** | No |
| **TPC dbgen/dsdgen** | H 3.0.1 / DS 4.0.0 | **TPC EULA v2.2** ⚠ | yes (by scale factor) | Fixed schema |

### The determinism finding that shapes the design

**Almost nothing guarantees determinism *across versions*, and several tools say so
explicitly.** Faker's own docs: *"results are not guaranteed to be consistent across
patch versions… pin the version down to the patch number."* @faker-js/faker: *"When
upgrading… you may get different values for the same seed."*

The load-bearing one is **NumPy NEP 19**: `Generator` carries **no stream-compatibility
guarantee**; only `.bytes()`, `.integers()` and `.random()` are stable, and NumPy
reserves the right to change distribution algorithms on minor releases. NumPy is the
substrate under most Python generators. **Sequential-PRNG determinism is therefore a
version-pinning problem, not a seed problem** — and version pinning is a weaker
guarantee than we want for an audit bundle that should still verify in three years.

**Two patterns survive this, and both were found in the wild:**

1. **Index-derived seeding** (LDBC's pattern). `PersonGenerator.generatePersonBlock(int
   blockId, …)` — *"@param blockId Used as a seed to feed the pseudo-random number
   generators"* → `RandomGeneratorFarm.resetRandomGenerators(seed)`. Because the seed
   derives from the block index, output is **independent of Spark partition order and
   parallelism**. LDBC calls determinism "a core defining principle" and deliberately
   exposes **no user-facing `--seed`**.
2. **Byte-verified reference conformance** (tpcgen-rs). Its TESTING.md compares MD5 of
   full output files against reference `dbgen`: *"Comparisons are byte-for-byte rather
   than statistical."*

**Adopted:** derive every value from a stable hash of `(seed, table, row_id, column)`
rather than from a library PRNG's call sequence. This was prototyped and verified in
Phase 3 (exp06) — byte-identical output across reverse ordering, 8-way threading, and
a fresh subprocess.

### Skew modelling

| Pattern | Where | Parameters |
|---|---|---|
| Single scalar exponent | YCSB `zipfianconstant`=0.99; sysbench `--rand-zipfian-exp`=0.8; pgbench `random_zipfian` p∈[1.001,1000]; memtier `--key-zipf-exp`=1 | one continuous shape |
| **Explicit hot-set fraction** | YCSB `hotspot` (`hotspotdatafraction`=0.2, `hotspotopnfraction`=0.8); sysbench `special` | % of keyspace hot, % of traffic to it |
| Self-similar recursive | Gray et al. 1994, parameter `h` | first `h·N` keys get `1−h` of the mass |
| Bounded modular hash | TPC-C `NURand(A,x,y)` | spec-fixed `A` per field |

**Adopted: the hot-set-fraction form.** `(hot_fraction, hot_traffic_fraction)` is
directly interpretable by a business stakeholder and **directly measurable against
production telemetry**; a bare Zipf θ is neither. Verified in exp06: the model hit
80.1% of draws landing in the hot 20%.

**Rank-skew and key-locality must be decoupled**, or you accidentally benchmark
sequential locality instead of skew. Two independent implementations confirm this
insight: YCSB's `ScrambledZipfianGenerator` applies `Utils.fnvhash64` to scatter
clustered popularity across the keyspace, and PostgreSQL 17 added `permute(i, size[,
seed])` with docs explicitly recommending `\set r random_zipfian(...)` then `\set k 1 +
permute(:r, :size)`.

Corrections found to commonly-repeated errors: **TPC-C's `C_LAST` NURand constant is
A=255**, not 1023 (1023 is `C_ID`; 8191 is `OL_I_ID`) — verified against TPC-C spec
v5.11 §2.1.6 and corroborated against BenchBase's `TPCCUtil.java`. **YCSB's default
`requestdistribution` is `uniform`, not zipfian** — the famous 0.99 applies only when
explicitly selected.

### Dead or moved — do not plan around these
- **Gretel**: acquired by NVIDIA; both repos **archived 2026-02-18**; was never
  OSI-licensed.
- **Neosync**: **archived 2025-08-30** (acquired by Grow Therapy).
- **ydata-synthetic**: renamed → `fg-data-synthetic` (MIT). `ydata-sdk` is a separate
  **proprietary** product, not the OSS successor.
- **tpchgen-rs** moved → `datafusion-contrib/tpcgen-rs`.
- **ann-benchmarks** now declares itself unmaintained and points to **VIBE**.

---

## 2.3 Configuration / spec languages

| | Latest | Licence | Canonical form → hashable? | Defaulting | Errors | JSON Schema export |
|---|---|---|---|---|---|---|
| **JSON Schema** | 2020-12 (still current); IETF draft-03 2026-08-26 | BSD-3 | ❌ none native — pair with RFC 8785 | ⚠ `default` is annotation-only | impl-dependent | n/a |
| **Pydantic** | 2.13.5 (2026-08-28) | MIT | ❌ none native | ✅ **best in class** | ✅ **best in class** | ✅ targets 2020-12 |
| **CUE** | 0.17.1 (2026-07-16) | Apache-2.0 | ⚠ normal form, no stable digest | ✅ `*default \| type` | ✅ strong | ⚠ **export experimental** |
| **Dhall** | std v23.1.0 (2025-01-16) | BSD-3 | ✅ **semantic hashing is a spec feature** | ✅ | ✅ | ⚠ via dhall-to-json |
| **Protobuf** | v36.1 (2026-08-31) | BSD-3 | ❌ **explicitly non-canonical** | ⚠ proto3 presence footgun | ⚠ codegen-level | ⚠ partial |
| **OpenAPI** | **3.2.0** (2025-09-19) | Apache-2.0 | ❌ | inherits JSON Schema | via validator | ✅ 3.1+ *is* 2020-12 |
| **TypeSpec** | 1.15.0 (2026-08-11) | MIT | ❌ | ✅ | ✅ | ✅ production-ready |

**JSON Schema 2020-12 is still the current dialect** as of 2026-09-01 — verified at
json-schema.org/specification. Its successor is now an IETF WG effort
(`draft-ietf-jsonschema-json-schema`, rev -03, 2026-08-26), at least a year out. 2020-12
is a safe target.

**On hashability — the axis that actually separates them:**
- **Dhall is the only one with semantic hashing in the standard.** It hashes the
  **αβ-normalised** expression, so formatting, comments, and variable naming wash out:
  *"The semantic integrity check is not a hash of the raw underlying text."*
- **Protobuf is the anti-pattern.** Google's own docs: *"protobuf serialization is not
  (and cannot be) canonical"*, varying with build flags and library updates.
  **Never hash serialized protobufs.**
- **Everyone else needs RFC 8785 (JCS).** `export → JCS → SHA-256` is the pragmatic
  recipe. Verified working in exp06: key reordering preserved the hash, a semantic edit
  changed it.

**CUE is disqualified** for this use: its JSON Schema *export* (added 2025) carries the
in-source warning *"this functionality is currently experimental. The form of the
generated schema may, and probably will, change from release to release."* That
volatility is fatal for a hashable artifact. It is also Go-only with no viable Python
binding, and still pre-1.0 after eight years.

---

## 2.4 Open questions carried into Phase 3

- Does a maintained cross-engine harness beat writing a thin first-party driver over
  official client libraries? (NoSQLBench is the only real contender; the JVM warmup
  cost is a real objection given QuestDB's measured 13%.)
- Is the macOS dev machine usable for results at all? *(Answered decisively in
  Phase 3: no.)*
- Do cgroup limits reach the engine, or does each engine need explicit knobs?
  *(Answered in Phase 3: both, and verify by readback.)*
