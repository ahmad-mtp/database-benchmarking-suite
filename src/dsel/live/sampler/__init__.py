"""Samplers write records. They never derive a phenomenon.

PLAN.md's structural rule, enforced by a test: `live/sampler/*` only writes
records; `phenomena/*` only reads `metrics.ndjson` and never touches Docker.
That separation is what makes S15's acceptance criterion -- an independent
script re-deriving the knee from the metrics file alone -- achievable.
"""
