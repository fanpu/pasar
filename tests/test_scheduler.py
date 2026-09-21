from pasar.scheduler import Queued, Running, decide

G = 1


def q(job_id, need, bid=1000, t=None, preemptible=True):
    return Queued(job_id, bid, float(job_id if t is None else t), need, preemptible)


def r(job_id, charge, bid=1000, start=0.0, preemptible=True, stopping=False):
    return Running(job_id, bid, start, charge, preemptible, stopping)


def test_launches_what_fits_in_order():
    d = decide([q(1, 50), q(2, 40), q(3, 30)], [], free=100)
    assert d.launch == [1, 2] and d.blocked == [3]


def test_higher_bid_first_then_earlier_queue_time():
    d = decide([q(1, 60, bid=1000, t=1), q(2, 60, bid=1500, t=2), q(3, 60, bid=1500, t=0)], [], 100)
    assert d.launch == [3]


def test_equal_bid_never_preempts():
    d = decide([q(2, 100)], [r(1, 60)], free=40)
    assert d.preempt == [] and d.blocked == [2]


def test_preempts_lowest_bid_then_most_recent_only_as_needed():
    running = [r(1, 30, bid=900, start=10), r(2, 30, bid=800, start=5), r(3, 30, bid=800, start=20)]
    d = decide([q(9, 40, bid=1500)], running, free=10)
    # shortfall 30: the most recently started of the two 800-bid jobs is enough
    assert d.preempt == [3] and d.waiting == [9] and d.launch == []


def test_non_preemptible_jobs_are_never_victims():
    d = decide([q(9, 50, bid=2000)], [r(1, 60, bid=500, preemptible=False)], free=40)
    assert d.preempt == [] and d.blocked == [9]


def test_lower_jobs_backfill_around_a_blocked_job():
    d = decide([q(1, 100, bid=1000), q(2, 20, bid=800)], [r(5, 60, bid=1000)], free=40)
    assert d.blocked == [1] and d.launch == [2]


def test_non_preemptible_cannot_start_while_higher_bid_blocked():
    d = decide([q(1, 100, bid=1000), q(2, 20, bid=800, preemptible=False)], [r(5, 60)], free=40)
    assert d.blocked == [1, 2] and d.launch == []


def test_memory_from_stopping_jobs_is_awaited_not_preempted_again():
    d = decide([q(9, 80, bid=1500)], [r(1, 60, bid=1000, stopping=True), r(2, 20, bid=1000)], free=20)
    assert d.preempt == [] and d.waiting == [9]


def test_whole_gpu_job_preempts_everything_lower():
    d = decide([q(9, 105, bid=1100)], [r(1, 40, bid=1000, start=1), r(2, 40, bid=900, start=2)], 25)
    assert sorted(d.preempt) == [1, 2] and d.waiting == [9]


def test_negative_free_blocks_without_crashing():
    d = decide([q(1, 10)], [r(5, 120)], free=-15)
    assert d.blocked == [1]
