"""The `metrics.ndjson` record schema (PLAN.md S6).

Every emitter in the build depends on this, which is why it is built early. The
research names `metrics.ndjson` as a bundle artifact and hashes it, but never
defines its record schema; defining it is a primary deliverable.

Design rules:

* **One line, one record, one kind.** A discriminated union on `kind` so a
  reader can dispatch without guessing, and an unknown kind is an error rather
  than a silently ignored line.
* **Every record carries `(t_ms, w, seq)`.** `t_ms` is the sample time, `w` the
  writer shard, `seq` that writer's monotonic counter. The triple is a total
  order, which is what makes a merge of shuffled shards deterministic (S6's
  acceptance criterion) -- wall-clock alone is not, because two writers can
  stamp the same millisecond.
* **Latency here is for watching, never for reporting.** `latency_window`
  carries within-window estimates so the TUI and Grafana have something to draw.
  The authoritative record is the HdrHistogram `.hlog`, and a scrape bucket must
  never become a reported percentile.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

SCHEMA_VERSION = 1


class Record(BaseModel):
    """Fields every record carries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    t_ms: int = Field(description="Sample time, milliseconds since the Unix epoch.")
    w: str = Field(description="Writer shard id. Unique per process or thread.")
    seq: int = Field(ge=0, description="Per-writer monotonic sequence number.")
    cell: str | None = Field(
        default=None, description="Run-matrix cell this record belongs to."
    )


class PhaseRecord(Record):
    """A lifecycle boundary: provision, init, load, warmup, measure, collect, teardown."""

    kind: Literal["phase"] = "phase"
    phase: Literal[
        "gate", "provision", "init", "load", "warmup", "measure", "collect", "teardown"
    ]
    event: Literal["begin", "end"]
    ok: bool = True
    detail: str | None = None


class LatencyWindowRecord(Record):
    """Within-window latency estimate. For watching only, never for reporting."""

    kind: Literal["latency_window"] = "latency_window"
    window_ms: int
    op: str
    count: int
    errors: int = 0
    rate_per_s: float
    p50_us: float | None = None
    p90_us: float | None = None
    p99_us: float | None = None
    max_us: float | None = None
    # Stamped so a consumer cannot mistake this for the authoritative figure.
    estimate_only: Literal[True] = True


class ContainerRecord(Record):
    """`docker stats` and cgroup readings for one container."""

    kind: Literal["container"] = "container"
    container: str
    cpu_usage_usec: int | None = None
    cpu_throttled_usec: int | None = None
    cpu_nr_throttled: int | None = None
    memory_current: int | None = None
    memory_max: int | None = None
    memory_events_oom: int | None = None
    memory_events_oom_kill: int | None = None
    pids_current: int | None = None
    blkio_read_bytes: int | None = None
    blkio_write_bytes: int | None = None
    # exp04: BlockIO is unreliable on bind mounts. Carried, never assumed.
    blkio_trusted: bool = True


class EngineRecord(Record):
    """Engine-wide internals: pg_stat_*, INFO, serverStatus, system.*."""

    kind: Literal["engine"] = "engine"
    engine: str
    sample_class: Literal["light", "heavy"] = "light"
    metrics: dict[str, float | int | str | None] = Field(default_factory=dict)


class BackendRecord(Record):
    """One backend/connection. The per-connection view the cliff work needs."""

    kind: Literal["backend"] = "backend"
    engine: str
    backend_id: str
    state: str | None = None
    wait_event_type: str | None = None
    wait_event: str | None = None
    vm_rss_bytes: int | None = None
    age_s: float | None = None
    query_start_age_s: float | None = None


class PoolRecord(Record):
    """Client-side connection pool state."""

    kind: Literal["pool"] = "pool"
    pool: str
    size: int
    in_use: int
    idle: int
    waiting: int
    acquire_wait_us_p99: float | None = None
    timeouts: int = 0


class AppRecord(Record):
    """App-tier span timings and saturation."""

    kind: Literal["app"] = "app"
    endpoint: str
    count: int
    errors: int = 0
    app_recv_to_db_start_us: float | None = None
    db_us: float | None = None
    db_end_to_send_us: float | None = None
    cpu_pct: float | None = None


class NetRecord(Record):
    """Socket and ephemeral-port state, for connection-lifecycle attribution."""

    kind: Literal["net"] = "net"
    scope: Literal["engine", "driver", "app"]
    established: int | None = None
    time_wait: int | None = None
    syn_recv: int | None = None
    listen_overflows: int | None = None
    listen_drops: int | None = None
    ephemeral_used: int | None = None
    ephemeral_available: int | None = None


class ValidityRecord(Record):
    """A validity gate firing. Invalidate rather than report with a caveat."""

    kind: Literal["validity"] = "validity"
    gate: str
    verdict: Literal["OK", "FLAG", "INVALID", "INCONCLUSIVE_DRIVER_BOUND"]
    observed: float | str | None = None
    limit: float | str | None = None
    detail: str | None = None


AnyRecord = Annotated[
    PhaseRecord
    | LatencyWindowRecord
    | ContainerRecord
    | EngineRecord
    | BackendRecord
    | PoolRecord
    | AppRecord
    | NetRecord
    | ValidityRecord,
    Field(discriminator="kind"),
]

RECORD_ADAPTER: TypeAdapter[AnyRecord] = TypeAdapter(AnyRecord)

RECORD_KINDS: tuple[str, ...] = (
    "phase",
    "latency_window",
    "container",
    "engine",
    "backend",
    "pool",
    "app",
    "net",
    "validity",
)


def json_schema() -> dict[str, object]:
    """The committed JSON Schema. CI fails if this drifts from the models."""
    schema = RECORD_ADAPTER.json_schema(mode="serialization")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "dsel metrics.ndjson record"
    schema["x-schema-version"] = SCHEMA_VERSION
    return schema
