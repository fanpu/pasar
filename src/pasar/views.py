"""JSON-ready views of jobs and machine status, shared by the API and the CLI."""

from dataclasses import asdict

from pasar.daemon import ACTIVE, LOCAL
from pasar.eta import remaining_time, run_time
from pasar.models import TERMINAL, Attempt, EndKind, Job, State
from pasar.projection import project


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


SPARK_POINTS = 32  # most points a dashboard-row sparkline is drawn from
# Bookkeeping fields a progress report carries alongside its metrics; never plottable.
SPARK_SKIP = frozenset({"step", "total_steps", "ts"})


def _spark_key(keys: set[str]) -> str | None:
    """The one metric a dashboard row plots: `loss` when the job reports it, else the first
    alphabetically. `web/src/lib/series.ts` orders the detail panel's charts the same way."""
    if not keys:
        return None
    return "loss" if "loss" in keys else min(keys)


def _thin(points: list[list[float]], limit: int) -> list[list[float]]:
    """Evenly spaced sample of at most `limit` points, always keeping the first and the last so
    the sparkline still starts and ends where the run did."""
    if len(points) <= limit:
        return points
    last = len(points) - 1
    return [points[round(i * last / (limit - 1))] for i in range(limit)]


def spark_view(store, job_ids: list[int]) -> dict[int, dict]:
    """Per-job sparkline data for the dashboard table: one metric each, already thinned. Jobs
    that never reported a numeric metric are left out."""
    out: dict[int, dict] = {}
    for job_id, events in store.progress_events(job_ids).items():
        keys = {k for e in events for k, v in e["payload"].items()
                if k not in SPARK_SKIP and isinstance(v, (int, float))
                and not isinstance(v, bool)}
        key = _spark_key(keys)
        if key is None:
            continue
        points = [[e["ts"], float(e["payload"][key])] for e in events
                  if isinstance(e["payload"].get(key), (int, float))
                  and not isinstance(e["payload"].get(key), bool)]
        if not points:
            continue
        out[job_id] = {"key": key, "points": _thin(points, SPARK_POINTS),
                       "latest": points[-1][1]}
    return out


def schedule_projection(daemon, now: float) -> dict[int, list[tuple[float, float]]]:
    queued, running = daemon.snapshot()
    remaining = {q.job_id: q.duration for q in queued}
    remaining |= {r.job_id: r.end - now for r in running}
    queue_times = {q.job_id: q.queue_time for q in queued}
    queue_times |= {r.job_id: daemon.job(r.job_id).queue_time for r in running}
    return project(queued, running, remaining, queue_times, daemon.pool - daemon.external, now)


def attempt_view(a: Attempt) -> dict:
    d = asdict(a)
    d["end_kind"] = a.end_kind.value if a.end_kind else None
    return d


def cloud_view(daemon, job: Job) -> dict | None:
    """The `cloud` object embedded in a cloud job's view; `None` for a job that ran locally.

    `estimated_cost`/`max_cost` show the ceiling that actually governs the job right now: the
    approved figures once an approval covers the attempt that's queued or running, live-priced
    numbers otherwise — before the first approval, or once a price rise has sent the job back to
    `awaiting` (reason `price_rose`) and it needs approving again. In the `price_rose` case the
    old ceiling that was breached is still in `job.summary`, alongside the new price. `max_cost`
    is the *effective* ceiling: when the submitter's own `--max-cost` is lower than the ceiling
    the target's rates would otherwise buy, `max_cost` is that lower figure (and `user_capped` is
    `True`) — never the bigger, uncapped number, which would misstate what a price rise is
    actually compared against.

    `phase` carries one of two different vocabularies depending on whether the daemon is
    currently watching a live attempt for this job (i.e. `daemon.cloud_units` has an entry for
    it): while it does, `phase` is the executor's own Phase/result string — one of `pending`,
    `starting`, `running`, `success`, `exit-code`, `stopped`, `reclaimed`, `time_limit`, `signal`.
    Once the attempt ends (or before one has ever started), `daemon.cloud_units` has nothing for
    this job and `phase` falls back to the job's own `state` — `awaiting`, `queued`, `failed`,
    `completed`, etc. A consumer switching on `phase` has to handle both vocabularies; there is no
    separate field marking which one is in play, so treat any value not in the executor list above
    as a job state instead.

    `console_url` is only ever populated while the daemon is actively polling the attempt (i.e.
    while it's the current entry in `daemon.cloud_units`); pasar doesn't persist it, so it reads
    as `None` once an attempt has ended, even if the provider's own console still works."""
    if job.spec.target == LOCAL:
        return None
    attempts = daemon.store.attempts(job.id)
    cur = attempts[-1] if attempts else None
    active = cur is not None and cur.end_time is None
    n = cur.n if active else len(attempts) + 1
    approved = next((a for a in daemon.store.approvals(job.id) if a["attempt"] == n), None)
    if approved is not None and job.state != State.AWAITING:
        estimated_cost, max_cost = approved["estimated_cost"], approved["max_cost"]
    else:
        live = daemon.cloud_estimate(job)
        estimated_cost, max_cost = live if live else (None, None)
    user_capped = (job.spec.max_cost is not None and max_cost is not None
                  and abs(max_cost - job.spec.max_cost) < 1e-6)
    unit = daemon.cloud_units.get(job.id)
    return {
        "target": job.spec.target,
        "gpu": job.spec.gpu,
        "phase": unit.result if unit is not None else job.state.value,
        "estimated_cost": estimated_cost,
        "max_cost": max_cost,
        "user_capped": user_capped,
        "console_url": unit.console_url if unit is not None else None,
    }


def job_view(daemon, job: Job, now: float, projection: dict) -> dict:
    atts = daemon.store.attempts(job.id)
    cur = atts[-1] if atts else None
    limit = daemon.limit(job)
    usage = daemon.usage.get(job.id)
    ran = run_time(atts, now)
    left, eta_source = remaining_time(daemon.store, job, atts, now)
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
        "remaining": max(0.0, left),
        "expected_runtime": ran + max(0.0, left),
        "eta_source": eta_source,
        "preemptible": job.spec.preemptible,
        "preempt": job.spec.preempt,
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
        "spans": [[a.start_time, a.end_time, a.end_kind.value if a.end_kind else None]
                  for a in atts],
        "cloud": cloud_view(daemon, job),
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


def cloud_status_view(daemon, now: float, projection: dict) -> dict:
    """Everything `GET /api/cloud` and `pasar cloud` show: each configured target's budget and
    spend (`spent_today`/`spent_month` already include what's `committed` from jobs still
    running, the same total the budget gate in `cloud.lane` checks against), its live rates, and
    every job currently waiting on a person to approve it, across all targets."""
    targets = []
    for name, target in sorted(daemon.cfg.clouds.items()):
        committed = daemon.ledger.committed(name)
        running = sum(1 for j in daemon.store.list_jobs(ACTIVE) if j.spec.target == name)
        targets.append({
            "name": name,
            "provider": target.provider,
            "configured": name in daemon.executors,
            "daily_budget": target.daily_budget,
            "monthly_budget": target.monthly_budget,
            "spent_today": daemon.ledger.settled_day(name) + committed,
            "spent_month": daemon.ledger.settled_month(name) + committed,
            "committed": committed,
            "max_running": target.max_running,
            "running": running,
            "rates": daemon.cloud_rates(target),
        })
    awaiting = [job_view(daemon, j, now, projection)
                for j in daemon.store.list_jobs([State.AWAITING])]
    return {"targets": targets, "awaiting": awaiting}
