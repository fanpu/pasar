"""How long jobs have run and how long they have left: from progress reports, else `--time`."""

from pasar.models import Attempt


def run_time(attempts: list[Attempt], now: float) -> float:
    return sum((a.end_time if a.end_time is not None else now) - a.start_time for a in attempts)


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def resume_step(store, job_id: int, att: Attempt) -> float | None:
    """The step an attempt started from: 0 for the first attempt; later ones resume from the step
    they reported in `resumed`, else the last checkpoint of an earlier attempt, else (unknown)
    their own first progress report."""
    if att.n == 1:
        return 0
    for event in (store.last_event(job_id, "resumed", attempt=att.n),
                  store.last_event_before(job_id, "checkpoint", att.n),
                  store.first_event(job_id, "progress", att.n)):
        if event and _number(event["step"]):
            return event["step"]
    return None


def progress_remaining(store, job_id: int, atts: list[Attempt], now: float) -> float | None:
    """Seconds left for a running job, from its progress reports: the attempt so far took
    `elapsed` for `done` steps, so the rest takes `elapsed × (total − step) ÷ done`, counted from
    the latest report. Startup is amortised into the rate. None when the job hasn't reported
    `total_steps` or any steps yet this attempt (callers then fall back to `--time`)."""
    if not atts or atts[-1].end_time is not None:
        return None
    att = atts[-1]
    p = store.last_event(job_id, "progress", attempt=att.n)
    if p is None:
        return None
    step, total = p["payload"].get("step"), p["payload"].get("total_steps")
    base = resume_step(store, job_id, att)
    if not (_number(step) and _number(total) and total > 0) or base is None:
        return None
    done, elapsed = step - base, p["ts"] - att.start_time
    if done <= 0 or elapsed <= 0:
        return None
    return max(0.0, total - step) * elapsed / done - (now - p["ts"])


def remaining_time(store, job, atts: list[Attempt], now: float) -> tuple[float, str]:
    """Seconds left and where that figure comes from: `"progress"` or the `"estimate"`."""
    left = progress_remaining(store, job.id, atts, now)
    if left is not None:
        return left, "progress"
    return job.spec.est_runtime - run_time(atts, now), "estimate"
