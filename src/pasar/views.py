"""JSON-ready views of jobs and machine status, shared by the API and the CLI."""

from dataclasses import asdict

from pasar.daemon import ACTIVE
from pasar.models import TERMINAL, Attempt, EndKind, Job
from pasar.projection import project


def run_time(attempts: list[Attempt], now: float) -> float:
    return sum((a.end_time if a.end_time is not None else now) - a.start_time for a in attempts)


def lost_time(attempts: list[Attempt]) -> dict:
    lost = {"preemption": 0.0, "failure": 0.0}
    known = True
    for i, a in enumerate(attempts):
        if a.end_kind not in (EndKind.PREEMPTED, EndKind.FAILED):
            continue
        key = "preemption" if a.end_kind == EndKind.PREEMPTED else "failure"
        if a.wasted_work is None:
            known = False
        else:
            lost[key] += a.wasted_work
        if i + 1 < len(attempts) and attempts[i + 1].restart_cost is not None:
            lost[key] += attempts[i + 1].restart_cost
    return {**lost, "known": known}


def schedule_projection(daemon, now: float) -> dict[int, list[tuple[float, float]]]:
    queued, running = daemon.snapshot()
    remaining, queue_times = {}, {}
    for job_id in [q.job_id for q in queued] + [r.job_id for r in running]:
        job = daemon.job(job_id)
        remaining[job_id] = job.spec.est_runtime - run_time(daemon.store.attempts(job_id), now)
        queue_times[job_id] = job.queue_time
    return project(queued, running, remaining, queue_times, daemon.pool - daemon.external, now)


def attempt_view(a: Attempt) -> dict:
    d = asdict(a)
    d["end_kind"] = a.end_kind.value if a.end_kind else None
    return d


def job_view(daemon, job: Job, now: float, projection: dict) -> dict:
    atts = daemon.store.attempts(job.id)
    cur = atts[-1] if atts else None
    limit = daemon.limit(job)
    usage = daemon.usage.get(job.id)
    ran = run_time(atts, now)
    progress = daemon.store.last_event(job.id, "progress")
    ckpt = daemon.store.last_event(job.id, "checkpoint")
    active = job.state in ACTIVE
    return {
        "id": job.id,
        "name": job.spec.name,
        "state": job.state.value,
        "reason": job.reason,
        "summary": job.summary,
        "stop_requested": job.stop_requested,
        "bid": job.bid,
        "command": job.spec.command,
        "cwd": job.spec.cwd,
        "note": job.spec.note,
        "tags": job.spec.tags,
        "submitter": job.spec.submitter,
        "git_commit": job.git_commit,
        "mode": "whole" if job.spec.mem_request is None else "shared",
        "mem_request": job.spec.mem_request,
        "limit": limit,
        "usage": usage,
        "over_limit": usage is not None and usage > limit,
        "peak": max((a.peak_mem for a in atts), default=0),
        "est_runtime": job.spec.est_runtime,
        "run_time": ran,
        "remaining": max(0.0, job.spec.est_runtime - ran),
        "preemptible": job.spec.preemptible,
        "grace": job.spec.grace,
        "retries": job.spec.retries,
        "retries_used": job.retries_used,
        "submit_time": job.submit_time,
        "queue_time": job.queue_time,
        "start_time": cur.start_time if cur and active else None,
        "end_time": cur.end_time if cur and job.state in TERMINAL else None,
        "attempts": len(atts),
        "preemptions": sum(a.end_kind == EndKind.PREEMPTED for a in atts),
        "lost": lost_time(atts),
        "progress": progress and {"step": progress["payload"].get("step"),
                                  "total_steps": progress["payload"].get("total_steps"),
                                  "ts": progress["ts"]},
        "last_checkpoint": ckpt and {"step": ckpt["step"], "ts": ckpt["ts"]},
        "projected": [list(s) for s in projection.get(job.id, [])],
    }


def status_view(daemon, now: float, projection: dict) -> dict:
    _, running = daemon.snapshot()
    reserved = sum(r.charge for r in running)
    return {
        "now": now,
        "version": daemon.version,
        "mem_total": daemon.sample.mem_total,
        "mem_available": daemon.sample.mem_available,
        "psi_some_avg10": daemon.sample.psi_some_avg10,
        "pool": daemon.pool,
        "reserved": reserved,
        "external": daemon.external,
        "free": daemon.free(running),
        "pressure_since": daemon.watchdog.pressure_since,
        "blocked": daemon.decision.blocked,
        "waiting": daemon.decision.waiting,
        "machine_events": daemon.store.machine_events(20),
        "hot_temp_c": daemon.cfg.hot_temp_c,
        "grafana_url": daemon.cfg.grafana_url,
    }
