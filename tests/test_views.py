from pasar.models import Attempt, EndKind
from pasar.units import GiB
from pasar.views import job_view, lost_time, run_time, schedule_projection, status_view


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
