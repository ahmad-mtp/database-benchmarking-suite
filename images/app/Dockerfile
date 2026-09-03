# The app tier (PLAN.md S13).
#
# Built on the same digest-pinned python:3.13-slim base as the driver, and
# built the same way -- wheels compiled in a stage that is thrown away, because
# `hdrhistogram` ships a C extension and a compiler in a measurement container
# is memory budget and attack surface spent on nothing the run uses.
#
# The app tier gets the host slice (cpuset 0-1) and a quota of its own. That
# quota is the denominator of `app_tier_cpu_pct`: an unlimited container has no
# denominator, and the gate would then pass by never having a number.
ARG BASE=python@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285

FROM ${BASE} AS build

RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN pip wheel --wheel-dir /wheels \
        "asyncpg==0.31.0" \
        "hdrhistogram==0.10.7" \
        "pydantic==2.12.5" \
        "fastapi==0.141.1" \
        "uvicorn==0.52.4"


FROM ${BASE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links /wheels \
        asyncpg hdrhistogram pydantic fastapi uvicorn \
    && rm -rf /wheels

WORKDIR /opt/dsel
COPY src/dsel /opt/dsel/dsel
ENV PYTHONPATH=/opt/dsel
RUN mkdir -p /run/dsel

EXPOSE 8000
# One uvicorn worker by default. The worker count is part of the app's
# envelope, not a detail: it is what turns the tier's quota into concurrency,
# and S13's ceiling is measured at a stated count.
ENV DSEL_APP_WORKERS=1
CMD ["sh", "-c", "exec uvicorn dsel.app.main:app --host 0.0.0.0 --port 8000 \
     --workers ${DSEL_APP_WORKERS} --no-access-log --log-level warning"]
