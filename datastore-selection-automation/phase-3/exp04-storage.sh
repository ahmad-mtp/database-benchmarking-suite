#!/usr/bin/env bash
# exp04 — does macOS Docker Desktop's virtualised filesystem distort DB storage IO?
# Compare: named volume (inside the Linux VM's ext4) vs bind mount (VirtioFS to macOS) vs tmpfs.
# This decides whether dev-machine numbers are usable at all.
set -uo pipefail
cd "$(dirname "$0")"
IMG="alpine@$(docker buildx imagetools inspect alpine:3.24 --format '{{.Manifest.Digest}}')"
echo "alpine ref = $IMG"
BINDDIR="$(pwd)/exp04-bind"; rm -rf "$BINDDIR"; mkdir -p "$BINDDIR"
docker volume rm -f dsa-exp04-vol >/dev/null 2>&1; docker volume create dsa-exp04-vol >/dev/null

# dd-based sequential + fsync-per-block (approximates WAL fsync behaviour, the thing DBs care about)
probe() {
  LABEL=$1; shift
  echo; echo "### $LABEL"
  docker run --rm "$@" "$IMG" sh -c '
    cd /probe
    echo -n "  seq_write_256MB_fsync_at_end: "
    dd if=/dev/zero of=f1 bs=1M count=256 conv=fsync 2>&1 | tail -1
    echo -n "  fsync_per_1MB_x64 (WAL-like):  "
    dd if=/dev/zero of=f2 bs=1M count=64 oflag=dsync 2>&1 | tail -1
    echo -n "  fsync_per_8KB_x2000 (commit-like): "
    dd if=/dev/zero of=f3 bs=8k count=2000 oflag=dsync 2>&1 | tail -1
    echo -n "  metadata: 2000 file creates:   "
    S=$(date +%s%N); i=0; while [ $i -lt 2000 ]; do : > "m$i"; i=$((i+1)); done; E=$(date +%s%N)
    echo "$(( (E-S)/1000000 )) ms"
    rm -f f1 f2 f3 m* 2>/dev/null
  ' 2>&1
}
probe "NAMED VOLUME (ext4 inside the Linux VM)" -v dsa-exp04-vol:/probe
probe "BIND MOUNT (VirtioFS bridge to macOS APFS)" -v "$BINDDIR":/probe
probe "TMPFS (RAM, upper bound / noise floor)" --tmpfs /probe:rw,size=1g
probe "CONTAINER WRITABLE LAYER (overlayfs)" -w /probe --entrypoint sh

docker volume rm -f dsa-exp04-vol >/dev/null 2>&1; rm -rf "$BINDDIR"
echo; echo "=== DONE ==="
