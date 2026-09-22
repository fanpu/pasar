"""One scheduling pass: which queued jobs start, and which running jobs to preempt.

Bids only order the queue. A queued job stops running jobs only if it was submitted with
`preempt`, and then only preemptible ones with a lower bid. When the first job in line that
can't start doesn't fit, it holds a reservation: the time enough running jobs are projected to
have ended. Jobs behind it still start in free memory, but only if that can't delay it: they fit
beside it once it starts, or they are projected to finish before then.
"""

import math
from dataclasses import dataclass, field

INF = math.inf


@dataclass(frozen=True)
class Queued:
    job_id: int
    bid: int
    queue_time: float
    need: int
    preemptible: bool
    preempt: bool = False  # may stop lower-bid preemptible jobs to start now
    duration: float = INF  # expected run time; unknown never slips in ahead of a reservation


@dataclass(frozen=True)
class Running:
    job_id: int
    bid: int
    start: float
    charge: int  # max(reservation, actual usage)
    preemptible: bool
    stopping: bool = False
    end: float = INF  # projected end
    preempt: bool = False  # the job's own flag, kept if it is preempted and requeued


@dataclass
class Decision:
    launch: list[int] = field(default_factory=list)
    preempt: list[int] = field(default_factory=list)
    waiting: list[int] = field(default_factory=list)  # start once stopping jobs exit
    blocked: list[int] = field(default_factory=list)


def order_queue(queued: list[Queued]) -> list[Queued]:
    return sorted(queued, key=lambda q: (-q.bid, q.queue_time, q.job_id))


def reservation(need: int, free: int, running: list[Running]) -> tuple[float, int] | None:
    """When `need` fits if nothing else starts, and how much memory is spare then; None if it
    never fits."""
    mem = free
    for end, charge in sorted((r.end, r.charge) for r in running):
        mem += charge
        if mem >= need:
            return end, mem - need
    return None


def decide(queued: list[Queued], running: list[Running], free: int, now: float = 0.0) -> Decision:
    d = Decision()
    avail = free
    incoming = sum(r.charge for r in running if r.stopping)  # freed once they exit
    taken: set[int] = set()
    started: list[Running] = []
    held: tuple[float, int] | None = None  # (reserved start, spare memory then)
    for q in order_queue(queued):
        if held is not None:
            start, spare = held
            finishes_first = now + q.duration <= start
            if q.need <= avail and (finishes_first or q.need <= spare):
                d.launch.append(q.job_id)
                avail -= q.need
                if not finishes_first:
                    held = (start, spare - q.need)
            else:
                d.blocked.append(q.job_id)
            continue
        if q.need <= avail:
            d.launch.append(q.job_id)
            avail -= q.need
            started.append(Running(q.job_id, q.bid, now, q.need, q.preemptible,
                                   end=now + q.duration))
            continue
        shortfall = q.need - avail
        if shortfall <= incoming:
            incoming -= shortfall
            avail = 0
            d.waiting.append(q.job_id)
            continue
        if q.preempt:
            candidates = sorted(
                (r for r in running
                 if not r.stopping and r.job_id not in taken and r.preemptible and r.bid < q.bid),
                key=lambda r: (r.bid, -r.start),
            )
            victims, freed = [], 0
            for r in candidates:
                if incoming + freed >= shortfall:
                    break
                victims.append(r)
                freed += r.charge
            if incoming + freed >= shortfall:
                d.preempt += [v.job_id for v in victims]
                taken.update(v.job_id for v in victims)
                incoming = incoming + freed - shortfall
                avail = 0
                d.waiting.append(q.job_id)
                continue
        d.blocked.append(q.job_id)
        others = [r for r in running if not r.stopping and r.job_id not in taken] + started
        held = reservation(q.need, avail + incoming, others)
    return d
