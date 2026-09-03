"""The app tier (PLAN.md S13).

FastAPI on uvicorn, one asyncpg pool per worker, every request timed at four
instants. Two things about it are deliberate and load-bearing.

**`/noop` exists to be saturated.** Its ceiling is the tier's own limit with no
engine involved, and S13 requires it measured *before* PATH B is wired, so that
app-tier saturation is a known number rather than a surprise discovered later
in a result that looked like a database difference. Everything about `/noop` is
chosen to make it the cheapest possible request: no pool, no I/O, a constant
body.

**The tier reports its CPU from the cgroup.** A gate written against one
process cannot fire when several uvicorn workers share one quota -- the same
trap D6 documented for the driver, one tier up.

**The span is owned by ASGI middleware, not by the handler.** A span created
inside a handler starts after the framework has already parsed, routed and
validated the request, and ends before the response is serialised and written.
Measured that way the tier's own cost came out as 1 us -- the framework had
made itself invisible to its own instrumentation. The middleware stamps
`t_app_recv` on the way in and `t_app_send` after the last response byte is
handed to the server, so the two outer intervals are the tier's real cost.
Plain ASGI rather than `BaseHTTPMiddleware`, because the wrapper's own
overhead would land inside the number.

Configured entirely from the environment, because it runs in a container and
nothing may be bind-mounted into it (D7):

    DSEL_DSN     postgresql://... (required for the database endpoints)
    DSEL_CELL    the run-matrix cell these records belong to
    DSEL_RUN_DIR where to write metrics shards (default /run/dsel)
    DSEL_SCALE   pgbench scale factor of the loaded dataset
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response

from dsel.app.dal.postgres import PostgresDal
from dsel.app.metrics import AppMetrics
from dsel.app.pools import PoolConfig, open_pool
from dsel.app.spans import Span

RUN_DIR = Path(os.environ.get("DSEL_RUN_DIR", "/run/dsel"))
CELL = os.environ.get("DSEL_CELL", "uc1/postgres/app/r0/rep1")
SCALE = int(os.environ.get("DSEL_SCALE", "10"))

# A constant body. `/noop` measures the tier, so anything it does that varies
# with the response would be measured too.
NOOP_BODY = b'{"ok":true}'


class State:
    """Per-worker state. One pool, one metrics writer, one DAL."""

    pool: Any = None
    dal: PostgresDal | None = None
    metrics: AppMetrics | None = None


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    state.metrics = AppMetrics(RUN_DIR, CELL, pool=None)
    state.metrics.start()
    dsn = os.environ.get("DSEL_DSN")
    if dsn:
        # No pool when there is no DSN: `/noop`'s ceiling has to be measurable
        # without an engine, or the number it produces includes one.
        state.pool = await open_pool(PoolConfig.from_env())
        state.dal = PostgresDal(state.pool, scale=SCALE)
        state.metrics.pool = state.pool
    try:
        yield
    finally:
        if state.metrics is not None:
            state.metrics.stop()
        if state.pool is not None:
            await state.pool.close()


app = FastAPI(title="dsel app tier", lifespan=lifespan, docs_url=None, redoc_url=None)

SPAN_KEY = "dsel.span"
# Endpoints that are instrumentation, not workload. Timing them would put the
# probe's own cost into the tier's numbers.
UNTIMED = frozenset({"/healthz", "/cpu"})


class SpanMiddleware:
    """Stamps the two outer instants around every request.

    The endpoint recorded is the *route template* -- `/account/{aid}`, never
    `/account/17`. One series per account id would be the same cardinality
    mistake the exporter refuses to make (S8b), one tier down.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") in UNTIMED:
            await self.app(scope, receive, send)
            return
        span = Span(endpoint=scope.get("path", "?"))
        scope[SPAN_KEY] = span
        status = 200

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message.get("status", 200))
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body"):
                # The last byte is on the wire; everything before it was the
                # tier's, including serialisation.
                span.send()
                route = scope.get("route")
                if route is not None and getattr(route, "path", None):
                    span.endpoint = route.path
                span.ok = status < 400
                if state.metrics is not None:
                    state.metrics.record(span)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(SpanMiddleware)


def _span(request: Request) -> Span:
    """The middleware's span for this request, or a detached one.

    Never `None`: a handler that silently stopped being timed would leave a
    gap in the record that looks exactly like an endpoint nobody called.
    """
    span = request.scope.get(SPAN_KEY)
    return span if isinstance(span, Span) else Span(endpoint=request.url.path)


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    """Readiness. Deliberately not timed: it is not part of any workload."""
    return {"ok": True, "pool": state.pool is not None, "cell": CELL}


@app.get("/noop")
async def noop() -> Response:
    """The tier's own ceiling: no pool, no I/O, a constant body.

    Timed by the middleware like any other endpoint, but its `db_us` is zero by
    construction -- which is what makes the ceiling a statement about the tier
    rather than about the pair.
    """
    return Response(content=NOOP_BODY, media_type="application/json")


@app.get("/cpu")
async def cpu() -> dict[str, object]:
    """The tier's cgroup CPU against its own quota, for the gate.

    Served rather than only written to the metrics stream so a ceiling probe
    can read it without waiting for a merge, and so the number the gate fires
    on is the number an operator can see.
    """
    metrics = state.metrics
    if metrics is None:
        raise HTTPException(status_code=503, detail="metrics not started")
    return {
        "cpu_pct": metrics.last_cpu_pct,
        "quota_cores": metrics.cpu.quota_cores,
    }


@app.get("/account/{aid}")
async def account(aid: int, request: Request) -> dict[str, object]:
    """The same statement PATH A issues, so S14 can compare the two."""
    span = _span(request)
    dal = _require_dal()
    try:
        balance = await dal.select_account(aid, span)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"aid": aid, "abalance": balance, "db_us": span.db_us}


@app.get("/range/{aid}")
async def count_range(aid: int, request: Request) -> dict[str, object]:
    span = _span(request)
    dal = _require_dal()
    try:
        count = await dal.count_range(aid, span)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"aid": aid, "count": count, "db_us": span.db_us}


@app.get("/report/{aid}")
async def report(aid: int, request: Request) -> dict[str, object]:
    span = _span(request)
    dal = _require_dal()
    try:
        rows = await dal.join_report(aid, span)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"aid": aid, "branches": len(rows), "db_us": span.db_us}


def _require_dal() -> PostgresDal:
    if state.dal is None:
        raise HTTPException(status_code=503, detail="no DSEL_DSN; database endpoints are off")
    return state.dal
