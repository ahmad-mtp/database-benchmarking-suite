# Phase 3 — Local verification: what actually happens when you do this

Everything below was executed on this machine on 2026-09-01. Raw logs are the `.log`
files beside this document; the scripts that produced them are the `.sh` files.
Nothing here is recalled from memory — where a claim is not backed by a log line, it
says so.

## Test bed

| | |
|---|---|
| Host | Apple **M5**, 10 physical / 10 logical cores, 16 GiB RAM, macOS 26.6.2 (25G83), arm64 |
| Container runtime | **Docker Desktop 4.88.1**, Engine **29.7.2**, API 1.55, containerd v2.3.3, runc 1.4.3 |
| Linux VM | kernel `7.0.12-linuxkit`, aarch64, **cgroup v2**, `NCPU=10`, `MemTotal=8319504384` (7.75 GiB) |
| Storage driver | `overlayfs` via the containerd snapshotter |
| Compose | **v5.4.0**; buildx v0.36.1-desktop.1 |
| Confounder present | one unrelated container (`temporalio/temporal`, healthy, up 3 days) was running throughout — a real noisy neighbour, and visible in the results |

Capability flags from `docker info`: `MemoryLimit=true`, `SwapLimit=true`,
`CpuCfsPeriod=true`, `CpuCfsQuota=true`, `CPUShares=true`, `CPUSet=true`,
`PidsLimit=true`. So every cgroup control the harness needs is available even on
Docker Desktop for Mac.

---

## E1 — Digest pinning: the index/manifest distinction matters

`docker buildx imagetools inspect` resolves a tag to digests **without pulling**,
which is exactly what a run-planner needs.

```
postgres:18.6-alpine
  index (OCI image index):     sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2
    linux/amd64  manifest:     sha256:63bdc97d67b5133bf0e5ebd500bec6d046fa851dc81340d838f0347e616107e8
    linux/arm64  manifest:     sha256:d67c55f7cb9c9ee6a3b3d9aee1c28460be18d2d52debdd2a283a70e836070590
    (also arm/v6, arm/v7, 386, mips64le, ppc64le, riscv64, s390x + attestation manifests)
  annotations: base = alpine:3.24, created 2026-08-13T19:14:36Z,
               source = github.com/docker-library/postgres @ e00e1bd…
```

**Finding E1.1.** A tag resolves to an *index* digest, and each platform has its own
*manifest* digest. The harness must pin the **index digest** in the spec — that is the
identity that is portable across a macOS/arm64 dev box and a Linux/amd64 CI runner —
and record the **resolved per-platform manifest digest** in the run manifest as the
thing actually executed. Pinning a platform manifest digest makes the spec
unrunnable on the other architecture; pinning only the tag makes it non-reproducible.
Both failure modes are common.

**Finding E1.2.** Under the containerd snapshotter, `docker image inspect --format
'{{.Id}}'` returned `sha256:d3e1620b…` — *the index digest itself*, not a config
digest. `RepoDigests` agreed. Convenient, but it means image-identity capture code
must not assume `.Id` is a config digest; read `RepoDigests` and record both.

**Finding E1.3.** The official images publish `org.opencontainers.image.source` and
`.revision` annotations pointing at the exact docker-library commit that built them.
Free, high-quality provenance — capture it into the manifest.

---

## E2 — The fair-comparison trap: CPU *quota* is not CPU *count*

This is the single most important mechanical result of Phase 3.

| Run | Flags | `cpu.max` | `cpuset.cpus.effective` | `nproc` inside |
|---|---|---|---|---|
| exp01 Postgres | `--cpus=2.0` | `200000 100000` (=2 cores) | `0-9` | **10** ❌ |
| exp03 case A | `--cpus=2.0` | `200000 100000` | `0-9` | **10** ❌ |
| exp03 case B | `--cpuset-cpus=0-1` | `max 100000` (unlimited!) | `0-1` | **2** ✔ |
| exp03 case C | both | `200000 100000` | `0-1` | **2** ✔ |
| exp02 Valkey (Compose) | `deploy.resources.limits.cpus: 2.0` + `cpuset: "0-1"` | `200000 100000` | `0-1` | **2** ✔ |

**Finding E2.1 — `--cpus` throttles but does not hide cores.** With `--cpus=2.0`
alone, the container is CFS-throttled to 2 cores' worth of time but `nproc` still
reports 10. Any engine, driver, or runtime that sizes its thread pool from the
*visible core count* will spin up 10 threads to share 2 cores' quota — producing
throttling stalls and a badly distorted latency tail. `--cpuset-cpus` is what changes
the visible count.

**Finding E2.2 — but modern engines may read the cgroup directly, so you cannot
generalise from one engine.** ClickHouse 25.8.33.6 got it *right in all three cases*:

```
case A (quota only):      max_threads = 'auto(2)'   CGroupMaxCPU = 2
case B (affinity only):   max_threads = 'auto(2)'   (CGroupMaxCPU absent — no quota set)
case C (both):            max_threads = 'auto(2)'   CGroupMaxCPU = 2
```

It exposes `CGroupMaxCPU` and `CGroupMemoryTotal` in `system.asynchronous_metrics` and
self-limits from whichever signal is present. A well-engineered engine handles quota;
a naive one, or any tool calling `sysconf(_SC_NPROCESSORS_ONLN)`, does not. **You
cannot know which without checking, per engine, per version.**

**Finding E2.3 — memory is worse, because nothing hides it.** In every run,
`memory.max` was set correctly (`2147483648` for `--memory=2g`) but `/proc/meminfo`
`MemTotal` reported the **whole VM**: `8124516 kB`. ClickHouse likewise reported
`OSMemoryTotal = 8319504384` alongside the correct `CGroupMemoryTotal = 3221225472`.
Any engine sizing a cache as "a fraction of system RAM" will over-commit and get
OOM-killed, or will silently get a different effective cache size than its
competitor. This is a classic source of bogus comparisons.

**Rule adopted.** The harness must do all three, always:
1. set **both** `--cpuset-cpus` (affinity, identical core *set* per candidate) **and**
   `--cpus` (quota), plus `--memory` with `--memory-swap` equal to it (swap off);
2. set every engine's parallelism/memory knob **explicitly** in its config — never
   rely on auto-detection;
3. **read the value back from the running engine** and record it in the manifest.
   Verification, not assertion.

Step 3 is not theoretical. In exp02, Valkey was started with `--io-threads 2`;
`CONFIG GET io-threads` returned `2` but `INFO server` reported
**`io_threads_active:0`**. Configured ≠ active. Only the readback tells the truth.

---

## E3 — Health gating works, and per-engine readiness commands verified

| Engine | Readiness command (verified working) | start → healthy |
|---|---|---|
| PostgreSQL 18.6 | `pg_isready -U <u> -d <db> -h 127.0.0.1` | **5.49 s** |
| ClickHouse 25.8.33.6 | `clickhouse-client --user u --password p -q "SELECT 1"` | **5.56 / 5.38 / 5.38 s** |
| Valkey 9.1.2 | `valkey-cli ping` | sub-second (see caveat) |

`docker inspect -f '{{.State.Health.Status}}'` polling worked cleanly; the health log
records `Start`, `End`, `ExitCode` and `Output` per probe, which is capturable
evidence for the audit bundle.

**Finding E3.1 — `pg_isready` must target `127.0.0.1`.** The official Postgres
entrypoint starts a temporary local-socket-only server to run initdb and init
scripts. A healthcheck using the Unix socket goes green during that phase, before the
real server is listening on TCP. Forcing `-h 127.0.0.1` gates on the TCP listener,
which is what the load generator will actually use. This is a well-known trap and the
harness must encode it rather than leaving it to each adapter author.

**Finding E3.2 — `docker compose up --wait` is convenient but not a measurement.**
It reported **13.60 s** to healthy for Valkey, an engine that starts in well under a
second. The figure bundles image pull, container create, start, and Compose's own
polling granularity. Compose's `--wait`/`--wait-timeout` is a fine *gate*; it is
useless as a *metric*. The harness must time pull, create, start, and
first-healthy as separate phases.

**Finding E3.3 — Compose v2 rejects mixing resource shorthands.** Setting top-level
`pids_limit: 512` alongside a `deploy.resources` block failed the whole project:

```
services.valkey: can't set distinct values on 'pids_limit' and
'deploy.resources.limits.pids': invalid compose project
```

Moving it to `deploy.resources.limits.pids` fixed it. Pick one style — `deploy.resources`
— and use it for everything.

**Finding E3.4 — `deploy.resources.limits` DOES apply outside Swarm in Compose v2.**
Worth stating because the opposite was true in the Compose v1 / Swarm era and the
folklore persists. Verified: the Compose file above produced
`HostConfig.NanoCpus=2000000000`, `HostConfig.Memory=2147483648`,
`HostConfig.PidsLimit=512`, `HostConfig.CpusetCpus=0-1`, and the matching cgroup v2
values inside the container. Note `cpuset` has **no `deploy.resources` equivalent** and
must be set as the top-level `cpuset:` key — the one permitted exception to E3.3.

**Finding E3.5 — teardown is idempotent, both ways.** `docker rm -f` on an
already-removed container exits 0; `docker compose down -v --remove-orphans` run twice
exits 0 both times (measured 0.216 s and 0.341 s). Idempotent cleanup is cheap and
needs no defensive scripting — but note `down -v` destroys named volumes, which is
what we want between cells and catastrophic if pointed at the wrong project. Always
set an explicit Compose `name:`.

---

## E4 — Storage backend: the result that constrains the whole design

Identical Postgres 18.6, identical config (`shared_buffers=256MB`, `fsync=on`,
`synchronous_commit=on`, `full_page_writes=on`), identical cgroup limits
(`--cpus=2 --cpuset-cpus=0-1 --memory=2g`), scale-20 pgbench (~320 MB). Only the
mount for `PGDATA` changed.

| PGDATA on | init time | write TPS (3×15 s) | read-only TPS (3×10 s) | `docker stats` BlockIO write |
|---|---|---|---|---|
| **Named volume** (ext4 in the VM) | **1.02 s** | 13737 / 12854 / 12741 | 102426 / 103956 / 101222 | **6.44 GB** |
| **Bind mount** (VirtioFS → APFS) | **2.98 s** | 11606 / 11621 / 11638 | 56784 / **83752** / **76655** | **8.19 kB** |
| **tmpfs** (RAM) | 0.92 s | **8597** / 12359 / 14095 | 87226 / 87854 / 92525 | 8.19 kB |

**Finding E4.1 — the bind mount's problem is variance, not slowness.** On write
throughput it costs only ~10%. But look at the read-only column, which should be pure
page-cache work and storage-insensitive: the named volume gives 101.2k–104.0k
(coefficient of variation ≈ **1.3%**) while the bind mount gives 56.8k–83.8k (CV ≈
**18%**). A constant handicap would cancel out in an A-vs-B comparison. A 14×
inflation of the noise floor destroys the harness's ability to *resolve* a difference
between two engines at all. **Bind-mounted data directories must be rejected by the
harness, not merely discouraged.**

**Finding E4.2 — tmpfs is not a valid "fast storage" substitute.** It was the *worst*
and most erratic on write-heavy load (8597 → 14095, a 1.64× spread). The reason is in
the stats line: `Mem=1.134GiB / 2GiB`. The tmpfs pages count against the container's
own memory limit, so the database's data files evict its buffer pool. tmpfs quietly
converts a storage-parity experiment into a memory-parity experiment.

**Finding E4.3 — `docker stats` BlockIO is blind to non-block backends.** The named
volume reported 6.44 GB written; the bind mount and tmpfs reported **8.19 kB** for the
same workload. This is not a small discrepancy, it is a total failure of the metric.
Write amplification — a genuinely decision-relevant metric, here ≈ 20× against a
320 MB dataset — is only observable on block-backed storage. Any harness that reads
BlockIO without checking the mount type will silently report zero I/O and nobody will
notice.

**Finding E4.4 — the filesystem is identifiable at runtime, so this can be enforced.**
`stat -f -c "%T"` returned `ext2/ext3` for the named volume, `UNKNOWN` for VirtioFS,
`tmpfs` for tmpfs; `df` showed the device as `/dev/vda1`, `virtiofs0`, `tmpfs`
respectively. The harness can and should assert the backing store before accepting a
run as valid.

---

## E5 — Load generation, warmup, and what the noise floor really is

**Finding E5.1 — pgbench's `--rate` gives genuine coordinated-omission correction.**
Open-loop at `-R 800`, three repeats:

```
rep1  tps=804.68  lat avg 1.313 ms  stddev 0.696  schedule lag avg 0.297 max 13.194 ms
rep2  tps=801.37  lat avg 1.405 ms  stddev 1.874  schedule lag avg 0.337 max 50.943 ms
rep3  tps=800.67  lat avg 1.312 ms  stddev 0.698  schedule lag avg 0.296 max 17.193 ms
```

The **schedule lag** line is the correction: latency is measured from the time a
transaction *should* have started, so a stalled server inflates it. `--latency-limit`
additionally reports late and skipped transactions (0 in all three runs). Note rep2's
max lag of 50.9 ms against 13.2 ms and 17.2 ms — a ~4× tail excursion with no change
in configuration. That is the noisy neighbour and the macOS scheduler, and it is
exactly what an unrepeated single run would have reported as fact.

**Finding E5.2 — closed-loop and open-loop measure different things, by ~7.7×.** The
same container, same dataset: unthrottled closed-loop warmup gave **6193 tps @ 1.292 ms
average**, while open-loop at 800 tps gave **1.313 ms average**. The latencies are
nearly identical while throughput differs 7.7×, which is the tell: closed-loop latency
is a function of how many clients you happened to configure, not of the system.
Reporting closed-loop latency as "the latency" is the most common benchmark error and
the harness must default to rate-controlled open-loop for any latency SLO claim.

**Finding E5.3 — the dev-machine noise floor is large and asymmetric.** Valkey 9.1.2,
`valkey-benchmark --csv`, identical invocations back-to-back:

```
        SET rps      GET rps    SET p99   GET p99   GET max
rep1    292397.66    181818.17   0.063     0.087     3.175 ms
rep2    160771.70    145348.83   0.087     0.127     8.079 ms
rep3    156250.00    155279.50   0.103     0.103     2.655 ms
```

A **1.87× spread** on SET throughput across three consecutive repeats, monotonically
decreasing after the warmup — consistent with thermal/scheduler effects on a laptop,
not with anything about Valkey. Meanwhile Postgres on a named volume held CV ≈ 1.3%.
**The noise floor is engine- and workload-dependent and must be measured per cell, not
assumed.** A harness that reports a point estimate from n=1 on a machine like this is
generating fiction. This single table is the strongest argument in the whole project
for mandatory repeats and interval reporting.

**Finding E5.4 — durability defaults make raw cross-engine numbers meaningless.**
Postgres wrote **6.44 GB** to disk during the run. Valkey, started with `--save ""
--appendonly no`, wrote **0 B** (`BlockIO=0B / 0B`) — it has no durability at all.
Comparing 150k Valkey ops/s against 13k Postgres tps without stating that is not a
comparison, it is a category error. The workload spec must carry a **durability
requirement**, and the harness must configure every engine to meet it (or record,
loudly, that an engine cannot).

**Finding E5.5 — a self-inflicted lesson worth recording.** My first ClickHouse load
probe was `SELECT count(), sum(number), max(number) FROM numbers_mt(20000000)`, which
returned in 0.006–0.008 s in every configuration. That is not a CPU measurement; the
query was optimised away. It cost nothing here because the probe's real purpose was
the cgroup readback, but it is precisely how a harness silently produces
authoritative-looking nonsense. **Every generated query needs a plausibility check —
e.g. assert rows-examined or wall-time against an expected order of magnitude — or the
harness will confidently report that an engine is infinitely fast.**

**Finding E5.6 — version strings lie; capture must be engine-aware.** Valkey 9.1.2
reports, in the same `INFO server` block:

```
redis_version:7.2.4      <-- compatibility fiction
valkey_version:9.1.2     <-- the truth
```

A generic "read `redis_version`" capture would stamp the audit manifest with a version
that does not exist in this container. Every adapter must declare *which* field is
authoritative, and the manifest should record the raw response so the error is
recoverable after the fact.

**Finding E5.7 — config readback needs unit-aware parsing.** `pg_settings` returns
`shared_buffers` as `setting=65536` with `unit='8kB'`. Naive concatenation produced the
nonsense string `655368kB` in my first pass. The correct read is 65536 × 8 kB =
512 MB, which matches what was requested. Use `SHOW`/`pg_size_bytes()` or multiply by
the unit; never string-concatenate. Also note `max_wal_size=1024MB` came from
`source='configuration file'` — the *image's* default, not anything I set. The
`source` column is exactly the provenance signal the manifest needs: it distinguishes
"we chose this" from "the image chose this for us".

---

## E6 — Timings for run-matrix budgeting

| Operation | Measured |
|---|---|
| `docker pull` postgres:18.6-alpine (arm64, cold) | **21.5 s** |
| `docker pull` clickhouse-server:25.8-alpine (187 MB, cold) | **23.7 s** |
| Postgres start → healthy | **5.5 s** |
| ClickHouse start → healthy | **5.4 s** (consistent across 3 starts) |
| Valkey start → healthy | < 1 s (Compose `--wait` reported 13.6 s including pull) |
| pgbench `-i -s 10` (1 M rows) | ~0.6 s |
| pgbench `-i -s 20` named volume / bind mount | 1.02 s / 2.98 s |
| `docker rm -f` | 0.216 s |
| `docker compose down -v` | 0.341 s |

Container lifecycle overhead is ~6–30 s per cell. With warmup + 3 repeats at 15–20 s
each, a single (engine × scenario) cell costs roughly **2 minutes**. A 5-engine ×
4-scenario × 3-repeat matrix is therefore on the order of **1–2 hours** — small enough
to run nightly in CI, too slow for an inner development loop. That argues for a
`--smoke` mode (1 repeat, 5 s runs) used for pipeline validation and a `--full` mode
whose results are the only ones allowed into a report.

---

## What this means for the design

1. **Named volumes only** for data directories. Reject bind mounts (E4.1) and tmpfs
   (E4.2) with an explicit error, and assert the backing filesystem at runtime (E4.4).
2. **Set affinity and quota and explicit engine knobs, then read them back** (E2).
3. **Time each lifecycle phase separately**; never use `compose up --wait` as a
   measurement (E3.2).
4. **Open-loop, rate-controlled load is the default** for any latency claim (E5.2).
5. **Repeats are mandatory and the observed spread is part of the result** (E5.3).
6. **Durability must be equalised from the spec, not left to image defaults** (E5.4).
7. **The macOS dev machine is for pipeline validation, not for results.** Its
   ~18% CV on a storage-insensitive read workload over VirtioFS, its 1.87× Valkey
   spread, and its blind BlockIO accounting are each individually disqualifying. Linux
   CI on a dedicated runner is the only place reportable numbers may be produced, and
   the harness should refuse to emit a signed report from a virtualised-FS host.

---

## E7 — An engine that simply will not run on the dev machine

`mongo:8` (`sha256:5211c51171f57ae60842b11664bb244628971b3d35325762a97888337b9bb0db`)
started and immediately exited 1:

```
{"s":"F","c":"CONTROL","id":12257600,"ctx":"main",
 "msg":"MongoDB cannot start: Linux kernel versions 6.19 and newer has a known
        incompatibility with this version of MongoDB.
        See https://jira.mongodb.org/browse/SERVER-121912"}
```

Docker Desktop's VM runs kernel **7.0.12-linuxkit**, so MongoDB 8.x is *fatally
incompatible with every macOS Docker Desktop dev machine* while remaining fine on the
older kernels typical of Linux CI runners. Control: `mongo:7`
(`sha256:b6421fd6…`) started and reached `healthy`, reporting `version=7.0.40`.

**Finding E7.1.** "Engine cannot run in this environment" must be a **first-class
outcome** of the adapter contract, not a crash. The gate phase must be able to fail on
*environment* as well as on capability, and the run manifest must record the
environment-specific exclusion so a reader understands why a candidate is missing.

**Finding E7.2.** This is the sharpest possible illustration of why the environment
capture is not bureaucratic overhead. A dev-machine run of this spec silently drops
MongoDB 8; a CI run includes it. Two bundles, same spec hash, different candidate sets.
Without the kernel version in the manifest, the difference is inexplicable.

**Finding E7.3 — the dev/CI split is not merely a fidelity question, it is a coverage
question.** Phase 1 assumed macOS was unsuitable for *results*. It is also unsuitable
for *completeness*. The harness must refuse to emit a ranked recommendation when any
candidate was excluded for environmental reasons.

---

## E8 — "How many CPUs do I have?" has three different answers at once

Same container, `--cpus=2 --cpuset-cpus=0-1 --memory=2g`:

| How a program asks | Answer | Correct? |
|---|---|---|
| `nproc` (→ `sched_getaffinity`) | **2** | ✔ |
| `getconf _NPROCESSORS_ONLN` | **2** | ✔ |
| `grep -c ^processor /proc/cpuinfo` | **10** | ✘ |
| `/sys/devices/system/cpu/online` | **0-9** (=10) | ✘ |
| cgroup `cpu.max` / `cpuset.cpus.effective` | `200000 100000` / `0-1` | ✔ |

| How a program asks about memory | Answer | Correct? |
|---|---|---|
| `/proc/meminfo` `MemTotal` | **8124516 kB** (7.75 GiB — the whole VM) | ✘ |
| cgroup `memory.max` | **2147483648** (2 GiB) | ✔ |

**Finding E8.1 — this is the root cause behind E2, and it is worse than E2 suggested.**
Phase 3's earlier conclusion was "use `--cpuset-cpus` and the engine will see the right
number". That is only true for programs that ask via `sched_getaffinity`. A program
reading `/proc/cpuinfo` sees **10 cores even with affinity pinning**, and a program
reading `/proc/meminfo` sees the whole VM's RAM even with a hard memory limit. **There
is no set of Docker flags that makes every detection method agree.**

Both behaviours were observed in real engines during Phase 3:

| Engine | Reads | Reported under `--cpuset-cpus=0-1 --memory=2g` |
|---|---|---|
| ClickHouse 25.8.33.6 | cgroup | `max_threads='auto(2)'`, `CGroupMemoryTotal=3221225472` ✔ |
| MongoDB 7.0.40 | `/proc/cpuinfo` for CPU, cgroup for memory | `hostInfo().system.numCores=10` ✘, `memSizeMB=7934` ✘, but WiredTiger cache **536870912** (= 50% of (2 GiB − 1 GiB)) ✔ |

MongoDB is the instructive case: it gets **memory right and CPU wrong, in the same
process**. Neither "modern engines handle cgroups" nor "engines ignore cgroups" is
true; the behaviour is per-subsystem, per-engine, per-version.

**Rule, restated and hardened.** Setting affinity and quota is necessary but *not
sufficient*. Every engine's parallelism and memory knobs must be set **explicitly from
the resource envelope**, and the adapter must **read back what the engine believes it
has** and record it. Where the engine's belief disagrees with the envelope, the harness
must either correct it or mark the cell as non-comparable. Auto-detection is never
trusted for anything.

---

## E9 — Other environment traps confirmed on this host

**Storage advertises itself as rotational.** Both virtual disks report
`rotational=1` despite sitting on Apple NVMe:

```
/sys/block/vda rotational=1 scheduler=none [mq-deadline] kyber
/sys/block/vdb rotational=1 scheduler=none [mq-deadline] kyber
```

The kernel and several engines tune read-ahead and I/O concurrency from this flag
(PostgreSQL's `effective_io_concurrency` guidance differs sharply for rotational vs
SSD). Engines are therefore mis-tuned on macOS **unequally**, which is why relative
numbers do not survive the platform change either — the distortion is not a common
multiplier.

**No BFQ scheduler.** The available schedulers are `none`, `mq-deadline`, `kyber`.
This is the mechanical reason `--blkio-weight` hard-errors and `--blkio-weight-device`
is accepted but does nothing: runc writes `io.bfq.weight` when present and otherwise
skips silently. **I/O *weighting* is unavailable; I/O *throttling* via
`--device-read-bps` / `--device-write-bps` works** because it writes `io.max`. The
harness should use throttling if it needs I/O parity and must never depend on weights.

**`deploy.resources.reservations.cpus` is silently ignored** outside Swarm. exp02's
Compose file requested `reservations: {cpus: "1.0", memory: 1G}`; the resulting
`HostConfig` carried the memory reservation but no CPU reservation. Reservations are
soft anyway and have no place in a parity design — but a harness author who believes
they applied has a silent hole in their fairness argument.
