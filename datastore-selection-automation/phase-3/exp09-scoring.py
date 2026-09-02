#!/usr/bin/env python3
"""
exp09 — the scoring model, worked end-to-end on UC-1 with the arithmetic shown.

Pipeline: GATE -> NORMALISE -> VETO -> WEIGHT -> RANK -> SENSITIVITY -> PARETO.
Deliberately simple maths. The value is not in the algorithm, it is in (a) gating
before scoring, (b) non-compensatory vetoes, (c) separating measured from judged,
and (d) reporting whether the winner survives perturbation of the weights.
"""
import itertools, json

# ---------------------------------------------------------------- inputs
# Measured values: the postgres row uses REAL numbers from phase-3 exp05
# (named volume, 3x15s write-heavy + 3x10s read-only, cpus=2/cpuset=0-1/2GiB).
# The other rows are ILLUSTRATIVE placeholders with the correct shape and units --
# they were not measured, and are labelled as such in every output.
CANDIDATES = {
    "postgres-18": {
        "measured": True,
        "slo_attainment":  0.97,   # fraction of ops meeting their per-pattern p99 SLO
        "throughput_headroom": 2.6,# sustained_rps / required_rps at <=60% CPU
        "cpu_at_target":   0.44,   # CPU fraction consumed at the required rate
        "write_amp":       20.1,   # bytes written / logical bytes  (6.44GB / 320MB)
        "op_burden":       4.0,    # 1-5 human rubric, 5 = least burden
        "familiarity":     5.0,
        "ecosystem":       5.0,
        "licence_risk":    5.0,    # 5 = permissive/OSI, 1 = hostile
    },
    "mongodb-8": {
        "measured": False,
        "slo_attainment": 0.91, "throughput_headroom": 2.1, "cpu_at_target": 0.52,
        "write_amp": 12.4, "op_burden": 3.0, "familiarity": 3.0,
        "ecosystem": 4.0, "licence_risk": 2.0,          # SSPL: not OSI-approved
    },
    "postgres-18-plus-valkey": {
        "measured": False,
        "slo_attainment": 0.99, "throughput_headroom": 4.2, "cpu_at_target": 0.38,
        "write_amp": 20.1, "op_burden": 2.5,            # two systems to operate
        "familiarity": 4.0, "ecosystem": 5.0, "licence_risk": 5.0,
    },
    "clickhouse-26": {
        "measured": False,
        "slo_attainment": 0.55, "throughput_headroom": 6.0, "cpu_at_target": 0.20,
        "write_amp": 3.1, "op_burden": 3.0, "familiarity": 2.0,
        "ecosystem": 4.0, "licence_risk": 5.0,
    },
}

# gate() results, from the adapter capability tables (evaluated BEFORE any container)
GATES = {
    "postgres-18":             [],
    "mongodb-8":               ["licence SSPL-1.0 is not OSI-approved "
                                "(spec requires licence_policy=osi_approved_only)"],
    "postgres-18-plus-valkey": [],
    "clickhouse-26":           ["transaction scope 'none' < required 'multi_document'",
                                "cannot provide durability=fsync_on_commit per-row"],
}

WEIGHTS = {  # from spec.scoring; measured and qualitative kept separate on purpose
    "measured":    {"slo_attainment": .30, "throughput_headroom": .15,
                    "resource_efficiency": .10, "storage_amplification": .05},
    "qualitative": {"op_burden": .20, "familiarity": .10,
                    "ecosystem": .05, "licence_risk": .05},
}
VETOES = [("slo_attainment", 0.80)]

HIGHER_IS_BETTER = {"slo_attainment", "throughput_headroom", "resource_efficiency",
                    "storage_amplification", "op_burden", "familiarity",
                    "ecosystem", "licence_risk"}

def derive(c):
    d = dict(c)
    d["resource_efficiency"]   = 1.0 - c["cpu_at_target"]   # less CPU = better
    d["storage_amplification"] = 1.0 / c["write_amp"]       # less amp = better
    return d

def minmax(vals):
    lo, hi = min(vals), max(vals)
    return (lambda v: 0.5) if hi == lo else (lambda v: (v - lo) / (hi - lo))

def score(cands, weights):
    d = {k: derive(v) for k, v in cands.items()}
    crit = {**weights["measured"], **weights["qualitative"]}
    norm = {c: minmax([d[k][c] for k in d]) for c in crit}
    out = {}
    for k, v in d.items():
        parts = {c: norm[c](v[c]) * w for c, w in crit.items()}
        out[k] = {"total": sum(parts.values()), "parts": parts,
                  "normed": {c: norm[c](v[c]) for c in crit}}
    return out

print("=" * 78)
print("STEP 1 — GATE (runs BEFORE any container starts)")
print("=" * 78)
survivors = {}
for k in CANDIDATES:
    if GATES[k]:
        print(f"  ✗ {k:26s} EXCLUDED")
        for f in GATES[k]:
            print(f"      · {f}")
    else:
        print(f"  ✓ {k:26s} admitted to benchmarking")
        survivors[k] = CANDIDATES[k]
print(f"\n  {len(survivors)}/{len(CANDIDATES)} candidates benchmarked. "
      f"Excluded candidates are NEVER scored -- a gate failure is not a low score.")

print("\n" + "=" * 78)
print("STEP 2 — DERIVED CRITERIA")
print("=" * 78)
print(f"  {'candidate':28s} {'slo':>6s} {'hdrm':>6s} {'res_eff':>8s} {'stor_amp':>9s}")
for k, v in survivors.items():
    d = derive(v)
    print(f"  {k:28s} {d['slo_attainment']:6.2f} {d['throughput_headroom']:6.2f} "
          f"{d['resource_efficiency']:8.3f} {d['storage_amplification']:9.4f}"
          f"   {'(MEASURED)' if v['measured'] else '(illustrative)'}")

print("\n" + "=" * 78)
print("STEP 3 — VETO (non-compensatory: no weight can rescue a vetoed candidate)")
print("=" * 78)
vetoed = {}
for k, v in list(survivors.items()):
    for crit, mn in VETOES:
        if v[crit] < mn:
            vetoed[k] = f"{crit}={v[crit]:.2f} < required {mn}"
            print(f"  ✗ {k:26s} VETOED: {vetoed[k]}")
            survivors.pop(k)
for k in survivors:
    print(f"  ✓ {k:26s} passes all vetoes")

print("\n" + "=" * 78)
print("STEP 4 — NORMALISE + WEIGHT (min-max within the surviving set)")
print("=" * 78)
res = score(survivors, WEIGHTS)
crit_order = list(WEIGHTS["measured"]) + list(WEIGHTS["qualitative"])
print(f"  {'criterion':22s} {'w':>5s} " + " ".join(f"{k[:14]:>16s}" for k in survivors))
for c in crit_order:
    w = {**WEIGHTS['measured'], **WEIGHTS['qualitative']}[c]
    row = " ".join(f"{res[k]['normed'][c]:6.3f}x{w:.2f}={res[k]['parts'][c]:5.3f}"
                   for k in survivors)
    tag = "M" if c in WEIGHTS["measured"] else "Q"
    print(f"  [{tag}] {c:18s} {w:5.2f} {row}")
print(f"  {'':22s} {'':>5s} " + " ".join(f"{'TOTAL '+format(res[k]['total'],'.4f'):>16s}"
                                          for k in survivors))
mw = sum(WEIGHTS['measured'].values()); qw = sum(WEIGHTS['qualitative'].values())
print(f"\n  [M]=measured ({mw:.0%} of weight)   [Q]=human judgement ({qw:.0%} of weight)")

print("\n" + "=" * 78)
print("STEP 5 — RANKING")
print("=" * 78)
rank = sorted(res.items(), key=lambda kv: -kv[1]["total"])
for i, (k, v) in enumerate(rank, 1):
    print(f"  {i}. {k:28s} {v['total']:.4f}")
winner, runner = rank[0][0], rank[1][0]
margin = rank[0][1]["total"] - rank[1][1]["total"]
print(f"\n  Winner: {winner}   margin over runner-up: {margin:.4f}")

print("\n" + "=" * 78)
print("STEP 6 — SENSITIVITY (does the winner survive ±25% on every weight?)")
print("=" * 78)
crit = {**WEIGHTS["measured"], **WEIGHTS["qualitative"]}
flips = []
for c in crit:
    for f in (0.75, 1.25):
        w2 = {"measured": dict(WEIGHTS["measured"]), "qualitative": dict(WEIGHTS["qualitative"])}
        grp = "measured" if c in WEIGHTS["measured"] else "qualitative"
        w2[grp][c] = crit[c] * f
        tot = sum(w2["measured"].values()) + sum(w2["qualitative"].values())
        for g in w2:
            w2[g] = {k: v / tot for k, v in w2[g].items()}     # renormalise to 1.0
        r2 = sorted(score(survivors, w2).items(), key=lambda kv: -kv[1]["total"])
        if r2[0][0] != winner:
            flips.append((c, f, r2[0][0]))
print(f"  {len(crit)*2} perturbations tested ({len(crit)} weights x ±25%).")
if flips:
    print("  ⚠ RANKING IS NOT ROBUST — the winner changes under:")
    for c, f, w in flips:
        print(f"      {c} x{f}  ->  {w}")
else:
    print(f"  ✓ '{winner}' wins in ALL {len(crit)*2} perturbations. Ranking is robust")
    print("    to ±25% on any single weight.")

# how far must a single weight move before the answer flips?
print("\n  Breaking point per weight (how much must ONE weight change to flip it?):")
for c in crit_order:
    bp = None
    for f in [x / 100 for x in range(5, 1005, 5)]:
        w2 = {"measured": dict(WEIGHTS["measured"]), "qualitative": dict(WEIGHTS["qualitative"])}
        grp = "measured" if c in WEIGHTS["measured"] else "qualitative"
        w2[grp][c] = crit[c] * f
        tot = sum(w2["measured"].values()) + sum(w2["qualitative"].values())
        for g in w2:
            w2[g] = {k: v / tot for k, v in w2[g].items()}
        r2 = sorted(score(survivors, w2).items(), key=lambda kv: -kv[1]["total"])
        if r2[0][0] != winner:
            bp = f; break
    print(f"    {c:22s} " + (f"flips at x{bp:.2f} (weight {crit[c]:.2f} -> {crit[c]*bp:.3f})"
                             if bp else "never flips within x0.05..x10"))

print("\n" + "=" * 78)
print("STEP 7 — PARETO FRONT (which candidates are not dominated on raw criteria?)")
print("=" * 78)
d = {k: derive(v) for k, v in survivors.items()}
cs = list(crit)
front = []
for a in survivors:
    dominated = any(all(d[b][c] >= d[a][c] for c in cs) and any(d[b][c] > d[a][c] for c in cs)
                    for b in survivors if b != a)
    if not dominated: front.append(a)
    print(f"  {a:28s} {'on the Pareto front' if not dominated else 'DOMINATED'}")
print(f"\n  {len(front)} non-dominated candidate(s). Where the front has >1 member the "
      f"weights\n  are doing the real work, and the reader should be told so explicitly.")
