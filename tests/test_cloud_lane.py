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
