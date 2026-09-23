import pytest

from pasar.cloud.lane import CloudQueued, decide_cloud


def t(**kw):
    from pasar.config import CloudTarget
    return CloudTarget(name="c", provider="fake", daily_budget=50.0, monthly_budget=300.0,
                       max_running=2, **kw)


def test_launches_in_bid_then_queue_time_order():
    q = [CloudQueued(1, 1000, 10.0, 5.0), CloudQueued(2, 2000, 20.0, 5.0),
         CloudQueued(3, 1000, 5.0, 5.0)]
    d = decide_cloud(q, 0, t(), spent_day=0, spent_month=0, committed=0)
    assert d.launch == [2, 3]          # max_running cuts it at two
    assert d.blocked == {1: "concurrency"}


def test_a_job_over_the_daily_budget_blocks_the_ones_behind_it():
    q = [CloudQueued(1, 1000, 10.0, 40.0), CloudQueued(2, 1000, 20.0, 1.0)]
    d = decide_cloud(q, 0, t(), spent_day=20.0, spent_month=0, committed=0)
    assert d.launch == [] and d.blocked == {1: "budget", 2: "budget"}


def test_committed_cost_of_running_jobs_counts_against_the_budget():
    q = [CloudQueued(1, 1000, 10.0, 10.0)]
    d = decide_cloud(q, 1, t(), spent_day=0, spent_month=0, committed=45.0)
    assert d.blocked == {1: "budget"}


def test_monthly_budget_also_blocks():
    q = [CloudQueued(1, 1000, 10.0, 10.0)]
    d = decide_cloud(q, 0, t(), spent_day=0, spent_month=295.0, committed=0)
    assert d.blocked == {1: "budget"}


def test_empty_queue_launches_nothing_and_blocks_nothing():
    d = decide_cloud([], 0, t(), spent_day=0, spent_month=0, committed=0)
    assert d.launch == [] and d.blocked == {}


def test_equal_bids_order_by_queue_time():
    q = [CloudQueued(1, 1000, 20.0, 5.0), CloudQueued(2, 1000, 10.0, 5.0),
         CloudQueued(3, 1000, 30.0, 5.0)]
    d = decide_cloud(q, 0, t(), spent_day=0, spent_month=0, committed=0)
    assert d.launch == [2, 1]           # earliest queue_time first; max_running=2 cuts the third
    assert d.blocked == {3: "concurrency"}


def test_max_running_already_exceeded_blocks_everything_as_concurrency():
    q = [CloudQueued(1, 1000, 10.0, 1.0)]
    d = decide_cloud(q, 5, t(), spent_day=0, spent_month=0, committed=0)
    assert d.launch == [] and d.blocked == {1: "concurrency"}


def test_a_jobs_own_estimate_exceeding_the_whole_daily_budget_still_reports_it_blocked():
    """It must never silently vanish from the decision: it always lands in launch or blocked, so
    a human can see it is stuck and cancel it or raise the budget."""
    q = [CloudQueued(1, 1000, 10.0, 999.0)]
    d = decide_cloud(q, 0, t(), spent_day=0, spent_month=0, committed=0)
    assert d.launch == [] and d.blocked == {1: "budget"}


def test_monthly_cap_binds_while_daily_does_not():
    from pasar.config import CloudTarget
    target = CloudTarget(name="c", provider="fake", daily_budget=50.0, monthly_budget=15.0,
                          max_running=2)
    q = [CloudQueued(1, 1000, 10.0, 10.0)]
    d = decide_cloud(q, 0, target, spent_day=0, spent_month=10.0, committed=0)
    assert d.launch == [] and d.blocked == {1: "budget"}


def test_daily_cap_binds_while_monthly_does_not():
    q = [CloudQueued(1, 1000, 10.0, 10.0)]
    d = decide_cloud(q, 0, t(), spent_day=45.0, spent_month=0, committed=0)
    assert d.launch == [] and d.blocked == {1: "budget"}


def test_committed_charges_both_daily_and_monthly_remaining_budget():
    """The same open dollars threaten both caps at once, so committed is not split between
    them; a target with a tight monthly cap can be blocked purely by committed spend even
    though the daily numbers alone would allow the job."""
    from pasar.config import CloudTarget
    target = CloudTarget(name="c", provider="fake", daily_budget=50.0, monthly_budget=40.0,
                          max_running=2)
    q = [CloudQueued(1, 1000, 10.0, 10.0)]
    d = decide_cloud(q, 1, target, spent_day=0, spent_month=0, committed=35.0)
    assert d.launch == [] and d.blocked == {1: "budget"}


def test_a_job_that_fits_launches_and_spends_down_the_budget_for_the_next_one():
    q = [CloudQueued(1, 1000, 10.0, 30.0), CloudQueued(2, 900, 20.0, 30.0)]
    d = decide_cloud(q, 0, t(), spent_day=0, spent_month=0, committed=0)
    assert d.launch == [1] and d.blocked == {2: "budget"}


def test_a_job_costing_exactly_the_remaining_budget_launches():
    q = [CloudQueued(1, 1000, 10.0, 20.0)]
    d = decide_cloud(q, 0, t(), spent_day=30.0, spent_month=0, committed=0)
    assert d.launch == [1] and d.blocked == {}


def test_float_accrual_does_not_spuriously_block_an_exact_fit():
    """Two prior launches of 0.1 and 0.2 leave a remaining daily budget of
    49.699999999999996 in raw floats; a job costing exactly 49.7 must still launch."""
    target = t()
    spent = 0.1 + 0.2
    q = [CloudQueued(1, 1000, 10.0, target.daily_budget - spent)]
    d = decide_cloud(q, 0, target, spent_day=spent, spent_month=0, committed=0)
    assert d.launch == [1] and d.blocked == {}


def test_decide_cloud_with_a_real_ledger_charges_a_running_job_exactly_once(tmp_path):
    """The wiring `decide_cloud` depends on: settled_day()/committed() from a real Ledger must
    charge a running attempt once, not zero or twice. Also shows why the raw spent_day() must
    not be summed with committed(): doing so double-counts the same running job and wrongly
    blocks a job that in fact fits."""
    from pasar.cloud.cost import Ledger
    from pasar.db import Store
    from pasar.models import Attempt, JobSpec
    from tests.fakes import FakeClock

    store = Store(tmp_path / "pasar.db")
    clock = FakeClock(1_700_000_000.0)
    ledger = Ledger(store, clock)
    target = t()

    running = store.insert_job(JobSpec(command="python a.py", est_runtime=3600, cwd="/tmp"),
                               1000, clock(), None)
    store.insert_attempt(Attempt(running, 1, "cloud:c:sb-1", clock()))
    ledger.record(target.name, running, 1, 30.0)
    clock.advance(900)  # a quarter of its own estimated runtime: committed() stays at the flat estimate

    settled_day = ledger.settled_day(target.name)
    committed = ledger.committed(target.name)
    assert settled_day == 0.0
    assert committed == pytest.approx(30.0)

    q = [CloudQueued(2, 1000, clock(), 15.0)]
    right = decide_cloud(q, running_count=1, target=target, spent_day=settled_day,
                         spent_month=ledger.settled_month(target.name), committed=committed)
    assert right.launch == [2] and right.blocked == {}  # 50 - 0 - 30 = 20 >= 15

    raw_day = ledger.spent_day(target.name)
    assert raw_day == pytest.approx(30.0)  # the open row's flat estimate, already in committed too
    wrong = decide_cloud(q, running_count=1, target=target, spent_day=raw_day,
                         spent_month=ledger.spent_month(target.name), committed=committed)
    assert wrong.blocked == {2: "budget"}  # 50 - 30 - 30 = -10: double-counting the running job


# ---- probing past the budget

def _t(**kw):
    """A target that opts in to probing, as one with a provider-side spending limit would."""
    from pasar.config import CloudTarget
    base = {"name": "fake", "provider": "fake", "daily_budget": 10.0, "monthly_budget": 10.0,
            "probe_past_budget": True}
    return CloudTarget(**{**base, **kw})


def test_an_unaffordable_head_of_queue_is_let_through_once_to_ask_the_provider():
    # pasar's budget is arithmetic over estimates; the provider's refusal is the fact. An account
    # has been seen refusing every launch while reporting nothing spent, so the two disagree in
    # both directions and the cheap way to settle it is to ask.
    d = decide_cloud([CloudQueued(1, 1000, 0.0, estimate=99.0)], 0, _t(),
                     spent_day=10.0, spent_month=10.0, committed=0.0)
    assert d.launch == [1] and d.probe == 1
    assert 1 not in d.blocked


def test_only_one_job_probes_and_the_rest_wait():
    q = [CloudQueued(1, 1000, 0.0, estimate=99.0), CloudQueued(2, 1000, 1.0, estimate=99.0)]
    d = decide_cloud(q, 0, _t(), spent_day=10.0, spent_month=10.0, committed=0.0)
    assert d.launch == [1] and d.probe == 1
    assert d.blocked == {2: "budget"}


def test_nothing_probes_while_the_target_is_already_busy():
    # Something is running, so the account is demonstrably not refusing launches; there is
    # nothing to learn and a second over-budget job would just be an over-budget job.
    d = decide_cloud([CloudQueued(1, 1000, 0.0, estimate=99.0)], 1, _t(max_running=2),
                     spent_day=10.0, spent_month=10.0, committed=0.0)
    assert d.launch == [] and d.probe is None
    assert d.blocked == {1: "budget"}


def test_no_probe_when_an_affordable_job_already_launched_this_pass():
    q = [CloudQueued(1, 2000, 0.0, estimate=1.0), CloudQueued(2, 1000, 1.0, estimate=99.0)]
    d = decide_cloud(q, 0, _t(max_running=4), spent_day=0.0, spent_month=0.0, committed=0.0)
    assert d.launch == [1] and d.probe is None
    assert d.blocked == {2: "budget"}


def test_probing_is_off_where_the_provider_has_no_limit_of_its_own():
    # Without a spending limit at the provider, the budget IS the limit, and spending past it is
    # exactly the thing it exists to stop.
    d = decide_cloud([CloudQueued(1, 1000, 0.0, estimate=99.0)], 0,
                     _t(probe_past_budget=False),
                     spent_day=10.0, spent_month=10.0, committed=0.0)
    assert d.launch == [] and d.probe is None
    assert d.blocked == {1: "budget"}


def test_an_affordable_queue_never_sets_probe():
    d = decide_cloud([CloudQueued(1, 1000, 0.0, estimate=1.0)], 0, _t(max_running=2),
                     spent_day=0.0, spent_month=0.0, committed=0.0)
    assert d.launch == [1] and d.probe is None
