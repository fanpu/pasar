"""Which of several interchangeable accounts a cloud job should go to.

A group's members differ only in whose credit pays and how much of it is left, so the choice is
about money and nothing else. It is made against the same ledger the budget gate reads, but only
checks the monthly budget — the gate also checks the daily one, so it can still refuse the
account this module picks.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pasar.cloud.cost import Ledger
from pasar.config import CloudTarget


def headroom(ledger: Ledger, target: CloudTarget, claimed: float = 0.0) -> float:
    """Dollars left on `target` this month: its budget, less what is already settled and what
    running attempts have already promised, and less `claimed` — what jobs waiting on it that
    the ledger cannot see yet will need. Never negative — an overspent account has no headroom,
    and how far past the line it went is not a number any caller here can use."""
    used = ledger.settled_month(target.name) + ledger.committed(target.name) + claimed
    return max(0.0, target.monthly_budget - used)


def choose(members: list[CloudTarget], ledger: Ledger, need: float,
           running: Callable[[str], int],
           claimed: Mapping[str, float] | None = None) -> CloudTarget | None:
    """The fullest account, by monthly headroom, that can still pay for the whole run, or None if
    none can. Only the monthly budget is checked; the launch-time gate also checks the daily one,
    so it may still refuse the account this returns.

    Packing rather than spreading: each account has its own image cache and its own volumes, so
    a job moved to a fresh account pays a fresh environment build out of credit meant for
    compute, and a job that pauses can only ever resume on the account that holds its
    checkpoint. Concentrating the work keeps both of those cheap.

    `running` must return an int for every member of `members`, not just the ones it expects to
    matter — `choose` calls it on every account that still fits before it can know which of them
    a free slot will decide between.

    `claimed` is dollars per account already spoken for by jobs waiting on it (see `headroom`).
    """
    claimed = claimed or {}

    def left(t: CloudTarget) -> float:
        return headroom(ledger, t, claimed.get(t.name, 0.0))

    fits = [t for t in members if left(t) >= need]
    if not fits:
        return None
    free = [t for t in fits if running(t.name) < t.max_running]
    # Least headroom first; `members` order breaks a tie, because a choice that moves with
    # dictionary ordering is one nobody can reproduce from a bug report.
    return min(free or fits, key=lambda t: (left(t), members.index(t)))


@dataclass(frozen=True)
class Shortfall:
    name: str
    owner: str
    left: float
    budget: float


def shortfall(members: list[CloudTarget], ledger: Ledger) -> list[Shortfall]:
    """What every account has left, for the message a person reads when none of them is enough."""
    return [Shortfall(t.name, t.owner, headroom(ledger, t), t.monthly_budget) for t in members]
