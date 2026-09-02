#!/usr/bin/env python3
"""exp11 — sensitivity + breaking points under REFERENCE-ANCHORED normalisation
(exp09's sweep used min-max, so its breaking points do not apply to the final model)."""
W = {"latency_slo_attainment": .30, "throughput_headroom": .15, "resource_efficiency": .10,
     "storage_amplification": .05, "operational_burden": .20, "team_familiarity": .10,
     "ecosystem_maturity": .05, "licence_risk": .05}
REF = {"latency_slo_attainment": (0.80, 1.00), "throughput_headroom": (1.0, 5.0),
       "resource_efficiency": (0.0, 1.0), "storage_amplification": (0.01, 0.50),
       "operational_burden": (1, 5), "team_familiarity": (1, 5),
       "ecosystem_maturity": (1, 5), "licence_risk": (1, 5)}
C = {
 "postgres-18":             {"latency_slo_attainment":.97,"throughput_headroom":2.6,
   "resource_efficiency":.56,"storage_amplification":.0498,"operational_burden":4.0,
   "team_familiarity":5.0,"ecosystem_maturity":5.0,"licence_risk":5.0},
 "postgres-18-plus-valkey": {"latency_slo_attainment":.99,"throughput_headroom":4.2,
   "resource_efficiency":.62,"storage_amplification":.0498,"operational_burden":2.5,
   "team_familiarity":4.0,"ecosystem_maturity":5.0,"licence_risk":5.0}}

def score(w):
    out={}
    for k,v in C.items():
        t=0.0
        for c,wt in w.items():
            lo,hi=REF[c]; t += max(0.,min(1.,(v[c]-lo)/(hi-lo)))*wt
        out[k]=t
    return out
base=score(W); rank=sorted(base,key=lambda k:-base[k]); winner=rank[0]
print(f"baseline: {rank[0]}={base[rank[0]]:.4f}  {rank[1]}={base[rank[1]]:.4f}  margin={base[rank[0]]-base[rank[1]]:.4f}")
print(f"winner = {winner}\n")
print("±25% sweep:")
flips=0
for c in W:
    for f in (.75,1.25):
        w2=dict(W); w2[c]=W[c]*f; tot=sum(w2.values()); w2={k:v/tot for k,v in w2.items()}
        r=sorted(score(w2),key=lambda k:-score(w2)[k])
        if r[0]!=winner: flips+=1; print(f"  FLIP: {c} x{f} -> {r[0]}")
print(f"  {flips}/{len(W)*2} perturbations flip the winner\n")
print("breaking point per weight (x-factor at which the winner changes):")
for c in W:
    bp=None
    for i in range(1,2001):
        f=i/100.0
        w2=dict(W); w2[c]=W[c]*f; tot=sum(w2.values()); w2={k:v/tot for k,v in w2.items()}
        s=score(w2); r=sorted(s,key=lambda k:-s[k])
        if r[0]!=winner: bp=f; break
    print(f"  {c:24s} " + (f"flips at x{bp:.2f}  ({W[c]:.2f} -> {W[c]*bp:.3f})" if bp
                            else "never flips within x0.01..x20"))

print("\nnearest breaking point, searched OUTWARD from x1.00 (direction matters):")
for c in W:
    best=None
    for i in range(1,2000):
        for f in (1.0 - i/1000.0, 1.0 + i/1000.0):
            if f <= 0: continue
            w2=dict(W); w2[c]=W[c]*f; tot=sum(w2.values()); w2={k:v/tot for k,v in w2.items()}
            s=score(w2); r=sorted(s,key=lambda k:-s[k])
            if r[0]!=winner:
                best=(f, "decrease" if f<1 else "increase"); break
        if best: break
    print(f"  {c:24s} " + (f"{best[1]:8s} to x{best[0]:.3f}  ({W[c]:.2f} -> {W[c]*best[0]:.3f})"
                            f"  = {abs(1-best[0])*100:.1f}% change" if best else "never flips"))
