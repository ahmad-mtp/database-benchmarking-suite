# The load driver, containerised (PLAN.md S11-S12, D5).
#
# The driver has to run in a container for the pgbench comparison to mean
# anything. pgbench runs from the engine image by D5, reaching Postgres over a
# Docker network; a driver on the host would reach it through Docker Desktop's
# published-port hop instead, and that hop would show up in the latency
# comparison looking like a difference between the tools. Both in containers on
# cpuset 6-9, on the same network, the paths are identical.
#
# The base is pinned by OCI *index* digest: python:3.13-slim, resolved
# 2026-09-03. The resolved platform manifest digest belongs in the run
# manifest, not here -- it is architecture-locked.
#
# Two stages because `hdrhistogram` ships a C extension (`pyhdrh`) and no wheel
# for linux/arm64, so it has to be compiled. The compiler belongs in the build
# stage only: a toolchain in the measurement container is 400 MB of memory
# budget and attack surface spent on nothing the run uses.
ARG BASE=python@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285

FROM ${BASE} AS build

RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Pinned to the lock's resolution, so the container measures with the same code
# the host would.
RUN pip wheel --wheel-dir /wheels \
        "asyncpg==0.31.0" \
        "hdrhistogram==0.10.7" \
        "pydantic==2.12.5"


FROM ${BASE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links /wheels \
        asyncpg hdrhistogram pydantic \
    && rm -rf /wheels

WORKDIR /opt/dsel
# Pydantic is a runtime dependency, not an optional one: a worker writes its
# `latency_window` and `validity` records through `dsel.live.schema`, so the
# containerised driver produces the same `metrics.ndjson` shards as the host
# one and the two merge into a single stream.
COPY src/dsel /opt/dsel/dsel
ENV PYTHONPATH=/opt/dsel

# Written to by the entrypoint; `docker cp` takes the histograms out
# afterwards. Never a bind mount (D7).
RUN mkdir -p /run/dsel

ENTRYPOINT ["python", "-m", "dsel.driver.entrypoint"]
