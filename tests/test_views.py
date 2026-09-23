from pasar.models import Attempt, EndKind
from pasar.units import GiB
from pasar.views import (
    SPARK_POINTS,
    job_view,
    lost_time,
    run_time,
    schedule_projection,
    spark_view,
    status_view,
)


def test_run_time_and_lost_time():
    atts = [
        Attempt(1, 1, "u", 0, 100, EndKind.PREEMPTED, wasted_work=30),
        Attempt(1, 2, "u", 150, 200, EndKind.FAILED, wasted_work=10, restart_cost=5),
        Attempt(1, 3, "u", 210, None, restart_cost=7),
    ]
    assert run_time(atts, now=250) == 100 + 50 + 40
    assert lost_time(atts) == {"preemption": 35, "failure": 17, "known": True}
    atts[0].wasted_work = None
    assert lost_time(atts)["known"] is False


def test_job_and_status_views(daemon, executor, clock, make_spec):
    daemon.submit(make_spec(mem_request=60 * GiB, note="big"))
    daemon.submit(make_spec(mem_request=60 * GiB, est_runtime=600))
    daemon.tick()
    clock.advance(120)
    now = clock()
    proj = schedule_projection(daemon, now)
    v1 = job_view(daemon, daemon.job(1), now, proj)
    assert v1["state"] == "running" and v1["mode"] == "shared" and v1["note"] == "big"
    assert v1["run_time"] == 120 and v1["limit"] == 66 * GiB and v1["attempts"] == 1
    v2 = job_view(daemon, daemon.job(2), now, proj)
    assert v2["state"] == "queued"
    assert v2["projected"][0][0] == 1000 + 3600  # after job 1's estimate
    s = status_view(daemon, now, proj)
    assert s["pool"] == 105 * GiB and s["reserved"] == 66 * GiB and s["free"] == 39 * GiB
    assert s["blocked"] == [2]


def test_spark_view_prefers_loss_over_other_metrics(daemon, make_spec):
    daemon.submit(make_spec())
    daemon.store.add_event(1, 1, 5.0, "progress", 1, {"step": 1, "acc": 0.3, "loss": 0.9})
    daemon.store.add_event(1, 1, 6.0, "progress", 2, {"step": 2, "acc": 0.6, "loss": 0.4})
    sparks = spark_view(daemon.store, [1])
    assert sparks[1]["key"] == "loss"
    assert sparks[1]["points"] == [[5.0, 0.9], [6.0, 0.4]]
    assert sparks[1]["latest"] == 0.4


def test_spark_view_falls_back_to_first_key_and_skips_bookkeeping(daemon, make_spec):
    daemon.submit(make_spec())
    daemon.submit(make_spec())
    # `step`/`total_steps`/`ts` are bookkeeping, and a string value isn't plottable.
    daemon.store.add_event(1, 1, 5.0, "progress", 1,
                           {"step": 1, "total_steps": 99, "ts": 5.0, "zed": 2.0, "acc": 0.3})
    daemon.store.add_event(2, 1, 5.0, "progress", 1, {"step": 1, "phase": "warmup"})
    sparks = spark_view(daemon.store, [1, 2])
    assert sparks[1]["key"] == "acc"
    assert 2 not in sparks  # nothing plottable, so no row sparkline


def test_spark_view_downsamples_long_runs_keeping_both_ends(daemon, make_spec):
    daemon.submit(make_spec())
    for i in range(500):
        daemon.store.add_event(1, 1, float(i), "progress", i, {"step": i, "loss": 1.0 - i / 1000})
    points = spark_view(daemon.store, [1])[1]["points"]
    assert len(points) == SPARK_POINTS
    assert points[0] == [0.0, 1.0]
    assert points[-1] == [499.0, 1.0 - 499 / 1000]
    assert points == sorted(points)  # still in time order after thinning
