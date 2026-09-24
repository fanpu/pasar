"""Which of several interchangeable accounts a cloud job should go to.

A group's members differ only in whose credit pays and how much of it is left, so the choice is
about money and nothing else. It is made against the same ledger the budget gate reads, but only
checks the monthly budget — the gate also checks the daily one, so it can still refuse the
account this module picks.
"""

from collections.abc import Callable
from dataclasses import dataclass

from pasar.cloud.cost import Ledger
from pasar.config import CloudTarget


def headroom(ledger: Ledger, target: CloudTarget, health: str | None = None) -> float:
    """Dollars left on `target` this month: its budget, less what is already settled and what
    running attempts have already promised. Never negative — an overspent account has no
    headroom, and how far past the line it went is not a number any caller here can use.

    `health` is the account's `Provider.health()`: an account that refuses every launch has no
    usable money, whatever its balance says, so anything but None is no headroom at all."""
    if health is not None:
        return 0.0
    used = ledger.settled_month(target.name) + ledger.committed(target.name)
    return max(0.0, target.monthly_budget - used)


def _healthy(_name: str) -> str | None:
    return None


def choose(members: list[CloudTarget], ledger: Ledger, need: float,
           running: Callable[[str], int],
           health: Callable[[str], str | None] = _healthy) -> CloudTarget | None:
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

    `health` is each account's `Provider.health()` by name; an account it has a reason for is
    never chosen, however much it has left.
    """
    fits = [t for t in members
            if health(t.name) is None and headroom(ledger, t) >= need]
    if not fits:
        return None
    free = [t for t in fits if running(t.name) < t.max_running]
    # Least headroom first; `members` order breaks a tie, because a choice that moves with
    # dictionary ordering is one nobody can reproduce from a bug report.
    return min(free or fits, key=lambda t: (headroom(ledger, t), members.index(t)))


@dataclass(frozen=True)
class Shortfall:
    name: str
    owner: str
    left: float
    budget: float
    unusable: str | None = None  # why it refuses every launch, if it does: see `headroom`


def shortfall(members: list[CloudTarget], ledger: Ledger,
              health: Callable[[str], str | None] = _healthy) -> list[Shortfall]:
    """What every account has left, for the message a person reads when none of them is enough
    — and, for an account refusing to launch, why, since that is fixed by somebody doing
    something quite different from topping up credit."""
    rows = []
    for t in members:
        why = health(t.name)
        rows.append(Shortfall(t.name, t.owner, headroom(ledger, t, why), t.monthly_budget, why))
    return rows
