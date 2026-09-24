"""Which of several interchangeable accounts a cloud job should go to.

A group's members differ only in whose credit pays and how much of it is left, so the choice is
about money and nothing else. It is made against the same ledger the budget gate reads, so an
account this picks is one the gate will also let the job launch on.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from pasar.cloud.cost import Ledger
from pasar.config import CloudTarget

log = logging.getLogger(__name__)


def headroom(ledger: Ledger, target: CloudTarget) -> float:
    """Dollars left on `target` this month: its budget, less what is already settled and what
    running attempts have already promised. Never negative — an overspent account has no
    headroom, and how far past the line it went is not a number any caller here can use."""
    used = ledger.settled_month(target.name) + ledger.committed(target.name)
    return max(0.0, target.monthly_budget - used)


def choose(members: list[CloudTarget], ledger: Ledger, need: float,
           running: Callable[[str], int]) -> CloudTarget | None:
    """The fullest account that can still pay for the whole run, or None if none can.

    Packing rather than spreading: each account has its own image cache and its own volumes, so
    a job moved to a fresh account pays a fresh environment build out of credit meant for
    compute, and a job that pauses can only ever resume on the account that holds its
    checkpoint. Concentrating the work keeps both of those cheap.
    """
    fits = [t for t in members if headroom(ledger, t) >= need]
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


def shortfall(members: list[CloudTarget], ledger: Ledger) -> list[Shortfall]:
    """What every account has left, for the message a person reads when none of them is enough."""
    return [Shortfall(t.name, t.owner, headroom(ledger, t), t.monthly_budget) for t in members]
