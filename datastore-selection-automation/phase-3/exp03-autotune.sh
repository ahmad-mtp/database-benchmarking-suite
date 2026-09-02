#!/usr/bin/env bash
# exp03 — THE fair-comparison question: does an engine that self-tunes from host
# resources respect a cgroup CPU *quota* (--cpus) or does it need *affinity* (--cpuset-cpus)?
# ClickHouse sets max_threads from detected core count; this is the cleanest probe.
set -uo pipefail
CH_TAG="clickhouse/clickhouse-server:25.8-alpine"
echo "=== resolve index digest for $CH_TAG ==="
docker buildx imagetools inspect "$CH_TAG" 2>&1 | head -4
IDX=$(docker buildx imagetools inspect "$CH_TAG" --format '{{.Manifest.Digest}}' 2>/dev/null)
echo "index_digest=$IDX"
CH="clickhouse/clickhouse-server@${IDX}"

S=$(date +%s.%N); docker pull -q "$CH" >/dev/null 2>&1; E=$(date +%s.%N)
awk -v a=$S -v b=$E 'BEGIN{printf "pull_elapsed=%.1fs\n", b-a}'
docker image inspect "$CH" --format 'size_bytes={{.Size}} arch={{.Architecture}}'

run_case () {
  NAME=$1; shift
  echo; echo "############ CASE: $NAME ############"
  echo "docker run flags: $*"
  docker rm -f dsa-exp03 >/dev/null 2>&1
  S=$(date +%s.%N)
  docker run -d --name dsa-exp03 "$@" \
    -e CLICKHOUSE_PASSWORD=bench -e CLICKHOUSE_USER=bench -e CLICKHOUSE_DB=bench \
    --health-cmd='clickhouse-client --user bench --password bench -q "SELECT 1"' \
    --health-interval=1s --health-timeout=3s --health-retries=90 --health-start-period=1s \
    "$CH" >/dev/null
  DEADLINE=$(( $(date +%s) + 150 )); ST=""
  while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    ST=$(docker inspect -f '{{.State.Health.Status}}' dsa-exp03 2>/dev/null)
    [ "$ST" = "healthy" -o "$ST" = "unhealthy" ] && break; sleep 0.25
  done
  E=$(date +%s.%N)
  awk -v a=$S -v b=$E -v s="$ST" 'BEGIN{printf "health=%s start_to_healthy=%.3fs\n", s, b-a}'
  [ "$ST" != "healthy" ] && { docker logs --tail 20 dsa-exp03; return; }
  docker exec dsa-exp03 sh -c 'echo "  nproc=$(nproc)  cpu.max=$(cat /sys/fs/cgroup/cpu.max)  cpuset.eff=$(cat /sys/fs/cgroup/cpuset.cpus.effective 2>/dev/null)  memory.max=$(cat /sys/fs/cgroup/memory.max)"'
  echo "  --- what ClickHouse thinks it has:"
  docker exec dsa-exp03 clickhouse-client --user bench --password bench -q "
    SELECT name, value FROM system.settings WHERE name IN ('max_threads','max_insert_threads','max_final_threads') ORDER BY name FORMAT TSV;"
  docker exec dsa-exp03 clickhouse-client --user bench --password bench -q "
    SELECT 'server_version', version()
    UNION ALL SELECT 'cpu_cores_detected', toString(getSetting('max_threads'))
    UNION ALL SELECT 'OSMemoryTotal_GiB', toString(round(value/1024/1024/1024,2)) FROM system.asynchronous_metrics WHERE metric='OSMemoryTotal'
    FORMAT TSV;" 2>&1 | head -5
  docker exec dsa-exp03 clickhouse-client --user bench --password bench -q "
    SELECT metric, value FROM system.asynchronous_metrics
    WHERE metric IN ('CGroupMaxCPU','CGroupMemoryTotal','OSMemoryTotal','NumberOfPhysicalCPUCores') ORDER BY metric FORMAT TSV;" 2>&1 | head -8
  echo "  --- trivial load: 20M-row aggregation, timed 3x"
  for r in 1 2 3; do
    docker exec dsa-exp03 clickhouse-client --user bench --password bench --time -q \
      "SELECT count(), sum(number), max(number) FROM numbers_mt(20000000) FORMAT Null" 2>&1 | tr '\n' ' '
    echo "s (repeat $r)"
  done
  docker rm -f dsa-exp03 >/dev/null 2>&1
}

run_case "A: --cpus=2 quota ONLY (no affinity)"  --cpus=2.0 --memory=3g --memory-swap=3g
run_case "B: --cpuset-cpus=0-1 affinity ONLY"    --cpuset-cpus=0-1 --memory=3g --memory-swap=3g
run_case "C: BOTH quota + affinity"              --cpus=2.0 --cpuset-cpus=0-1 --memory=3g --memory-swap=3g

docker rm -f dsa-exp03 >/dev/null 2>&1
echo; echo "=== DONE ==="
