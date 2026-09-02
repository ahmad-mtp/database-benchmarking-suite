# Sources

Verified on **2026-09-01**. Sources are grouped by the question they answered. Items
marked **UNVERIFIED** could not be pinned to a primary source and must not be relied on
without independent checking.

A note on method: GitHub's `pushed_at` is **not** a maintenance signal — it advances on
CI pushes and forks. Three tools initially assessed as active (TSBS, wrk2, sysbench)
turned out to be dormant once the commit history was read. All liveness claims below are
based on commit dates, not push dates.

---

## 1. Primary evidence — executed on this machine

These are not citations; they are the experimental record produced during Phase 3 and
are the basis for every mechanical claim in `findings.md`.

| Artifact | What it establishes |
|---|---|
| `phase-3/env-capture.txt` | Test bed: Apple M5, macOS 26.6.2, Docker Engine 29.7.2, kernel 7.0.12-linuxkit, cgroup v2, overlayfs |
| `phase-3/digest-resolution.txt` | OCI index vs per-platform manifest digests resolved without pulling |
| `phase-3/exp01-postgres.{sh,log}` | Full provision→health→load→run→teardown on Postgres 18.6; pgbench `--rate` coordinated-omission correction; `nproc`=10 under `--cpus=2` |
| `phase-3/exp02-valkey-compose.{sh,log}` | Compose `deploy.resources.limits` applying outside Swarm; `cpuset` making `nproc`=2; Valkey's fictitious `redis_version`; 1.87× run-to-run spread |
| `phase-3/exp03-autotune.{sh,log}` | ClickHouse 25.8.33.6 is cgroup-aware (`CGroupMaxCPU`, `max_threads='auto(2)'`) under quota, affinity, and both |
| `phase-3/exp04-storage.{sh,log}` | First storage probe; busybox `dd` lacks `oflag=dsync` — superseded by exp05 |
| `phase-3/exp05-storage-db.{sh,log}` | **Decisive:** named volume vs bind mount vs tmpfs measured at the DB layer. Bind mount CV 18% vs 1.3%; `docker stats` BlockIO reads 8.19 kB instead of 6.44 GB off block storage |
| `phase-3/exp06-determinism.{py,log}` | Index-derived generation byte-identical across ordering, 8-way threading, fresh process; RFC 8785 canonical hashing; merkle tamper detection |
| `phase-3/exp07-mongo-kernel.log` | MongoDB 8 fatally refuses kernel ≥ 6.19 (SERVER-121912); MongoDB 7.0.40 healthy on the same host |
| `phase-3/exp08-cpu-apis.log` | Five CPU-count APIs give three different answers in one container; `/proc/meminfo` reports the whole VM |
| `phase-3/exp09-scoring.{py,log}` | Gate → veto → weight → rank → sensitivity → Pareto, end to end |
| `phase-3/exp10-normalisation.{py,log}` | **Min-max rank reversal demonstrated live**; reference anchoring shown invariant |
| `phase-3/exp11-anchored-sensitivity.{py,log}` | Breaking points under the final anchored model: 5.4% weight change reverses UC-1 |

---

## 2. Benchmark harnesses and load drivers

| Source | Relevance |
|---|---|
| https://github.com/nosqlbench/nosqlbench | The only mature cross-engine harness; 22 adapters, external-JAR adapter SPI, CO correction via `cyclerate`. v5.25.12-release (2026-06-01), Apache-2.0 |
| https://github.com/grafana/k6 | Open-loop `constant-arrival-rate` executor; v2.2.0 (2026-08-10), **AGPL-3.0** |
| https://github.com/grafana/xk6-sql | k6 SQL extension; requires recompiling the binary per driver; sub-module licences reportedly a mix of Apache-2.0 and AGPL-3.0 — **UNVERIFIED** |
| https://github.com/paradedb/benchmarker | Closest existing architecture to this design: per-engine `backends/`, Compose profiles, staggered phase timer. MIT, created 2026-02-03. Reference design, not a dependency |
| https://github.com/cmu-db/benchbase | Successor to OLTPBench; 11 JDBC targets, 19 benchmarks, XML workload format, `<arrival>POISSON`. Also the corroborating source for TPC-C's `NURand` constants |
| https://github.com/brianfrankcooper/YCSB | Rejected: draws from `ThreadLocalRandom` with no seed property, so datasets are irreproducible; last release 0.17.0 (2019-10-06) |
| https://github.com/pingcap/go-ycsb | Go port; v1.0.3 (2025-12-31), Apache-2.0 |
| https://github.com/timescale/tsbs | Rejected: reports min/med/mean/max/stddev and **no p95/p99**; dormant since 2021 |
| https://github.com/akopytov/sysbench | Rejected: MySQL and PostgreSQL only; last release 1.0.20 (2020-04-24) |
| https://github.com/giltene/wrk2 | Rejected as a dependency but its CO-correction model is the reference; dormant since 2019-09-24 |
| https://www.postgresql.org/docs/current/pgbench.html | `--rate`, `--latency-limit`, schedule-lag reporting — the reference implementation for CO-correct open-loop scheduling, and our calibration baseline |
| https://github.com/valkey-io/valkey | `valkey-benchmark --csv` p50/p95/p99/max; second calibration baseline. Valkey 9.1.2, BSD-3-Clause |
| https://github.com/RedisLabs/memtier_benchmark | RESP/memcache driver with HdrHistogram; v2.5.1 (2026-07-16), GPL-2.0 |
| https://www.hammerdb.com/ | TPC-C/TPC-H-derived; v6.0 (2026-06-26) added P25/50/75/95/99; GPL-3.0, TPC-Council governed |
| https://github.com/locustio/locust | Python load framework; 2.46.4 (2026-08-23), MIT; `constant_pacing` degrades under saturation |
| https://github.com/ClickHouse/ClickBench | 182 engine configurations and the `drop_caches`+restart cold-run methodology. ⚠ **CC BY-NC-SA 4.0 — NonCommercial** |
| https://github.com/HdrHistogram/HdrHistogram | `recordValueWithExpectedInterval`, `copyCorrectedForCoordinatedOmission`, serialised log format. v2.2.2, dual CC0-1.0 / BSD-2-Clause |
| https://www.infoq.com/presentations/latency-response-time/ | Gil Tene, *How NOT to Measure Latency* — the canonical statement of coordinated omission |
| https://brooker.co.za/blog/2021/08/13/open-closed.html | Marc Brooker, *Open and Closed, Omission and Collapse* — the best modern treatment of open vs closed loop |
| https://doi.org/10.1145/3209950.3209955 | Raasveldt, Holanda, Gubner, Mühleisen, *Fair Benchmarking Considered Difficult*, DBTest'18 — the academic catalogue of the pitfalls this design guards against |
| https://questdb.com/blog/ | Source of the measured ~13% JMH score swing from JIT warmup iterations — the quantified case against a JVM-based driver |

## 3. Synthetic data generation and skew

| Source | Relevance |
|---|---|
| https://github.com/ldbc/ldbc_snb_datagen_spark | The index-derived seeding pattern we adopted: `blockId` *is* the seed, so output is independent of Spark partitioning. Deliberately exposes no `--seed` |
| https://github.com/datafusion-contrib/tpcgen-rs | TPC-H generation with **MD5 byte-for-byte verification against reference `dbgen`** — the strongest determinism evidence surveyed. v3.0.0 (2026-06-29), Apache-2.0. Note: moved from `tpchgen-rs` |
| https://numpy.org/neps/nep-0019-rng-policy.html | **NEP 19** — NumPy explicitly refuses stream-compatibility guarantees across versions. The load-bearing reason we do not use a sequential PRNG |
| https://faker.readthedocs.io/ | Faker 40.37.0 (2026-08-21), MIT. Documents that seeded results are *not* guaranteed consistent across patch versions |
| https://fakerjs.dev/ | @faker-js/faker 10.6.0 (2026-08-14), MIT. Same cross-version caveat |
| https://github.com/sdv-dev/SDV | Rejected: **BUSL-1.1**, not open source, with a Synthetic Data Service carve-out. 1.38.2 (2026-08-28) |
| https://github.com/SFDO-Tooling/Snowfakery | Rejected: **no `--seed` parameter exists at all**. 4.2.1, BSD-3 |
| https://github.com/lk-geimfari/mimesis | Seeded and schema-aware (`SchemaBuilder`, `sb.ref()`); 21.0.0 (2026-07-16), MIT |
| https://github.com/synthetichealth/synthea | Deterministic given seed + version; v4.0.0 (2026-03-05), Apache-2.0 |
| https://www.tpc.org/tpc_documents_current_versions/ | TPC-C v5.11 §2.1.6 (`NURand` constants: `C_LAST`=255, `C_ID`=1023, `OL_I_ID`=8191) and **TPC EULA v2.2** §4c/§9 — the publication and redistribution constraints |
| https://www.postgresql.org/docs/17/pgbench.html | PostgreSQL 17's `permute(i, size[, seed])` and the documented `random_zipfian` + `permute` idiom — independent confirmation that rank-skew must be decoupled from key locality |

## 4. Containerisation, provisioning, and parity

| Source | Relevance |
|---|---|
| https://docs.docker.com/engine/containers/resource_constraints/ | `--cpus`, `--cpuset-cpus`, `--memory`, `--memory-swap`, `--pids-limit` semantics on cgroup v2 |
| https://compose-spec.io/ | The Compose Specification; `deploy.resources.limits` vs top-level shorthands |
| https://docs.docker.com/reference/cli/docker/compose/up/ | `--wait` / `--wait-timeout` and `depends_on: condition: service_healthy` |
| https://docs.docker.com/reference/cli/docker/buildx/imagetools/inspect/ | Resolving a tag to index and per-platform manifest digests **without pulling** |
| https://jira.mongodb.org/browse/SERVER-121912 | The kernel ≥ 6.19 incompatibility that makes MongoDB 8 unstartable on Docker Desktop — verified reproduced in exp07 |
| https://www.mongodb.com/docs/manual/tutorial/transparent-huge-pages/ | MongoDB 8+ **reversed** its long-standing advice and now asks for THP `always` |
| https://redis.io/docs/latest/operate/oss_and_stack/management/admin/ | Redis asks for THP `never` — irreconcilable with MongoDB on one host |
| https://clickhouse.com/docs/en/operations/tips | ClickHouse asks for THP `madvise` — the third irreconcilable value |
| https://kind.sigs.k8s.io/ | Rejected for this use: CPU Manager static policy does not work in nested containers |
| https://k3d.io/ | Rejected: `--servers-memory` only fakes `/proc/meminfo` with no cgroup enforcement |
| https://testcontainers.com/ | Rejected for this use: reuse is experimental and defaults on in Node; rootless Podman disables Ryuk cleanup |
| https://docs.podman.io/ | Deferred to v2: rootless **silently ignores** `--cpus`/`--cpuset-cpus` without systemd cgroup delegation |
| https://nixos.org/manual/nix/stable/command-ref/new-cli/nix3-flake.html | Nix flakes — the only toolchain pinning with hashed locks spanning macOS-arm64 + Linux and covering DB client binaries |
| https://docs.brew.sh/Brew-Bundle-and-Brewfile | Homebrew rejected: documents that a Brewfile lock file does not and will not exist |
| https://docs.astral.sh/uv/ | uv 0.12.8 — pins the interpreter and PyPI deps; cannot provision DB binaries |
| https://www.docker.com/pricing/ | Docker Desktop licence: free commercial use only below 250 employees and $10M revenue |

## 5. Specification, canonicalisation, and schema

| Source | Relevance |
|---|---|
| https://json-schema.org/specification | **2020-12 is still the current dialect** as of 2026-09-01; successor is IETF `draft-ietf-jsonschema-json-schema` rev-03 (2026-08-26) |
| https://docs.pydantic.dev/latest/ | Pydantic v2.13.5 — defaulting, validation, structured errors and 2020-12 export in one library |
| https://www.rfc-editor.org/rfc/rfc8785 | RFC 8785 JSON Canonicalisation Scheme — the basis of spec identity; verified in exp06 |
| https://cuelang.org/docs/ | CUE 0.17.1 rejected: JSON Schema export self-declared experimental and expected to change between releases |
| https://docs.dhall-lang.org/ | Dhall — the only surveyed language with semantic hashing in its standard (hashes the αβ-normalised expression); rejected on ecosystem grounds |
| https://protobuf.dev/programming-guides/serialization-not-canonical/ | Google's own statement that protobuf serialisation *"is not (and cannot be) canonical"* — disqualifying for a hashed artifact |

## 6. Candidate engines — versions and licences

| Source | Relevance |
|---|---|
| https://www.postgresql.org/about/news/postgresql-183-179-1613-1517-and-1422-released-3246/ | PostgreSQL 18.6 current (2026-08-13); PG 18 released Sep 2025 |
| https://www.postgresql.org/support/versioning/ | Versioning and support policy |
| https://redis.io/blog/agplv3/ | Redis moved to AGPLv3 at 8.0 (May 2025); tri-licensed AGPLv3 / RSALv2 / SSPLv1 |
| https://en.wikipedia.org/wiki/Valkey | Valkey — Linux Foundation BSD-3-Clause fork of Redis 7.2.4; wire-compatible |
| https://www.mongodb.com/legal/licensing/community-edition | MongoDB Community under **SSPL v1 — not OSI-approved**; the working example for the licence gate |
| https://www.elastic.co/pricing/faq/licensing | Elasticsearch tri-licensed AGPLv3 / ELv2 / SSPL since Aug 2024 |
| https://en.wikipedia.org/wiki/OpenSearch_(software) | OpenSearch Apache-2.0; governance moved to the Linux Foundation's OpenSearch Software Foundation (Sep 2024) |
| https://github.com/ClickHouse/ClickHouse | ClickHouse Apache-2.0; 26.x current, 25.8.33.6 verified running |
| https://questdb.com/blog/best-time-series-databases/ | Tier-2 time-series licences: QuestDB Apache-2.0, TimescaleDB Apache-2.0 core + source-available TSL, InfluxDB 3 Core MIT/Apache-2.0 |
| https://hub.docker.com/_/postgres | Official image tags, `org.opencontainers.image.source`/`.revision` annotations |

## 7. Prior art on automated selection

| Source | Relevance |
|---|---|
| https://dbdb.io/ | CMU's Database of Databases — the most complete capability catalogue available; a plausible seed for the gate's capability tables |
| https://db-engines.com/en/ranking | Popularity ranking; useful only as a weak proxy for the `ecosystem_maturity` qualitative criterion |
| https://github.com/paradedb/benchmarker | The nearest thing to prior art for the *execution* half; still does no gating, weighting or recommendation |

**Finding, stated plainly:** no product surveyed does automated datastore *selection*.
The benchmark tools measure; none gate on feasibility, weight qualitative criteria, or
emit a recommendation. That gap is the reason this POC is a build rather than an
evaluation.
