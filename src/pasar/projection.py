"""Forward-simulate the scheduler with estimated runtimes to project start/end times."""

from dataclasses import replace

from pasar.scheduler import Queued, Running, decide

MIN_REMAINING = 60.0


def project(
    queued: list[Queued],
    running: list[Running],
    remaining: dict[int, float],
    queue_times: dict[int, float],
    capacity: int,
    now: float,
    horizon: float = 7 * 86400,
) -> dict[int, list[tuple[float, float]]]:
    segments: dict[int, list[tuple[float, float]]] = {}
    rem = {j: max(MIN_REMAINING, v) for j, v in remaining.items()}
    queue = {q.job_id: q for q in queued}
    run: dict[int, Running] = {}
    starts: dict[int, float] = {}
    ends: dict[int, float] = {}
    for job in queued:
        segments[job.job_id] = []
    for r in running:
        if r.stopping:
            continue
        segments[r.job_id] = []
        run[r.job_id] = r
        starts[r.job_id] = r.start
        ends[r.job_id] = now + rem[r.job_id]
    t = now
    for _ in range(10_000):
        used = sum(r.charge for r in run.values())
        d = decide([replace(q, duration=rem[j]) for j, q in queue.items()],
                   [replace(r, end=ends[j]) for j, r in run.items()], capacity - used, t)
        if d.preempt:
            for j in d.preempt:
                r = run.pop(j)
                segments[j].append((starts[j], t))
                rem[j] = max(MIN_REMAINING, ends[j] - t)
                queue[j] = Queued(j, r.bid, queue_times.get(j, starts[j]), r.charge, r.preemptible,
                                  r.preempt)
            continue
        for j in d.launch:
            q = queue.pop(j)
            run[j] = Running(j, q.bid, t, q.need, q.preemptible, preempt=q.preempt)
            starts[j] = t
            ends[j] = t + rem[j]
        if not run:
            break
        t = min(ends[j] for j in run)
        if t > now + horizon:
            break
        for j in [j for j in run if ends[j] <= t]:
            segments[j].append((starts[j], ends[j]))
            del run[j]
    for j in run:
        segments[j].append((starts[j], ends[j]))
    return segments
