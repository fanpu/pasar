"""HTTP API. Handlers are async so they run on the daemon's event loop, never racing a tick."""

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from pasar import __version__
from pasar.daemon import UNSET, Conflict, Daemon, NotFound
from pasar.metrics import Prometheus
from pasar.models import TERMINAL, JobSpec, State
from pasar.units import parse_duration, parse_size
from pasar.views import attempt_view, job_view, schedule_projection, status_view

RECENT = 86400


class SubmitBody(BaseModel):
    command: str
    time: str | int
    cwd: str
    mem: str | int | None = None
    bid: int | None = None
    preemptible: bool = True
    grace: str | int | None = None
    retries: int = 0
    name: str = ""
    note: str = ""
    tags: list[str] = []
    submitter: str = ""
    env: dict[str, str] | None = None


class PatchBody(BaseModel):
    bid: int


class RestartBody(BaseModel):
    mem: str | int | None = None
    whole_gpu: bool = False
    time: str | int | None = None
    bid: int | None = None
    retries: int | None = None


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


def create_app(daemon: Daemon, *, prom: Prometheus | None = None, wake=lambda: None) -> FastAPI:
    app = FastAPI(title="pasar", version=__version__)
    cfg = daemon.cfg

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
            preemptible=body.preemptible,
            grace=cfg.default_grace if body.grace is None else parse_duration(body.grace),
            retries=body.retries, name=body.name, note=body.note, tags=body.tags,
            submitter=body.submitter, env=body.env,
        )
        job = daemon.submit(spec)
        wake()
        return view(job)

    @app.get("/api/jobs")
    async def list_jobs(all: bool = False, state: str | None = None):
        now = daemon.clock()
        proj = schedule_projection(daemon, now)
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

    @app.patch("/api/jobs/{job_id}")
    async def patch(job_id: int, body: PatchBody):
        job = daemon.set_bid(job_id, body.bid)
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
            bid=body.bid, retries=body.retries,
        )
        wake()
        return view(job)

    @app.get("/api/jobs/{job_id}/events")
    async def events(job_id: int):
        daemon.job(job_id)
        return daemon.store.events(job_id)

    @app.get("/api/jobs/{job_id}/metrics")
    async def job_metrics(job_id: int):
        daemon.job(job_id)
        return daemon.store.metric_summaries(job_id)

    @app.get("/api/jobs/{job_id}/logs")
    async def logs(job_id: int, offset: int = 0, follow: bool = False):
        daemon.job(job_id)
        path = daemon.job_dir(job_id) / "output.log"
        if not follow:
            text, new_offset = _read_chunk(path, offset)
            return {"text": text, "offset": new_offset}

        async def gen():
            off = offset
            while True:
                text, new_off = _read_chunk(path, off)
                if text:
                    off = new_off
                    yield _sse({"text": text, "offset": off})
                elif daemon.job(job_id).state in TERMINAL:
                    yield _sse({}, event="end")
                    return
                else:
                    await asyncio.sleep(0.5)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/status")
    async def status():
        now = daemon.clock()
        return status_view(daemon, now, schedule_projection(daemon, now))

    @app.get("/api/gpu")
    async def gpu(minutes: int = 30):
        if prom is None:
            return {"power_w": [], "temp_c": [], "util_pct": []}
        return await asyncio.to_thread(prom.gpu_series, minutes, daemon.clock())

    @app.get("/api/stream")
    async def stream(limit: int | None = None):
        async def gen():
            seen, sent = None, 0
            while limit is None or sent < limit:
                if daemon.version != seen:
                    seen = daemon.version
                    yield _sse(snapshot())
                    sent += 1
                await asyncio.sleep(0.5)

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app
