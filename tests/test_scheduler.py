from pasar.scheduler import INF, Queued, Running, decide

G = 1


def q(job_id, need, bid=1000, t=None, preemptible=True, preempt=False, duration=INF):
    return Queued(job_id, bid, float(job_id if t is None else t), need, preemptible, preempt, duration)


def r(job_id, charge, bid=1000, start=0.0, preemptible=True, stopping=False, end=INF):
    return Running(job_id, bid, start, charge, preemptible, stopping, end)


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
    d = decide([q(9, 40, bid=1500, preempt=True)], running, free=10)
    # shortfall 30: the most recently started of the two 800-bid jobs is enough
    assert d.preempt == [3] and d.waiting == [9] and d.launch == []


def test_non_preemptible_jobs_are_never_victims():
    d = decide([q(9, 50, bid=2000, preempt=True)], [r(1, 60, bid=500, preemptible=False)], free=40)
    assert d.preempt == [] and d.blocked == [9]


def test_higher_bid_without_preempt_only_queues():
    d = decide([q(9, 100, bid=5000)], [r(1, 60, bid=500)], free=40)
    assert d.preempt == [] and d.blocked == [9]


def test_backfill_that_finishes_before_the_reservation():
    # #1 needs 100 and can start once #5 ends at t=500; #2 (40, done by t=300) slips in first
    running = [r(5, 60, end=500)]
    d = decide([q(1, 100), q(2, 40, bid=800, duration=300)], running, free=40, now=0)
    assert d.blocked == [1] and d.launch == [2]


def test_backfill_that_would_delay_the_reservation_waits():
    running = [r(5, 60, end=500)]
    d = decide([q(1, 100), q(2, 40, bid=800, duration=600)], running, free=40, now=0)
    assert d.blocked == [1, 2] and d.launch == []


def test_backfill_with_unknown_duration_waits():
    d = decide([q(1, 100), q(2, 40, bid=800)], [r(5, 60, end=500)], free=40, now=0)
    assert d.blocked == [1, 2]


def test_backfill_that_fits_beside_the_reserved_job():
    # at t=500 #5's 60 frees up: #1 takes 70 of the 100, leaving 30 spare for long-running #2
    running = [r(5, 60, end=500)]
    d = decide([q(1, 70), q(2, 30, bid=800), q(3, 10, bid=700)], running, free=40, now=0)
    assert d.blocked == [1, 3] and d.launch == [2]


def test_job_that_can_never_fit_holds_no_reservation():
    d = decide([q(1, 500), q(2, 40, bid=800)], [r(5, 60, end=500)], free=40, now=0)
    assert d.blocked == [1] and d.launch == [2]


def test_preempting_job_only_takes_lower_bids():
    d = decide([q(9, 100, bid=1000, preempt=True)], [r(1, 60, bid=1000)], free=40)
    assert d.preempt == [] and d.blocked == [9]


def test_memory_from_stopping_jobs_is_awaited_not_preempted_again():
    d = decide([q(9, 80, bid=1500, preempt=True)], [r(1, 60, bid=1000, stopping=True), r(2, 20, bid=1000)], free=20)
    assert d.preempt == [] and d.waiting == [9]


def test_whole_gpu_job_preempts_everything_lower():
    d = decide([q(9, 105, bid=1100, preempt=True)], [r(1, 40, bid=1000, start=1), r(2, 40, bid=900, start=2)], 25)
    assert sorted(d.preempt) == [1, 2] and d.waiting == [9]


def test_negative_free_blocks_without_crashing():
    d = decide([q(1, 10)], [r(5, 120)], free=-15)
    assert d.blocked == [1]
