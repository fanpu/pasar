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


_UNKNOWN = object()
"""Default for the `pace` argument below: `None` is a real answer ("on pace"), so a caller that
already has an answer has to be distinguishable from one that has none."""


def persist_view(daemon, job: Job) -> dict | None:
    """What a finished cloud job left in its persist dir, where it went, and when what is still
    at the provider goes; `None` until the job is `TERMINAL`, while that dir is still the live
    checkpoint an attempt resumes from. From pasard's own records only — the pulls, the sweep,
    when the job finished — and never the provider, which a view must not wait on.

    `files`/`bytes`/`pulled_at`/`pulled_to`/`remote_deleted` are the latest pull that landed
    anything (all `None`, and `False`, when none has). `remote_bytes` is what the provider held
    when a pull last measured it — 0 once a pull deleted it or the sweep did, `None` when no
    pull has measured it. `swept_at`/`swept_bytes` say the retention sweep ran and what it
    deleted (0 bytes when a pull had already emptied it). `sweeps_at` is when that sweep may
    delete what is left — `cloud_retention_days` after the job finished — and is `None` once
    nothing is (the landed pull deleted the remote copy, or the sweep already ran, or the job
    never launched and so never had anything there).
    `last_error` is the latest pull's own error, if it had one: why a pull was skipped or failed,
    or that a verified pull could not then delete the remote copy."""
    if job.state not in TERMINAL:
        return None
    pulls = daemon.store.pulls(job.id)
    # The same rows `Store.last_pull` reads, taken in one query rather than one per question.
    landed = next((p for p in reversed(pulls) if p["files"]), {})
    last = pulls[-1] if pulls else {}
    measured = next((p for p in reversed(pulls) if p["remote_bytes"] is not None), None)
    swept = daemon.store.cloud_swept(job.id)
    remote_deleted = bool(landed.get("remote_deleted"))
    # A job rejected or cancelled before it ever launched never had a persist dir at all.
    never_ran = daemon.store.current_attempt(job.id) is None
    gone = remote_deleted or swept is not None or never_ran
    remote_bytes = 0 if gone else measured and measured["remote_bytes"]
    return {
        "files": landed.get("files"),
        "bytes": landed.get("bytes"),
        "pulled_at": landed.get("ts"),
        "pulled_to": landed.get("dest"),
        "remote_deleted": remote_deleted,
        "remote_bytes": remote_bytes,
        "swept_at": swept["ts"] if swept else None,
        "swept_bytes": swept["bytes"] if swept else None,
        "sweeps_at": None if gone else daemon.sweeps_at(job.id),
        "last_error": last.get("error"),
    }


def cloud_view(daemon, job: Job, pace=_UNKNOWN) -> dict | None:
    """The `cloud` object embedded in a cloud job's view; `None` for a job that ran locally.

    `estimated_cost`/`max_cost` show the ceiling that actually governs the job right now: the
    approved figures once an approval covers the attempt that's queued or running, live-priced
    numbers otherwise — before the first approval, or once a price rise has sent the job back to
    `awaiting` (reason `price_rose`) and it needs approving again. In the `price_rose` case the
    old ceiling that was breached is still in `job.summary`, alongside the new price. `max_cost`
    is the *effective* ceiling: when the submitter's own `--max-cost` is lower than the ceiling
    the target's rates would otherwise buy, `max_cost` is that lower figure and `user_capped` is
    `True`; when what is left of the job's lifetime cap is lower still, `max_cost` is that.

    `job_cap` is the most this job may spend over its whole life (the target's `max_job_cost`,
    raised only in pasard's config), and `job_spent` what its attempts have spent so far: settled
    figures for attempts that ended, and the full reservation of one still running, since it may
    yet bill all of it. `job_cap` is `None` for a job whose target is no longer configured.

    `approved_seconds`/`full_seconds` are how long one approval buys and how long it would buy
    without its dollar caps (`--max-cost`, the job cap): a dollar cap is enforced as a shorter run
    (the daemon pauses the attempt when the cap's dollars are spent), so a capped job gets less
    time, and these two say how much less. They are priced live, like `max_cost`, so they say
    what the daemon would do now.

    A price rise is *not* measured against `max_cost` — the approvals row keeps the hourly rate
    for that, so that a cap (which never moves) cannot hide a rate that did. See
    `Daemon._over_the_approval`.

    `phase` is where the *attempt* the daemon is currently watching has got to, and nothing else:
    one of the executor's own Phase/result strings — `pending`, `starting`, `running`, `success`,
    `exit-code`, `stopped`, `reclaimed`, `time_limit`, `signal` — or `None` when there is no live
    attempt to report on, which covers a job that has not started one yet and one whose attempt
    has ended. Where the *job* has got to is `state`, on the job view itself; the two are
    deliberately separate fields, because they share words (both can say `running`) while meaning
    different things, and one field carrying whichever was available left a consumer no way to
    tell which it had been handed.

    `console_url` is only ever populated while the daemon is actively polling the attempt (i.e.
    while it's the current entry in `daemon.cloud_units`); pasar doesn't persist it, so it reads
    as `None` once an attempt has ended, even if the provider's own console still works.

    `persist` is what the job left behind once it finished: see `persist_view`."""
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
    window = daemon.cloud_window(job)
    approved_seconds, full_seconds = window if window else (None, None)
    target = daemon.cfg.clouds.get(job.spec.target)
    unit = daemon.cloud_units.get(job.id)
    return {
        "target": job.spec.target,
        "gpu": job.spec.gpu,
        "phase": unit.result if unit is not None else None,
        "estimated_cost": estimated_cost,
        "max_cost": max_cost,
        "user_capped": user_capped,
        "approved_seconds": approved_seconds,
        "full_seconds": full_seconds,
        "job_cap": target.max_job_cost if target is not None else None,
        "job_spent": daemon.ledger.job_spent(job.id),
        "console_url": unit.console_url if unit is not None else None,
        # Extra seconds this attempt's own pace (see `pasar.cloud.pace.needs_more_time`) projects
        # past what was approved; `None` while running on pace or before enough of it has been
        # observed. Recomputed fresh on every view rather than stored, so it clears itself the
        # moment either the pace recovers or a person extends the ceiling far enough to cover it
        # — there is nothing to reset by hand. A caller that has already asked (and filtered on
        # the answer) passes it in rather than paying for the same handful of queries twice.
        "needs_more_time": daemon.needs_more_time(job) if pace is _UNKNOWN else pace,
        "persist": persist_view(daemon, job),
    }


def job_view(daemon, job: Job, now: float, projection: dict, pace=_UNKNOWN) -> dict:
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
        "cloud": cloud_view(daemon, job, pace),
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
    """Everything `GET /api/cloud` and `pasar cloud` show: each configured target's budget, the
    most one job may spend on it over its whole life (`max_job_cost`), and its spend
    (`spent_today`/`spent_month` already include what's `committed` from jobs still running, the
    same total the budget gate in `cloud.lane` checks against), its live rates, its GPUs as clean
    rows (`gpus`: name as `--gpu` accepts it, live $/hour, memory in GB, cheapest first — see
    `Daemon.cloud_gpus`), every job currently waiting on a person to approve it, and every
    *running* job whose own pace projects it past its approved run time (`needs_time`) — a
    different tray from `awaiting`,
    because these jobs are not paused and do not need to be: they keep running on the time they
    already have unless a person extends them (`POST /api/jobs/{id}/approve?extend=1`).

    `known_stored_bytes`/`known_stored_jobs` are what finished jobs are known to have left on the
    target's volume: the last measurement a pull took of each whose remote copy neither a pull
    nor the retention sweep has deleted yet. A floor from pasard's records, not a live size:
    running and paused jobs' checkpoints are there too, uncounted, and a provider bills storage
    whether or not anything is running."""
    held: dict[str, list[int]] = {}
    for job_id, size in daemon.store.held_remote().items():
        job = daemon.store.get_job(job_id)
        if job is not None:
            held.setdefault(job.spec.target, []).append(size)
    targets = []
    for name, target in sorted(daemon.cfg.clouds.items()):
        committed = daemon.ledger.committed(name)
        running = sum(1 for j in daemon.store.list_jobs(ACTIVE) if j.spec.target == name)
        targets.append({
            "name": name,
            "provider": target.provider,
            # Whose account pays for this target, for the web UI's approve dialog to say so.
            "owner": target.owner,
            "configured": name in daemon.executors,
            "daily_budget": target.daily_budget,
            "monthly_budget": target.monthly_budget,
            "spent_today": daemon.ledger.settled_day(name) + committed,
            "spent_month": daemon.ledger.settled_month(name) + committed,
            "committed": committed,
            "max_running": target.max_running,
            "max_job_cost": target.max_job_cost,
            "running": running,
            "rates": daemon.cloud_rates(target),
            "known_stored_bytes": sum(held.get(name, [])),
            "known_stored_jobs": len(held.get(name, [])),
            "gpus": [{"name": g.name, "hourly_rate": g.hourly_rate, "memory_gb": g.memory_gb}
                     for g in daemon.cloud_gpus(target)],
        })
    awaiting = [job_view(daemon, j, now, projection)
                for j in daemon.store.list_jobs([State.AWAITING])]
    # One `needs_more_time` per running job, not two: it is several queries deep (the approvals
    # row, the attempts, the attempt's progress events), the filter and the view want the same
    # answer, and this view is what `pasar cloud` polls.
    needs_time = []
    for j in daemon.store.list_jobs([State.RUNNING]):
        pace = daemon.needs_more_time(j)
        if pace is not None:
            needs_time.append(job_view(daemon, j, now, projection, pace))
    return {"targets": targets, "awaiting": awaiting, "needs_time": needs_time}


RECENT_CLOUD_LIMIT = 5


def _recent_cloud_jobs(daemon, now: float, projection: dict, limit: int = RECENT_CLOUD_LIMIT):
    """Up to `limit` most recently finished cloud jobs, newest first. `cloud_finished` (stamped
    the moment a cloud job goes terminal — see `Daemon._cloud_ended`) is the source of which jobs
    to even look at, so this never walks every local job ever run; each one's own end time is its
    current attempt's `end_time` where that survived, else the stamp itself (a job cancelled or
    rejected while awaiting approval never ran an attempt at all)."""
    candidates: list[tuple[float, Job]] = []
    for job_id, ts in daemon.store.cloud_finished().items():
        job = daemon.store.get_job(job_id)
        if job is None or job.state not in TERMINAL or job.spec.target == LOCAL:
            continue
        att = daemon.store.current_attempt(job_id)
        ended = att.end_time if att and att.end_time else ts
        candidates.append((ended, job))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return [job_view(daemon, job, now, projection) for _, job in candidates[:limit]]


def cloud_snapshot_view(daemon, now: float, projection: dict) -> dict | None:
    """The `cloud` key in the live snapshot (`GET /api/stream`, `snapshot()` in api.py): `None`
    when no cloud target is configured, so a plain local box never carries an empty cloud block
    around. Otherwise everything `cloud_status_view` already reports, plus `recent`, the most
    recently finished cloud jobs — reusing `cloud_status_view` rather than rebuilding targets,
    `awaiting` and `needs_time` a second way.

    Kept cheap on purpose: the snapshot is rebuilt on every version bump, so nothing here may call
    a provider except through `cloud_rates`/`cloud_gpus`, both already cached for
    `CLOUD_RATE_TTL` seconds (see `Daemon.cloud_rates`)."""
    if not daemon.cfg.clouds:
        return None
    view = cloud_status_view(daemon, now, projection)
    view["recent"] = _recent_cloud_jobs(daemon, now, projection)
    return view
