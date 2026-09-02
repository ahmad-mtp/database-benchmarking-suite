#!/usr/bin/env python3
"""
exp06 — verify the two audit claims that the whole design rests on:
  (1) a dataset generated from (seed, table, row_id, column) is byte-identical
      across processes, across insertion order, and across parallelism;
  (2) a canonicalised spec hashes stably under reformatting but changes under
      semantic edits.
Uses only the stdlib so it is reproducible without a package install.
"""
import hashlib, json, os, subprocess, sys, unicodedata
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------- (1) data gen
def field(seed: str, table: str, row_id: int, col: str, n: int) -> int:
    """Counter-based, index-derived draw. NOT a sequential PRNG: the value depends
    only on its coordinates, so partitioning and ordering cannot change it."""
    h = hashlib.blake2b(f"{seed}|{table}|{row_id}|{col}".encode(), digest_size=16).digest()
    return int.from_bytes(h, "big") % n

def zipf_rank(seed, table, row_id, col, n, hot_frac, hot_traffic):
    """Hot-set model: hot_traffic of draws land in the first hot_frac of the keyspace.
    Chosen over a bare Zipf theta because both parameters are directly measurable
    against production telemetry."""
    u = field(seed, table, row_id, col + ":sel", 10**9) / 10**9
    hot_n = max(1, int(n * hot_frac))
    if u < hot_traffic:
        return field(seed, table, row_id, col + ":hot", hot_n)
    return hot_n + field(seed, table, row_id, col + ":cold", max(1, n - hot_n))

def gen_row(seed, i):
    return {
        "order_id": i,
        "customer_id": field(seed, "orders", i, "customer", 50_000),
        "sku_id": zipf_rank(seed, "orders", i, "sku", 50_000, 0.20, 0.80),
        "qty": 1 + field(seed, "orders", i, "qty", 5),
        "cents": 500 + field(seed, "orders", i, "cents", 500_00),
    }

def dataset_hash(seed, n, workers=1, reverse=False):
    idx = range(n - 1, -1, -1) if reverse else range(n)
    if workers == 1:
        rows = [gen_row(seed, i) for i in idx]
    else:
        with ThreadPoolExecutor(workers) as ex:
            rows = list(ex.map(lambda i: gen_row(seed, i), idx))
    rows.sort(key=lambda r: r["order_id"])          # canonical order for hashing
    h = hashlib.sha256()
    for r in rows:
        h.update(json.dumps(r, sort_keys=True, separators=(",", ":")).encode())
        h.update(b"\n")
    return h.hexdigest()

N = 200_000
base = dataset_hash("uc1-seed-2026", N)
print("=== (1) deterministic dataset generation ===")
print(f"  rows={N}")
print(f"  serial, forward        : {base}")
for label, kw in [("serial, REVERSE order", dict(reverse=True)),
                  ("8 threads, forward   ", dict(workers=8)),
                  ("8 threads, REVERSE   ", dict(workers=8, reverse=True))]:
    h = dataset_hash("uc1-seed-2026", N, **kw)
    print(f"  {label}  : {h}  {'MATCH' if h == base else 'DIFFER <-- BUG'}")
diff = dataset_hash("uc1-seed-2027", N)
print(f"  different seed         : {diff}  {'DIFFER (correct)' if diff != base else 'MATCH <-- BUG'}")

# subprocess = fresh interpreter, fresh PYTHONHASHSEED
env = dict(os.environ); env.pop("PYTHONHASHSEED", None)
out = subprocess.run([sys.executable, "-c",
    f"import sys; sys.path.insert(0,'.'); "
    f"exec(open('exp06-determinism.py').read().split('N = 200_000')[0]); "
    f"print(dataset_hash('uc1-seed-2026', {N}))"],
    capture_output=True, text=True, env=env, cwd=os.path.dirname(os.path.abspath(__file__)))
sp = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else f"ERR {out.stderr[-200:]}"
print(f"  fresh subprocess       : {sp}  {'MATCH' if sp == base else 'DIFFER <-- BUG'}")

# check the skew actually materialised
rows = [gen_row("uc1-seed-2026", i) for i in range(50_000)]
hot = sum(1 for r in rows if r["sku_id"] < 10_000)
print(f"  skew check: {hot/len(rows):.1%} of draws hit the hot 20% (target 80%)")

# ------------------------------------------------------- (2) spec canonicalisation
def jcs(o):
    """RFC 8785-style canonicalisation: sorted keys, no whitespace, UTF-8, NFC."""
    if isinstance(o, str):  return unicodedata.normalize("NFC", o)
    if isinstance(o, dict): return {jcs(k): jcs(v) for k, v in sorted(o.items())}
    if isinstance(o, list): return [jcs(v) for v in o]
    return o

def spec_hash(d):
    return hashlib.sha256(json.dumps(jcs(d), sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()

A = {"spec_version": "1.0", "name": "uc1-orders",
     "workload": {"read_pct": 70, "write_pct": 30, "target_rps": 800},
     "slo": {"p99_ms": 50}}
B = {"slo": {"p99_ms": 50}, "name": "uc1-orders", "spec_version": "1.0",
     "workload": {"target_rps": 800, "write_pct": 30, "read_pct": 70}}   # reordered
C = json.loads(json.dumps(A)); C["workload"]["target_rps"] = 900          # semantic edit

print("\n=== (2) spec canonicalisation ===")
ha, hb, hc = spec_hash(A), spec_hash(B), spec_hash(C)
print(f"  A (as authored)        : {ha}")
print(f"  B (keys reordered)     : {hb}  {'MATCH (correct)' if ha == hb else 'DIFFER <-- BUG'}")
print(f"  C (target_rps 800->900): {hc}  {'DIFFER (correct)' if ha != hc else 'MATCH <-- BUG'}")

# ------------------------------------------------------- (3) bundle merkle root
print("\n=== (3) bundle manifest hashing ===")
files = {"spec.json": json.dumps(A).encode(),
         "dataset.sha256": base.encode(),
         "metrics.ndjson": b'{"cell":"pg/uc1","tps":12854.2}\n'}
leaves = {n: hashlib.sha256(b).hexdigest() for n, b in files.items()}
for n, h in sorted(leaves.items()):
    print(f"  {h}  {n}")
root = hashlib.sha256("".join(f"{n}:{leaves[n]}\n" for n in sorted(leaves)).encode()).hexdigest()
print(f"  bundle_root = {root}")
tampered = dict(files); tampered["metrics.ndjson"] = b'{"cell":"pg/uc1","tps":99999.9}\n'
tl = {n: hashlib.sha256(b).hexdigest() for n, b in tampered.items()}
troot = hashlib.sha256("".join(f"{n}:{tl[n]}\n" for n in sorted(tl)).encode()).hexdigest()
print(f"  after tampering with one metric value:")
print(f"  bundle_root = {troot}  {'DETECTED' if troot != root else 'MISSED <-- BUG'}")
