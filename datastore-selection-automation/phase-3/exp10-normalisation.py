#!/usr/bin/env python3
"""
exp10 — why min-max-within-the-candidate-set is the wrong normaliser, demonstrated,
and what to use instead. This is the classic MCDM rank-reversal failure mode.
"""
W = {"slo_attainment": .30, "throughput_headroom": .15, "resource_efficiency": .10,
     "storage_amplification": .05, "op_burden": .20, "familiarity": .10,
     "ecosystem": .05, "licence_risk": .05}

A = {"slo_attainment": 0.97, "throughput_headroom": 2.6, "resource_efficiency": .56,
     "storage_amplification": .0498, "op_burden": 4.0, "familiarity": 5.0,
     "ecosystem": 5.0, "licence_risk": 5.0}
B = {"slo_attainment": 0.99, "throughput_headroom": 4.2, "resource_efficiency": .62,
     "storage_amplification": .0498, "op_burden": 2.5, "familiarity": 4.0,
     "ecosystem": 5.0, "licence_risk": 5.0}
# an irrelevant third candidate: mediocre everywhere, wins nothing
C = {"slo_attainment": 0.72, "throughput_headroom": 1.4, "resource_efficiency": .30,
     "storage_amplification": .0400, "op_burden": 3.0, "familiarity": 3.0,
     "ecosystem": 3.0, "licence_risk": 3.0}

def minmax_score(cands):
    out = {}
    for c in W:
        vals = [x[c] for x in cands.values()]
        lo, hi = min(vals), max(vals)
        for k in cands:
            n = 0.5 if hi == lo else (cands[k][c] - lo) / (hi - lo)
            out.setdefault(k, 0.0)
            out[k] += n * W[c]
    return out

# Reference-based: normalise against ABSOLUTE anchors from the spec, not the field.
REF = {  # (value scoring 0.0, value scoring 1.0) -- fixed, candidate-independent
    "slo_attainment":       (0.80, 1.00),   # veto floor .. perfect
    "throughput_headroom":  (1.00, 5.00),   # just-meets .. 5x headroom
    "resource_efficiency":  (0.00, 1.00),   # saturated .. idle
    "storage_amplification":(0.01, 0.50),   # 100x amp .. 2x amp
    "op_burden":            (1.00, 5.00),   # rubric endpoints
    "familiarity":          (1.00, 5.00),
    "ecosystem":            (1.00, 5.00),
    "licence_risk":         (1.00, 5.00),
}
def ref_score(cands):
    out = {}
    for k, v in cands.items():
        t = 0.0
        for c, w in W.items():
            lo, hi = REF[c]
            t += max(0.0, min(1.0, (v[c] - lo) / (hi - lo))) * w
        out[k] = t
    return out

print("="*74)
print("THE PROBLEM: min-max normalises against whoever happens to be in the set")
print("="*74)
for label, field in [("two candidates {A,B}", {"A": A, "B": B}),
                     ("add irrelevant C  {A,B,C}", {"A": A, "B": B, "C": C})]:
    s = minmax_score(field)
    order = sorted(s, key=lambda k: -s[k])
    print(f"\n  {label}")
    for k in order: print(f"    {k}: {s[k]:.4f}")
    print(f"    ranking: {' > '.join(order)}   gap(A,B) = {abs(s['A']-s['B']):.4f}")

print("\n  Two defects visible:")
print("   1. MAGNITUDE IS DESTROYED. slo 0.97 vs 0.99 -- a 2-point difference --")
print("      normalises to 0.000 vs 1.000, identical to what 0.10 vs 0.99 would give.")
print("      The model cannot tell a photo-finish from a landslide.")
print("   2. RANK REVERSAL RISK. Adding a candidate that wins nothing still changes")
print("      every existing score, because it moves the min/max anchors.")

print("\n" + "="*74)
print("THE FIX: normalise against absolute, spec-derived anchors")
print("="*74)
for label, field in [("two candidates {A,B}", {"A": A, "B": B}),
                     ("add irrelevant C  {A,B,C}", {"A": A, "B": B, "C": C})]:
    s = ref_score(field)
    order = sorted(s, key=lambda k: -s[k])
    print(f"\n  {label}")
    for k in order: print(f"    {k}: {s[k]:.4f}")
    print(f"    ranking: {' > '.join(order)}   gap(A,B) = {abs(s['A']-s['B']):.4f}")

sAB = ref_score({"A": A, "B": B}); sABC = ref_score({"A": A, "B": B, "C": C})
print(f"\n  A's score with C absent : {sAB['A']:.4f}")
print(f"  A's score with C present: {sABC['A']:.4f}   "
      f"{'UNCHANGED - immune to rank reversal' if abs(sAB['A']-sABC['A'])<1e-12 else 'CHANGED'}")
print(f"  gap(A,B) is now {abs(sAB['A']-sAB['B']):.4f}, not 0.2500 -- a photo finish")
print("  now LOOKS like a photo finish, which is the point.")
print("\n  Cost: someone must author the anchors. That is a feature, not a bug --")
print("  the anchors are business facts ('1x headroom is the floor, 5x is plenty')")
print("  and belong in the spec, reviewed, rather than emerging from an accident")
print("  of which candidates were tested.")
