"""Which approved cloud jobs start this pass: bid order, a concurrency cap, then a budget gate.

Approval happens upstream (Task 8's flow); a job awaiting approval never reaches `queued` here
and this module never checks approval state. What is decided here is order and affordability
only, and it is pure: no store, no clock, no I/O — the daemon reads the ledger and target,
converts them to plain numbers, and calls `decide_cloud`.
"""

from dataclasses import dataclass, field

from pasar.config import CloudTarget


@dataclass(frozen=True)
class CloudQueued:
    job_id: int
    bid: int
    queue_time: float
    estimate: float  # dollars for the approved run time


@dataclass
class CloudDecision:
    launch: list[int] = field(default_factory=list)
    blocked: dict[int, str] = field(default_factory=dict)  # job -> "budget" | "concurrency"


def order_queue(queued: list[CloudQueued]) -> list[CloudQueued]:
    return sorted(queued, key=lambda q: (-q.bid, q.queue_time, q.job_id))


def decide_cloud(queued: list[CloudQueued], running_count: int, target: CloudTarget,
                  spent_day: float, spent_month: float, committed: float) -> CloudDecision:
    """Walk the queue in bid order (ties by queue_time, then job_id) and launch what fits.
    There is no `--preempt` for cloud jobs and no reshuffling to let a cheaper job ahead: once a
    job is blocked, every job behind it in this pass is blocked for the same reason, so the
    queue's bid order is also its start order across passes.

    Budget contract the caller must satisfy: `spent_day` and `spent_month` are dollars already
    realized for that period — from attempts that are no longer running (billed, or otherwise
    closed) — and must exclude any attempt still running now. `committed` is the ledger's figure
    for every currently running, unbilled attempt of this target (`Ledger.committed()`), and is
    charged against both the daily and the monthly remaining budget, because the same open
    dollars threaten both at once. Passing `Ledger.spent_day()`/`spent_month()` unmodified here
    double-counts: today those already fold in every open attempt's flat estimate, and
    `committed` folds the same attempts back in, at or above their estimate. The caller must
    strip open attempts out of `spent_day`/`spent_month` first (sum only rows that are billed,
    or whose attempt has ended) so each open attempt is counted exactly once — in `committed`.

    A job whose own estimate exceeds the target's budget outright is never dropped from the
    queue; it lands in `blocked` every pass, same as any other unaffordable job, so it stays
    visible for a human to cancel or an operator to raise the budget rather than vanishing.
    """
    d = CloudDecision()
    slots = target.max_running - running_count
    day_budget = target.daily_budget - spent_day - committed
    month_budget = target.monthly_budget - spent_month - committed
    budget_exhausted = False
    for q in order_queue(queued):
        if budget_exhausted:
            d.blocked[q.job_id] = "budget"
            continue
        if slots <= 0:
            d.blocked[q.job_id] = "concurrency"
            continue
        if q.estimate > day_budget or q.estimate > month_budget:
            d.blocked[q.job_id] = "budget"
            budget_exhausted = True
            continue
        d.launch.append(q.job_id)
        slots -= 1
        day_budget -= q.estimate
        month_budget -= q.estimate
    return d
