"""One scheduling pass: which queued jobs start, and which running jobs to preempt."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Queued:
    job_id: int
    bid: int
    queue_time: float
    need: int
    preemptible: bool


@dataclass(frozen=True)
class Running:
    job_id: int
    bid: int
    start: float
    charge: int  # max(reservation, actual usage)
    preemptible: bool
    stopping: bool = False


@dataclass
class Decision:
    launch: list[int] = field(default_factory=list)
    preempt: list[int] = field(default_factory=list)
    waiting: list[int] = field(default_factory=list)  # start once stopping jobs exit
    blocked: list[int] = field(default_factory=list)


def order_queue(queued: list[Queued]) -> list[Queued]:
    return sorted(queued, key=lambda q: (-q.bid, q.queue_time, q.job_id))


def decide(queued: list[Queued], running: list[Running], free: int) -> Decision:
    d = Decision()
    avail = free
    incoming = sum(r.charge for r in running if r.stopping)  # freed once they exit
    taken: set[int] = set()
    blocked_bid: int | None = None
    for q in order_queue(queued):
        if not q.preemptible and blocked_bid is not None and blocked_bid > q.bid:
            d.blocked.append(q.job_id)
            continue
        if q.need <= avail:
            d.launch.append(q.job_id)
            avail -= q.need
            continue
        shortfall = q.need - avail
        if shortfall <= incoming:
            incoming -= shortfall
            avail = 0
            d.waiting.append(q.job_id)
            continue
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
        else:
            d.blocked.append(q.job_id)
            blocked_bid = q.bid if blocked_bid is None else max(blocked_bid, q.bid)
    return d
