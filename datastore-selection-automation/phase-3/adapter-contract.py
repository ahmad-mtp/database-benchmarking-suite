#!/usr/bin/env python3
"""
The per-engine adapter contract.

Six phases, each independently timed, each emitting evidence into the run bundle.
Every method is allowed to fail; failure is a RESULT, not an exception to swallow —
"MongoDB could not satisfy the durability requirement" is a finding, not a crash.

Design constraints this encodes, all derived from Phase-3 measurements:
  * provision() pins by INDEX digest and records the resolved PLATFORM digest  (E1.1)
  * provision() sets affinity AND quota AND swap-off                            (E2.1)
  * provision() refuses non-named-volume storage                                (E4.1/E4.2)
  * init() sets engine knobs EXPLICITLY, never relying on auto-detection        (E2.2/E2.3)
  * init() reads the config BACK and returns it as evidence                     (E2, E5.6, E5.7)
  * load() is deterministic from (seed, table, row_id, column)                   (exp06)
  * run() is open-loop and rate-controlled                                       (E5.2)
  * collect() records which metrics are TRUSTWORTHY on this storage backend      (E4.3)
  * teardown() is idempotent and runs even on crash/SIGINT                       (E3.5)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Protocol, Literal
from pathlib import Path


# ----------------------------------------------------------------- value types
@dataclass(frozen=True)
class ImagePin:
    repo: str                      # "postgres"
    index_digest: str              # sha256:... — portable across arm64/amd64. PINNED IN SPEC.
    platform_digest: str | None = None   # resolved at run time, RECORDED in manifest
    spdx: str = ""                 # re-verified per run; a change is a loud failure
    tag_hint: str = ""             # human breadcrumb only, never used for resolution


@dataclass(frozen=True)
class ResourceEnvelope:
    cpuset: str                    # "2-5" — affinity is REQUIRED, not optional
    cpu_quota: float               # 4.0
    memory_bytes: int
    pids_limit: int = 4096
    storage: Literal["named_volume"] = "named_volume"   # only legal value


@dataclass
class Evidence:
    """Everything an auditor needs to believe a phase happened as claimed."""
    phase: str
    started_at: str
    duration_s: float
    ok: bool
    commands: list[str] = field(default_factory=list)   # verbatim, re-runnable
    readback: dict = field(default_factory=dict)        # what the ENGINE says, not what we asked
    artifacts: dict[str, str] = field(default_factory=dict)  # filename -> sha256
    error: str | None = None


@dataclass
class GateResult:
    passed: bool
    failures: list[str]            # human-readable, e.g. "no multi_document transactions"


# ------------------------------------------------------------------- the contract
class EngineAdapter(Protocol):
    engine_id: str                 # "postgres"
    family: str                    # "relational"

    # ---- phase 0: GATE. Pure function, no I/O, runs before anything starts. ----
    def gate(self, requirements: dict) -> GateResult:
        """Declare capability against the spec's hard requirements.

        Runs BEFORE provisioning: benchmarking an engine that cannot meet a hard
        requirement wastes compute and produces a seductive number that invites
        someone to relax the requirement. Answers from a static capability table
        keyed by engine VERSION — capabilities change between majors.
        """
        ...

    # ---- phase 1: PROVISION ----
    def provision(self, pin: ImagePin, env: ResourceEnvelope) -> Evidence:
        """Pull by index digest, start the container, gate on health.

        MUST: resolve and record the platform digest; set --cpuset-cpus AND --cpus
        AND --memory with --memory-swap equal to it; create a NAMED VOLUME for the
        data directory and assert the backing filesystem after start; time pull,
        create, start and first-healthy as SEPARATE phases (compose --wait bundles
        them and is not a measurement).
        """
        ...

    # ---- phase 2: INIT ----
    def init(self, spec: dict) -> Evidence:
        """Apply schema/DDL, indexes, and the normalised engine configuration.

        MUST set explicitly, never by auto-detection: parallelism/thread count,
        cache/buffer size, and the durability setting demanded by
        spec.requirements.durability. MUST then READ EVERY ONE BACK from the running
        engine and return it in Evidence.readback. Configured != active (Valkey
        reported io_threads_active:0 with io-threads=2). Version strings must be read
        from the engine-authoritative field (Valkey reports redis_version:7.2.4 AND
        valkey_version:9.1.2 — only the latter is true).
        """
        ...

    # ---- phase 3: LOAD ----
    def load(self, dataset: "DatasetHandle") -> Evidence:
        """Bulk-load the deterministic dataset, then bring the engine to steady state.

        The dataset is generated ONCE per (spec, seed) and reused across all engines,
        so every candidate sees byte-identical input. After loading, the adapter MUST
        settle the engine — ANALYZE/VACUUM for Postgres, OPTIMIZE FINAL for ClickHouse,
        compaction drain for LSM engines — and record how long that took. An engine
        benchmarked with compaction debt outstanding is being measured mid-stride.
        Returns the loaded row counts and on-disk size as readback evidence.
        """
        ...

    # ---- phase 4: RUN ----
    def run(self, pattern: dict, target_rps: float, duration_s: float,
            warmup_s: float, seed: str) -> Evidence:
        """Execute ONE access pattern at ONE offered rate, open-loop.

        MUST be rate-controlled with latency measured from the SCHEDULED start time,
        not the actual start (coordinated omission). MUST emit a full latency
        histogram, not summary statistics — a third party recomputes percentiles
        rather than trusting ours, and percentiles cannot be averaged across repeats.
        MUST record load-generator CPU; a run where the driver exceeded 70% is marked
        INVALID rather than reported.
        """
        ...

    # ---- phase 5: COLLECT ----
    def collect(self) -> Evidence:
        """Harvest engine-internal counters and container-level resource metrics.

        MUST tag each metric with a trust flag. docker stats BlockIO reported 6.44 GB
        on a named volume and 8.19 kB on a bind mount for the SAME workload — the
        metric is silently meaningless off block-backed storage. A metric the harness
        cannot trust must be omitted or flagged, never reported as a number.
        Engine-internal sources: pg_stat_database/pg_stat_statements, MongoDB
        serverStatus, Valkey INFO latencystats/commandstats, ClickHouse
        system.query_log + system.asynchronous_metrics.
        """
        ...

    # ---- phase 6: TEARDOWN ----
    def teardown(self) -> Evidence:
        """Remove container, volume and network. MUST be idempotent and MUST run on
        crash, SIGINT and SIGTERM. Verified: docker rm -f and compose down -v both
        exit 0 when the target is already gone."""
        ...


# ------------------------------------------------- worked example: PostgreSQL
POSTGRES_CAPABILITIES = {
    "18": {
        "transactions": "multi_document",
        "isolation_max": "serializable",
        "consistency": "linearizable",       # single node
        "durability": ["none", "periodic", "fsync_on_commit"],
        "queries": {"secondary_index_lookup", "range_scan_with_order",
                    "multi_entity_join", "aggregate_group_by", "full_text_search"},
        "schema_evolution": "migration_ok",
        "spdx": "PostgreSQL",
        "osi_approved": True,
    }
}

class PostgresAdapter:
    engine_id, family = "postgres", "relational"

    def __init__(self, version="18", container="dsa-pg"):
        self.version, self.c = version, container

    def gate(self, req) -> GateResult:
        cap, fails = POSTGRES_CAPABILITIES[self.version], []
        order = ["none", "single_document", "multi_document", "multi_shard"]
        if order.index(cap["transactions"]) < order.index(req["transactions"]["scope"]):
            fails.append(f"transaction scope {cap['transactions']} < required {req['transactions']['scope']}")
        for q in req["queries_must_support"]:
            if q not in cap["queries"]:
                fails.append(f"unsupported query capability: {q}")
        if req["durability"]["commit"] not in cap["durability"]:
            fails.append(f"cannot provide durability={req['durability']['commit']}")
        if req["licence_policy"] == "osi_approved_only" and not cap["osi_approved"]:
            fails.append(f"licence {cap['spdx']} is not OSI-approved")
        return GateResult(not fails, fails)

    def _docker_run(self, pin: ImagePin, env: ResourceEnvelope) -> list[str]:
        # Every flag here is load-bearing and was verified in Phase 3.
        return [
            "docker", "run", "-d", "--name", self.c,
            "--cpuset-cpus", env.cpuset,            # E2.1: affinity, so nproc is honest
            "--cpus", str(env.cpu_quota),           # E2.1: quota, so we cannot exceed it
            "--memory", str(env.memory_bytes),
            "--memory-swap", str(env.memory_bytes), # equal => swap disabled
            "--pids-limit", str(env.pids_limit),
            "-v", f"{self.c}-data:/var/lib/postgresql/data",   # E4.1: named volume ONLY
            "-e", "PGDATA=/var/lib/postgresql/data/pgdata",
            # E3.1: -h 127.0.0.1 or the probe goes green during initdb's local-only phase
            "--health-cmd", "pg_isready -U bench -d bench -h 127.0.0.1",
            "--health-interval", "1s", "--health-retries", "60", "--health-start-period", "1s",
            f"{pin.repo}@{pin.index_digest}",
            # E2.2/E2.3: set parallelism and memory EXPLICITLY. Never auto-detect.
            "-c", f"shared_buffers={env.memory_bytes // 4 // 2**20}MB",
            "-c", f"effective_cache_size={env.memory_bytes * 3 // 4 // 2**20}MB",
            "-c", f"max_parallel_workers={int(env.cpu_quota)}",
            "-c", f"max_parallel_workers_per_gather={max(1, int(env.cpu_quota)//2)}",
            "-c", "fsync=on", "-c", "synchronous_commit=on",   # durability from the spec
        ]

    # Readback query. Note pg_settings returns shared_buffers as setting=65536,
    # unit='8kB' — string concatenation yields the nonsense "655368kB" (E5.7).
    # pg_size_bytes(current_setting(...)) is the correct read. The `source` column
    # distinguishes "we chose this" from "the image chose this for us".
    READBACK_SQL = """
      SELECT name,
             CASE WHEN unit IS NULL THEN setting
                  ELSE pg_size_bytes(current_setting(name))::text END AS value,
             unit, source
      FROM pg_settings
      WHERE name IN ('shared_buffers','effective_cache_size','max_parallel_workers',
                     'max_parallel_workers_per_gather','fsync','synchronous_commit',
                     'wal_level','max_wal_size','server_version');
    """
    VERSION_SQL = "SELECT current_setting('server_version') AS authoritative_version;"

    # ... provision/init/load/run/collect/teardown implement the Protocol, each
    # returning Evidence with commands[] verbatim and readback{} from the engine.
