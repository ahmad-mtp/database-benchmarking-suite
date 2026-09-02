#!/usr/bin/env bash
# exp02 — (a) prove the same lifecycle works for a non-relational engine,
#         (b) test `docker compose up --wait` as the health gate,
#         (c) test whether deploy.resources.limits applies in plain Compose v2 (non-Swarm),
#         (d) test --cpuset-cpus for noisy-neighbour isolation.
set -uo pipefail
cd "$(dirname "$0")"
D=exp02-workdir; rm -rf "$D"; mkdir -p "$D"; cd "$D"

VK="valkey/valkey@sha256:a174b894902bd3367e330d47cc2054367dc4917701776aaf336f41d83b65ec7a"

cat > compose.yaml <<YAML
name: dsa-exp02
services:
  valkey:
    image: ${VK}
    command: ["valkey-server","--save","","--appendonly","no","--maxmemory","512mb","--maxmemory-policy","noeviction","--io-threads","2"]
    ports: ["56379:6379"]
    healthcheck:
      test: ["CMD","valkey-cli","ping"]
      interval: 1s
      timeout: 3s
      retries: 60
      start_period: 1s
    deploy:
      resources:
        limits:   { cpus: "2.0", memory: 2G, pids: 512 }
        reservations: { cpus: "1.0", memory: 1G }
    cpuset: "0-1"
YAML

echo "=== compose config (does it validate + what does it resolve to?) ==="
docker compose config 2>&1 | head -40

echo; echo "=== [A] compose up --wait  (health gate delegated to compose) ==="
S=$(date +%s.%N)
docker compose up -d --wait --wait-timeout 120 2>&1 | tail -5
E=$(date +%s.%N)
awk -v a=$S -v b=$E 'BEGIN{printf "compose_up_wait_to_healthy=%.3fs\n", b-a}'

echo; echo "=== [B] did deploy.resources.limits + cpuset actually reach the cgroup? ==="
CID=$(docker compose ps -q valkey)
docker inspect "$CID" --format 'HostConfig.NanoCpus={{.HostConfig.NanoCpus}}
HostConfig.Memory={{.HostConfig.Memory}}
HostConfig.CpusetCpus={{.HostConfig.CpusetCpus}}
HostConfig.PidsLimit={{.HostConfig.PidsLimit}}'
docker exec "$CID" sh -c '
 echo "cpu.max=$(cat /sys/fs/cgroup/cpu.max)"
 echo "cpuset.cpus.effective=$(cat /sys/fs/cgroup/cpuset.cpus.effective 2>/dev/null)"
 echo "memory.max=$(cat /sys/fs/cgroup/memory.max)"
 echo "nproc_visible=$(nproc)"'

echo; echo "=== [C] engine version + config read back from the RUNNING server ==="
docker exec "$CID" valkey-cli INFO server | tr -d '\r' | grep -E '^(valkey_version|redis_version|io_threads_active|os|arch_bits|multiplexing_api|process_id)'
docker exec "$CID" valkey-cli CONFIG GET maxmemory
docker exec "$CID" valkey-cli CONFIG GET io-threads
docker exec "$CID" valkey-cli CONFIG GET appendonly
docker exec "$CID" valkey-cli CONFIG GET save

echo; echo "=== [D] LOAD + measured runs with valkey-benchmark ==="
echo "--- warmup (discarded)"
docker exec "$CID" valkey-benchmark -q -t set,get -n 20000 -c 16 -P 1 2>&1 | tail -4
for r in 1 2 3; do
  echo "--- repeat $r (csv output, p50/p95/p99 included)"
  docker exec "$CID" valkey-benchmark --csv -t set,get -n 50000 -c 16 -P 1 2>&1 | tail -3
done

echo; echo "=== [E] latency-percentile detail available? ==="
docker exec "$CID" valkey-cli INFO latencystats 2>&1 | tr -d '\r' | head -8
docker exec "$CID" valkey-cli INFO commandstats 2>&1 | tr -d '\r' | head -5
docker exec "$CID" valkey-cli MEMORY STATS 2>&1 | head -6

echo; echo "=== [F] stats ==="
docker stats --no-stream --format '{{json .}}' "$CID"

echo; echo "=== [G] TEARDOWN via compose down -v (run twice for idempotency) ==="
S=$(date +%s.%N)
docker compose down -v --remove-orphans 2>&1 | tail -3
docker compose down -v --remove-orphans 2>&1 | tail -2; echo "second down exit=$?"
E=$(date +%s.%N); awk -v a=$S -v b=$E 'BEGIN{printf "teardown=%.3fs\n", b-a}'
echo "=== DONE ==="
