"""Which of several interchangeable accounts a cloud job should go to."""

import pytest

from pasar.cloud.base import Credit
from pasar.cloud.cost import Ledger
from pasar.cloud.group import choose, headroom, shortfall
from pasar.config import CloudTarget
from pasar.db import Store
from pasar.models import Attempt, JobSpec
from tests.fakes import FakeClock


def _target(name, monthly, owner="", max_running=2):
    return CloudTarget(name=name, provider=name, daily_budget=monthly, monthly_budget=monthly,
                       owner=owner, max_running=max_running)


def _credit(used, exhausted=False):
    """What a provider's own books say: `used` this cycle, and whether the free tier is gone."""
    return Credit(used=used, limit=None, exhausted=exhausted, cycle_start=0.0, cycle_end=1.0)


def _spec():
    return JobSpec(command="python a.py", est_runtime=3600, cwd="/tmp")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "pasar.db")


@pytest.fixture
def clock():
    return FakeClock(1_700_000_000.0)


@pytest.fixture
def ledger(store, clock):
    return Ledger(store, clock)


def _run(store, ledger, clock, target, estimated):
    """Record a running attempt (always the job's first) against `target`, with a real job and
    attempt behind it so `committed()`/`settled_month()` see it as genuinely open. Returns the
    job id."""
    job_id = store.insert_job(_spec(), 1000, clock(), None)
    store.insert_attempt(Attempt(job_id, 1, f"cloud:{target}:sb-{job_id}", clock()))
    ledger.record(target, job_id, 1, estimated)
    return job_id


def _finish(store, clock, job_id, attempt=1):
    """End `job_id`'s attempt without billing it, so its row moves from `committed()` to
    `settled_month()` at the estimate it was recorded with."""
    store.update_attempt(job_id, attempt, end_time=clock())


def _settle_all(store, clock):
    """End every attempt still open, across every job the test has recorded."""
    for job in store.list_jobs():
        attempt = store.current_attempt(job.id)
        if attempt is not None and attempt.end_time is None:
            store.update_attempt(job.id, attempt.n, end_time=clock())


# ---- headroom


def test_headroom_is_the_month_minus_what_is_settled_and_committed(store, ledger, clock):
    target = _target("a", monthly=30.0)
    j1 = _run(store, ledger, clock, "a", 4.0)   # an open attempt
    _finish(store, clock, j1)                    # ...now settled
    _run(store, ledger, clock, "a", 6.0)          # still running
    assert headroom(ledger, target) == pytest.approx(20.0)


def test_headroom_never_goes_below_zero(store, ledger, clock):
    target = _target("a", monthly=5.0)
    _run(store, ledger, clock, "a", 9.0)
    assert headroom(ledger, target) == 0.0


def test_headroom_uses_whichever_figure_is_larger(store, ledger, clock):
    """Over-counting is the safe direction: the alternative is a launch that bills somebody.
    The provider sees what pasar could not (an image build, a hand-started sandbox); pasar sees
    what is promised but not yet billed. Neither contains the other."""
    target = _target("a", monthly=30.0)
    _run(store, ledger, clock, "a", 5.0)
    _settle_all(store, clock)
    assert headroom(ledger, target, credit=_credit(used=12.0)) == pytest.approx(18.0)
    assert headroom(ledger, target, credit=_credit(used=2.0)) == pytest.approx(25.0)


def test_an_exhausted_account_has_no_headroom_whatever_the_ledger_says(ledger):
    target = _target("a", monthly=30.0)
    assert headroom(ledger, target, credit=_credit(used=0.0, exhausted=True)) == 0.0


def test_headroom_with_no_provider_figure_is_the_ledgers(store, ledger, clock):
    """A provider that cannot say (or a billing API that is down) leaves the ledger in charge."""
    target = _target("a", monthly=30.0)
    _run(store, ledger, clock, "a", 5.0)
    assert headroom(ledger, target, credit=None) == pytest.approx(25.0)


def test_waiting_jobs_claims_come_on_top_of_the_larger_figure(store, ledger, clock):
    """What jobs waiting on the account will need is in neither the ledger nor the provider's
    books yet, so it is taken off after the two are compared, never swallowed by the max."""
    target = _target("a", monthly=30.0)
    _run(store, ledger, clock, "a", 5.0)
    _settle_all(store, clock)
    assert headroom(ledger, target, credit=_credit(used=12.0), claimed=4.0) == pytest.approx(
        14.0)
    assert headroom(ledger, target, credit=_credit(used=2.0), claimed=4.0) == pytest.approx(21.0)
    assert headroom(ledger, target, credit=_credit(used=0.0, exhausted=True), claimed=0.0) == 0.0


# ---- choose


def test_choose_packs_the_fullest_account_that_still_fits(store, ledger, clock):
    """Least headroom that still fits: it keeps checkpoints, image caches and volumes on one
    account at a time, and leaves the untouched accounts untouched."""
    a, b, c = _target("a", 30.0), _target("b", 30.0), _target("c", 30.0)
    _run(store, ledger, clock, "a", 29.0)   # $1 left: too little
    _run(store, ledger, clock, "b", 25.0)   # $5 left: fits, and is the fullest that does
    _run(store, ledger, clock, "c", 10.0)   # $20 left: fits, but emptier
    _settle_all(store, clock)
    assert choose([a, b, c], ledger, need=4.0, running=lambda _: 0).name == "b"


def test_choose_prefers_an_account_with_a_free_slot(ledger):
    a, b = _target("a", 30.0, max_running=1), _target("b", 30.0, max_running=1)
    running = {"a": 1, "b": 0}.get
    assert choose([a, b], ledger, need=1.0, running=running).name == "b"


def test_choose_falls_back_to_the_packed_account_when_none_is_free(store, ledger, clock):
    """Waiting behind the fullest account is still packing; it just queues."""
    a, b = _target("a", 30.0, max_running=1), _target("b", 30.0, max_running=1)
    _run(store, ledger, clock, "a", 20.0)
    _settle_all(store, clock)
    assert choose([a, b], ledger, need=1.0, running=lambda _: 1).name == "a"


def test_choose_returns_none_when_nothing_fits(store, ledger, clock):
    a, b = _target("a", 30.0), _target("b", 30.0)
    _run(store, ledger, clock, "a", 29.8)
    _run(store, ledger, clock, "b", 29.5)
    _settle_all(store, clock)
    assert choose([a, b], ledger, need=4.0, running=lambda _: 0) is None


def test_choose_is_deterministic_on_a_tie(ledger):
    a, b = _target("a", 30.0), _target("b", 30.0)
    assert choose([b, a], ledger, need=1.0, running=lambda _: 0).name == "b"  # config order


def test_choose_counts_what_the_provider_says_an_account_has_used(ledger):
    """An account the ledger thinks is untouched, but whose owner has spent it elsewhere."""
    a, b = _target("a", 30.0), _target("b", 30.0)
    books = {"a": _credit(used=28.0), "b": None}
    assert choose([a, b], ledger, need=4.0, running=lambda _: 0, credit=books.get).name == "b"
    books["b"] = _credit(used=0.0, exhausted=True)
    assert choose([a, b], ledger, need=4.0, running=lambda _: 0, credit=books.get) is None


# ---- shortfall


def test_shortfall_reports_every_account_with_its_owner(store, ledger, clock):
    a = _target("a", 30.0, owner="First Owner")
    _run(store, ledger, clock, "a", 29.6)
    _settle_all(store, clock)
    rows = shortfall([a], ledger)
    assert rows[0].name == "a" and rows[0].owner == "First Owner"
    assert rows[0].left == pytest.approx(0.4) and rows[0].budget == 30.0


def test_shortfall_reports_what_the_provider_says_is_left(ledger):
    a = _target("a", 30.0)
    rows = shortfall([a], ledger, credit={"a": _credit(used=29.0)}.get)
    assert rows[0].left == pytest.approx(1.0)
