"""Which of several interchangeable accounts a cloud job should go to.

A group's members differ only in whose credit pays and how much of it is left, so the choice is
about money and nothing else. It is made against the same ledger the budget gate reads, but only
checks the monthly budget — the gate also checks the daily one, so it can still refuse the
account this module picks.
"""

from collections.abc import Callable
from dataclasses import dataclass

from pasar.cloud.base import Credit
from pasar.cloud.cost import Ledger
from pasar.config import CloudTarget

# What the provider's own books say an account has used, by target name; None where it cannot
# say. `Daemon.cloud_credit`, cached — never a network call of its own.
CreditOf = Callable[[str], Credit | None]


def _no_credit(_name: str) -> None:
    return None


def headroom(ledger: Ledger, target: CloudTarget, credit: Credit | None = None) -> float:
    """Dollars left on `target` this month: its budget, less what is already settled and what
    running attempts have already promised. Never negative — an overspent account has no
    headroom, and how far past the line it went is not a number any caller here can use.

    `credit` is the provider's own figure for the account, where it has one: an account it says
    is past its free allowance has no headroom at all, whatever the ledger thinks."""
    used = ledger.settled_month(target.name) + ledger.committed(target.name)
    if credit is not None:
        if credit.exhausted:
            return 0.0
        # The provider counts what pasar could not see — an image build, somebody else's job on
        # the same account. pasar counts what has been promised but not yet billed. Neither is a
        # superset of the other, so the larger of the two is the only safe figure. They are
        # compared, never added, because they are two views of one cycle that need not even be
        # the same window: Modal's is the UTC calendar month, the ledger's the local one, and
        # another provider may bill on a cycle of its own.
        used = max(used, credit.used)
    return max(0.0, target.monthly_budget - used)


def choose(members: list[CloudTarget], ledger: Ledger, need: float,
           running: Callable[[str], int], credit: CreditOf = _no_credit) -> CloudTarget | None:
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

    `credit` gives the provider's own figure for each account (see `headroom`).
    """
    fits = [t for t in members if headroom(ledger, t, credit(t.name)) >= need]
    if not fits:
        return None
    free = [t for t in fits if running(t.name) < t.max_running]
    # Least headroom first; `members` order breaks a tie, because a choice that moves with
    # dictionary ordering is one nobody can reproduce from a bug report.
    return min(free or fits, key=lambda t: (headroom(ledger, t, credit(t.name)),
                                          members.index(t)))


@dataclass(frozen=True)
class Shortfall:
    name: str
    owner: str
    left: float
    budget: float


def shortfall(members: list[CloudTarget], ledger: Ledger,
              credit: CreditOf = _no_credit) -> list[Shortfall]:
    """What every account has left, for the message a person reads when none of them is enough."""
    return [Shortfall(t.name, t.owner, headroom(ledger, t, credit(t.name)), t.monthly_budget)
            for t in members]
