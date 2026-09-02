#!/usr/bin/env bash
# exp05 — the decisive storage question, measured at the DB layer instead of with dd:
# identical Postgres, identical workload, PGDATA on (a) named volume, (b) macOS bind mount,
# (c) tmpfs. If (a) and (b) differ a lot, dev-machine numbers are not portable.
set -uo pipefail
cd "$(dirname "$0")"
PG="postgres@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2"
BIND="$(pwd)/exp05-bind"

cleanup(){ docker rm -f dsa-exp05 >/dev/null 2>&1; docker volume rm -f dsa-exp05-vol >/dev/null 2>&1; rm -rf "$BIND"; }
trap cleanup EXIT INT TERM; cleanup

case_run () {
  LABEL="$1"; shift
  echo; echo "################ $LABEL ################"
  docker rm -f dsa-exp05 >/dev/null 2>&1
  docker run -d --name dsa-exp05 --cpus=2.0 --cpuset-cpus=0-1 --memory=2g --memory-swap=2g \
    -e POSTGRES_PASSWORD=bench -e POSTGRES_USER=bench -e POSTGRES_DB=bench \
    -e PGDATA=/var/lib/postgresql/data/pgdata "$@" \
    --health-cmd='pg_isready -U bench -d bench -h 127.0.0.1' \
    --health-interval=1s --health-retries=90 --health-start-period=1s \
    "$PG" -c shared_buffers=256MB -c fsync=on -c synchronous_commit=on -c full_page_writes=on >/dev/null
  D=$(( $(date +%s) + 150 )); ST=""
  while [ "$(date +%s)" -lt "$D" ]; do ST=$(docker inspect -f '{{.State.Health.Status}}' dsa-exp05 2>/dev/null); [ "$ST" = healthy -o "$ST" = unhealthy ] && break; sleep 0.25; done
  echo "health=$ST"
  [ "$ST" != healthy ] && { docker logs --tail 15 dsa-exp05; return; }
  docker exec dsa-exp05 pgbench -i -s 20 -q -U bench -d bench 2>&1 | tail -1
  echo "  -- warmup 8s (discarded)"; docker exec dsa-exp05 pgbench -c 8 -j 2 -T 8 -U bench -d bench >/dev/null 2>&1
  echo "  -- 3 x 15s WRITE-HEAVY (default tpcb-like, commit-bound => fsync-sensitive)"
  for r in 1 2 3; do
    docker exec dsa-exp05 pgbench -c 8 -j 2 -T 15 -U bench -d bench 2>&1 | grep -E '^(tps|latency average)' | tr '\n' ' '; echo "[rep $r]"
  done
  echo "  -- 3 x 10s READ-ONLY (-S, page-cache bound => storage-insensitive control)"
  for r in 1 2 3; do
    docker exec dsa-exp05 pgbench -S -c 8 -j 2 -T 10 -U bench -d bench 2>&1 | grep -E '^tps' | tr '\n' ' '; echo "[rep $r]"
  done
  docker exec dsa-exp05 sh -c 'df -h /var/lib/postgresql/data | tail -1; stat -f -c "fstype=%T" /var/lib/postgresql/data'
  docker stats --no-stream --format 'BlockIO={{.BlockIO}} Mem={{.MemUsage}}' dsa-exp05
  docker rm -f dsa-exp05 >/dev/null 2>&1
}

docker volume create dsa-exp05-vol >/dev/null
case_run "A: NAMED VOLUME (ext4 in the Linux VM)"  -v dsa-exp05-vol:/var/lib/postgresql/data
mkdir -p "$BIND"; chmod 777 "$BIND"
case_run "B: BIND MOUNT (VirtioFS -> macOS APFS)"  -v "$BIND":/var/lib/postgresql/data
case_run "C: TMPFS (RAM — no durable storage at all)" --tmpfs /var/lib/postgresql/data:rw,size=2g,mode=0777
trap - EXIT; cleanup
echo; echo "=== DONE ==="
