#!/usr/bin/env bash
# exp01-postgres.sh — verify the full provision -> health-gate -> load -> collect -> teardown
# loop against a digest-pinned Postgres container. Records exact timings.
set -uo pipefail

PG_INDEX_DIGEST="sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2"
PG_REF="postgres@${PG_INDEX_DIGEST}"
CNAME="dsa-exp01-pg"
CPUS="2.0"
MEM="2g"

ts() { python3 -c 'import time;print(f"{time.time():.3f}")'; }
el() { python3 -c "print(f'{float('$2')-float('$1'):.3f}s')"; }

cleanup() { docker rm -f "$CNAME" >/dev/null 2>&1; docker volume rm -f dsa-exp01-pgdata >/dev/null 2>&1; }
trap cleanup EXIT INT TERM
cleanup

echo "=== [1] PULL by index digest ==="
T0=$(ts)
docker pull "$PG_REF" 2>&1 | tail -3
T1=$(ts); echo "pull_elapsed=$(el $T0 $T1)"

echo; echo "=== [2] Resolved local identity ==="
docker image inspect "$PG_REF" --format 'Id={{.Id}}
RepoDigests={{json .RepoDigests}}
Arch={{.Architecture}}/{{.Os}}
Created={{.Created}}'

echo; echo "=== [3] START with resource limits + HEALTHCHECK ==="
docker volume create dsa-exp01-pgdata >/dev/null
T2=$(ts)
docker run -d --name "$CNAME" \
  --cpus="$CPUS" --memory="$MEM" --memory-swap="$MEM" --pids-limit=512 \
  -e POSTGRES_PASSWORD=bench -e POSTGRES_DB=bench -e POSTGRES_USER=bench \
  -e PGDATA=/var/lib/postgresql/data/pgdata \
  -v dsa-exp01-pgdata:/var/lib/postgresql/data \
  --health-cmd='pg_isready -U bench -d bench -h 127.0.0.1' \
  --health-interval=1s --health-timeout=3s --health-retries=60 --health-start-period=1s \
  -p 55432:5432 \
  "$PG_REF" \
  -c shared_buffers=512MB -c max_connections=200 -c fsync=on -c synchronous_commit=on \
  >/dev/null
echo "container started"

echo; echo "=== [4] HEALTH GATE (poll .State.Health.Status) ==="
DEADLINE=$(( $(date +%s) + 120 )); STATUS=""
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  STATUS=$(docker inspect -f '{{.State.Health.Status}}' "$CNAME" 2>/dev/null)
  [ "$STATUS" = "healthy" ] && break
  [ "$STATUS" = "unhealthy" ] && break
  sleep 0.2
done
T3=$(ts)
echo "final_health=$STATUS"
echo "start_to_healthy=$(el $T2 $T3)"
docker inspect -f '{{json .State.Health}}' "$CNAME" | python3 -m json.tool | head -25

echo; echo "=== [5] DID THE CGROUP LIMITS ACTUALLY APPLY? (macOS Docker Desktop check) ==="
docker exec "$CNAME" sh -c '
  echo "nproc_visible=$(nproc)"
  echo "cpu.max=$(cat /sys/fs/cgroup/cpu.max 2>/dev/null)"
  echo "memory.max=$(cat /sys/fs/cgroup/memory.max 2>/dev/null)"
  echo "memory.swap.max=$(cat /sys/fs/cgroup/memory.swap.max 2>/dev/null)"
  echo "pids.max=$(cat /sys/fs/cgroup/pids.max 2>/dev/null)"
  echo "cgroup_version=$(test -f /sys/fs/cgroup/cgroup.controllers && echo v2 || echo v1)"
  echo "meminfo_MemTotal=$(grep MemTotal /proc/meminfo)"
'

echo; echo "=== [6] ENGINE VERSION + APPLIED CONFIG READ BACK FROM RUNNING SERVER ==="
docker exec "$CNAME" psql -U bench -d bench -tAc "select version();"
docker exec "$CNAME" psql -U bench -d bench -tAc \
  "select name||'='||setting||coalesce(unit,'')||' [src='||source||']' from pg_settings where name in ('shared_buffers','max_connections','fsync','synchronous_commit','wal_level','checkpoint_timeout','max_wal_size','server_version') order by name;"

echo; echo "=== [7] LOAD: pgbench init (scale 10 ~ 1M rows) then a timed run ==="
T4=$(ts)
docker exec "$CNAME" pgbench -i -s 10 -U bench -d bench 2>&1 | tail -6
T5=$(ts); echo "load_elapsed=$(el $T4 $T5)"

docker exec "$CNAME" psql -U bench -d bench -tAc \
  "select relname||' rows='||n_live_tup from pg_stat_user_tables order by relname;"
docker exec "$CNAME" psql -U bench -d bench -tAc \
  "select pg_size_pretty(pg_database_size('bench')) as dbsize;"

echo; echo "=== [8] WARMUP (discarded) then 3 MEASURED REPEATS ==="
echo "--- warmup 10s (result discarded)"
docker exec "$CNAME" pgbench -c 8 -j 2 -T 10 -U bench -d bench 2>&1 | grep -E 'tps|latency' || true
for r in 1 2 3; do
  echo "--- repeat $r : 20s, 8 clients, 2 threads, rate-limited 800tps (-R) to expose coordinated omission handling"
  docker exec "$CNAME" pgbench -c 8 -j 2 -T 20 -R 800 --latency-limit=200 -U bench -d bench 2>&1 \
    | grep -E 'tps|latency|lag|skipped|number of transactions actually' || true
done

echo; echo "=== [9] METRIC COLLECTION: docker stats one-shot ==="
docker stats --no-stream --format '{{json .}}' "$CNAME"

echo; echo "=== [10] TEARDOWN (idempotent — run twice) ==="
T6=$(ts)
docker rm -f "$CNAME" >/dev/null 2>&1 && echo "rm pass1 ok"
docker rm -f "$CNAME" >/dev/null 2>&1 && echo "rm pass2 ok" || echo "rm pass2 no-op (expected: already gone)"
docker volume rm -f dsa-exp01-pgdata >/dev/null 2>&1 && echo "volume rm ok"
T7=$(ts); echo "teardown_elapsed=$(el $T6 $T7)"
trap - EXIT
echo; echo "=== DONE ==="
