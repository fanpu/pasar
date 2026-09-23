"""HTTP API. Handlers are async so they run on the daemon's event loop, never racing a tick."""

import asyncio
import json
import math
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from pasar import __version__
from pasar.config import Config
from pasar.daemon import UNSET, Conflict, Daemon, NotFound
from pasar.guide import load_guide
from pasar.mascot import manifest, resolve, resolve_builtin
from pasar.metrics import Prometheus
from pasar.models import TERMINAL, JobSpec, State
from pasar.units import parse_duration, parse_size
from pasar.views import (
    attempt_view,
    cloud_status_view,
    job_view,
    schedule_projection,
    spark_view,
    status_view,
)

RECENT = 86400
MAX_SPARK_IDS = 200  # more rows than a dashboard ever shows at once
KEEPALIVE_INTERVAL = 15.0  # seconds of quiet before an SSE stream sends a `: keep-alive` comment
_MAX_INT = 2**62  # keeps user-supplied numbers well clear of sqlite's signed-64-bit columns

WEBUI_DIR = Path(__file__).parent / "webui"
_UNBUILT = ("<!doctype html><meta charset=utf-8><title>pasar</title>"
            "<p>pasar web UI is not built. In a clone: build it "
            "(<code>cd web && npm ci && npm run build</code>) and reinstall "
            "(<code>uv tool install --force --reinstall .</code>), or run pasard from the clone.</p>")


def _bounded(v):
    """A field_validator for fields that mix str and int (parsed later by parse_duration /
    parse_size): reject an out-of-range int, leave strings and None alone."""
    if isinstance(v, int) and not isinstance(v, bool) and not (0 <= v <= _MAX_INT):
        raise ValueError(f"out of range (0..{_MAX_INT}): {v}")
    return v


class SubmitBody(BaseModel):
    command: str
    time: str | int
    cwd: str
    mem: str | int | None = None
    bid: int | None = Field(default=None, ge=0, le=_MAX_INT)
    preempt: bool = False
    preemptible: bool = True
    grace: str | int | None = None
    retries: int = Field(default=0, ge=0, le=_MAX_INT)
    name: str = ""
    note: str = ""
    tags: list[str] = []
    submitter: str = ""
    env: dict[str, str] | None = None
    target: str = "local"
    gpu: str | None = None
    env_keys: list[str] = []
    data: list[str] = []
    max_cost: float | None = Field(default=None, ge=0)

    _bounded_mixed = field_validator("time", "mem", "grace")(_bounded)


class PatchBody(BaseModel):
    bid: int | None = Field(default=None, ge=0, le=_MAX_INT)
    preempt: bool | None = None


class RestartBody(BaseModel):
    mem: str | int | None = None
    whole_gpu: bool = False
    time: str | int | None = None
    bid: int | None = Field(default=None, ge=0, le=_MAX_INT)
    retries: int | None = Field(default=None, ge=0, le=_MAX_INT)
    preempt: bool = False  # never carried over from the previous run

    _bounded_mixed = field_validator("time", "mem")(_bounded)


def _sse(data: dict, event: str | None = None) -> str:
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {json.dumps(data)}\n\n"


def _read_chunk(path: Path, offset: int, limit: int = 1 << 20) -> tuple[str, int]:
    if not path.exists():
        return "", offset
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read(limit)
    return data.decode("utf-8", errors="replace"), offset + len(data)


async def _wait_or_sleep(shutdown: asyncio.Event | None, seconds: float) -> None:
    """The generators below poll every `seconds`; when a `shutdown` event is given, wake up
    immediately once it's set instead of finishing out the poll interval."""
    if shutdown is None:
        await asyncio.sleep(seconds)
        return
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def _follow_logs(daemon: Daemon, job_id: int, path: Path, offset: int,
                        shutdown: asyncio.Event | None = None):
    """SSE body for `GET .../logs?follow=1`: log text as it's written, a `: keep-alive` comment
    every `KEEPALIVE_INTERVAL` of quiet (so a client's read timeout doesn't trip), and an `end`
    event once the job is done or `shutdown` is set (daemon shutting down). A module-level
    function (rather than a route-local closure) so it can be driven directly in tests without
    a live HTTP stream."""
    off = offset
    last_sent = time.monotonic()
    while True:
        if shutdown is not None and shutdown.is_set():
            yield _sse({}, event="end")
            return
        text, new_off = _read_chunk(path, off)
        if text:
            off = new_off
            yield _sse({"text": text, "offset": off})
            last_sent = time.monotonic()
        elif daemon.job(job_id).state in TERMINAL:
            yield _sse({}, event="end")
            return
        else:
            if time.monotonic() - last_sent >= KEEPALIVE_INTERVAL:
                yield ": keep-alive\n\n"
                last_sent = time.monotonic()
            await _wait_or_sleep(shutdown, 0.5)


async def _stream_updates(daemon: Daemon, snapshot, limit: int | None,
                           shutdown: asyncio.Event | None = None):
    """SSE body for `GET /api/stream`: one snapshot per state change, plus the same keep-alive
    comment as `_follow_logs` if the daemon goes quiet for a while. Ends as soon as `shutdown`
    is set (daemon shutting down)."""
    seen, sent = None, 0
    last_sent = time.monotonic()
    while limit is None or sent < limit:
        if shutdown is not None and shutdown.is_set():
            return
        if daemon.version != seen:
            seen = daemon.version
            yield _sse(snapshot())
            sent += 1
            last_sent = time.monotonic()
        elif time.monotonic() - last_sent >= KEEPALIVE_INTERVAL:
            yield ": keep-alive\n\n"
            last_sent = time.monotonic()
        await _wait_or_sleep(shutdown, 0.5)


def _default_allowed_hosts(cfg: Config) -> list[str]:
    """Host allowlist for TrustedHostMiddleware: what pasard binds to (from `cfg.addresses()`,
    stripped of ports and brackets), always localhost, plus any extra names from
    `allowed_hosts` in config.toml (e.g. a Tailscale DNS name)."""
    hosts = {"localhost", "127.0.0.1", "::1", "[::1]"}
    for addr in cfg.addresses():
        host, _, _ = addr.rpartition(":")
        hosts.add(host.strip("[]"))
    hosts.update(cfg.allowed_hosts)
    return sorted(hosts)


def create_app(daemon: Daemon, *, prom: Prometheus | None = None, wake=lambda: None,
               allowed_hosts: list[str] | None = None, webui_dir: Path | None = None,
               shutdown: asyncio.Event | None = None) -> FastAPI:
    app = FastAPI(title="pasar", version=__version__)
    cfg = daemon.cfg
    # `allowed_hosts` here is *extra* names on top of the production defaults below (e.g.
    # TestClient's "testserver"); it must never be used to widen what production allows.
    hosts = _default_allowed_hosts(cfg)
    if allowed_hosts:
        hosts = [*hosts, *allowed_hosts]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, exc: NotFound):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(Conflict)
    async def _conflict(request: Request, exc: Conflict):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def _invalid(request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    def view(job, now=None, proj=None):
        now = daemon.clock() if now is None else now
        proj = schedule_projection(daemon, now) if proj is None else proj
        return job_view(daemon, job, now, proj)

    def listed(now: float, include_all: bool = False, state: str | None = None):
        jobs = daemon.store.list_jobs([State(state)] if state else None)
        if include_all or state:
            return jobs
        out = []
        for j in jobs:
            att = daemon.store.current_attempt(j.id)
            ended = att.end_time if att and att.end_time else j.queue_time
            if j.state not in TERMINAL or now - ended < RECENT:
                out.append(j)
        return out

    def snapshot() -> dict:
        now = daemon.clock()
        proj = schedule_projection(daemon, now)
        return {"status": status_view(daemon, now, proj),
                "jobs": [job_view(daemon, j, now, proj) for j in listed(now)]}

    @app.post("/api/jobs", status_code=201)
    async def submit(body: SubmitBody):
        spec = JobSpec(
            command=body.command, est_runtime=parse_duration(body.time), cwd=body.cwd,
            mem_request=None if body.mem is None else parse_size(body.mem),
            bid=cfg.default_bid if body.bid is None else body.bid,
            preempt=body.preempt, preemptible=body.preemptible,
            grace=cfg.default_grace if body.grace is None else parse_duration(body.grace),
            retries=body.retries, name=body.name, note=body.note, tags=body.tags,
            submitter=body.submitter, env=body.env, target=body.target, gpu=body.gpu,
            env_keys=body.env_keys, data=body.data, max_cost=body.max_cost,
        )
        job = daemon.submit(spec)
        wake()
        return view(job)

    @app.get("/api/jobs")
    async def list_jobs(all: bool = False, state: str | None = None,
                        since: float | None = None, until: float | None = None):
        now = daemon.clock()
        proj = schedule_projection(daemon, now)
        if since is not None or until is not None:
            # Jobs that ran at some point in [since, until): the schedule chart's history.
            lo = since if since is not None else 0.0
            hi = until if until is not None else math.inf
            if math.isnan(lo) or math.isnan(hi) or hi <= lo:
                raise HTTPException(422, "need since < until")
            ids = daemon.store.job_ids_active_between(lo, hi)
            jobs = [j for j in (daemon.store.get_job(i) for i in ids) if j is not None]
            if state:
                jobs = [j for j in jobs if j.state == State(state)]
            return [job_view(daemon, j, now, proj) for j in jobs]
        return [job_view(daemon, j, now, proj) for j in listed(now, all, state)]

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: int):
        job = daemon.job(job_id)
        return {**view(job), "attempts": [attempt_view(a) for a in daemon.store.attempts(job_id)]}

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel(job_id: int):
        job = daemon.cancel(job_id)
        wake()
        return view(job)

    # Deliberately no CLI equivalent: approval is a web-UI-only action, and the agent guide
    # tells agents never to call this — a person is meant to see the price before it launches.
    # `extend=1` is the same verb aimed at a different job: a *running* attempt whose own pace
    # projects it past its approved run time (`daemon.needs_more_time`), raising its ceiling
    # instead of admitting it to the queue. See `Daemon._extend`.
    @app.post("/api/jobs/{job_id}/approve")
    async def approve(job_id: int, extend: bool = False):
        job = daemon.approve(job_id, extend=extend)
        wake()
        return view(job)

    @app.post("/api/jobs/{job_id}/reject")
    async def reject(job_id: int):
        job = daemon.reject(job_id)
        wake()
        return view(job)

    @app.patch("/api/jobs/{job_id}")
    async def patch(job_id: int, body: PatchBody):
        job = daemon.set_bid(job_id, body.bid, body.preempt)
        wake()
        return view(job)

    @app.post("/api/jobs/{job_id}/restart")
    async def restart(job_id: int, body: RestartBody | None = None):
        body = body or RestartBody()
        mem = UNSET
        if body.whole_gpu:
            mem = None
        elif body.mem is not None:
            mem = parse_size(body.mem)
        job = daemon.restart(
            job_id, mem_request=mem,
            est_runtime=None if body.time is None else parse_duration(body.time),
            bid=body.bid, retries=body.retries, preempt=body.preempt,
        )
        wake()
        return view(job)

    # Deliberately not "/api/jobs/sparks": that would sit under the "/api/jobs/{job_id}" path and
    # only work as long as it stayed declared first, which is a trap for whoever reorders next.
    @app.get("/api/sparks")
    async def sparks(ids: str = ""):
        """Sparkline data for many jobs at once, so the dashboard table costs one request instead
        of one per row. Jobs without a plottable metric are simply absent from the result."""
        parts = [p for p in ids.split(",") if p]
        if len(parts) > MAX_SPARK_IDS:
            raise HTTPException(422, f"at most {MAX_SPARK_IDS} ids")
        try:
            job_ids = [int(p) for p in parts]
        except ValueError:
            raise HTTPException(422, "ids must be a comma-separated list of job ids") from None
        return spark_view(daemon.store, job_ids)

    @app.get("/api/jobs/{job_id}/events")
    async def events(job_id: int):
        daemon.job(job_id)
        return daemon.store.events(job_id)

    @app.get("/api/jobs/{job_id}/metrics")
    async def job_metrics(job_id: int):
        daemon.job(job_id)
        return daemon.store.metric_summaries(job_id)

    @app.get("/api/jobs/{job_id}/usage")
    async def job_usage(job_id: int):
        daemon.job(job_id)
        return [[t, v] for t, v in daemon.usage_history.get(job_id, ())]

    @app.get("/api/jobs/{job_id}/logs")
    async def logs(job_id: int, offset: int = Query(0, ge=0), follow: bool = False):
        daemon.job(job_id)
        path = daemon.job_dir(job_id) / "output.log"
        if not follow:
            text, new_offset = _read_chunk(path, offset)
            return {"text": text, "offset": new_offset}
        return StreamingResponse(_follow_logs(daemon, job_id, path, offset, shutdown=shutdown),
                                 media_type="text/event-stream")

    @app.get("/api/status")
    async def status():
        now = daemon.clock()
        return status_view(daemon, now, schedule_projection(daemon, now))

    @app.get("/api/cloud")
    async def cloud():
        now = daemon.clock()
        return cloud_status_view(daemon, now, schedule_projection(daemon, now))

    @app.get("/api/gpu")
    async def gpu(minutes: int = 30):
        if prom is None:
            return {"power_w": [], "temp_c": [], "util_pct": []}
        return await asyncio.to_thread(prom.gpu_series, minutes, daemon.clock())

    @app.get("/api/stream")
    async def stream(limit: int | None = None):
        return StreamingResponse(_stream_updates(daemon, snapshot, limit, shutdown=shutdown),
                                 media_type="text/event-stream")

    @app.get("/api/mascot")
    async def mascot_manifest():
        return manifest(Path(cfg.mascot_dir))

    @app.get("/mascot/builtin/{filename}")
    async def mascot_builtin(filename: str):
        path = resolve_builtin(filename)
        if path is None:
            raise HTTPException(404)
        return FileResponse(path, headers={"Cache-Control": "max-age=300"})

    @app.get("/mascot/{filename}")
    async def mascot_file(filename: str):
        path = resolve(Path(cfg.mascot_dir), filename)
        if path is None:
            raise HTTPException(404)
        return FileResponse(path, headers={"Cache-Control": "max-age=300"})

    @app.get("/llms.txt")
    async def llms_txt():
        return PlainTextResponse(load_guide())

    ui = WEBUI_DIR if webui_dir is None else webui_dir
    assets = (ui / "assets").resolve()

    @app.get("/assets/{name:path}")
    async def asset(name: str):
        path = (assets / name).resolve()
        if not path.is_relative_to(assets) or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith(("api/", "mascot/")) or path in ("api", "mascot"):
            raise HTTPException(404)
        index = ui / "index.html"
        if not index.is_file():
            return HTMLResponse(_UNBUILT, status_code=503)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app
