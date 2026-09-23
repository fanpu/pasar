"""Early warning for a cloud job whose own pace is outrunning its approved time.

Two layers: `pasar.cloud.pace.needs_more_time` is a pure projection (tested directly, against
plain local jobs — it only needs a store, a job, its attempts and an approved-seconds figure, not
a cloud target); `Daemon.needs_more_time` and `Daemon.approve(..., extend=True)` are the daemon
wiring around it (tested with a cloud job, since only cloud jobs are ever approved or extended).
"""

import json

import pytest

from pasar.cloud.cost import estimate, hourly_rate
from pasar.cloud.executor import parse_unit
from pasar.cloud.pace import MIN_OBSERVATION, MIN_REPORTS, needs_more_time
from pasar.daemon import Conflict
from pasar.models import JobSpec, State
from pasar.views import job_view


def emit(daemon, job_id, **event):
    with (daemon.job_dir(job_id) / "events.jsonl").open("a") as f:
        f.write(json.dumps(event) + "\n")


def steady_reports(daemon, clock, job_id, t0, step, total, over):
    """Land `MIN_REPORTS` progress reports along one steady line: `step` of `total` steps in
    `over` seconds, the last landing exactly `over` seconds into the attempt that started at `t0`.

    Only the latest report sets the projected pace (`eta.progress_remaining` measures from the
    attempt's start to the last report), so the earlier ones sit on that same line and leave every
    projection below unchanged. They are here because one report is not a pace: a verdict needs
    `MIN_REPORTS` of them."""
    for i in range(1, MIN_REPORTS + 1):
        clock.t = t0 + over * i / MIN_REPORTS
        event = {"event": "progress", "step": round(step * i / MIN_REPORTS)}
        if total is not None:
            event["total_steps"] = total
        emit(daemon, job_id, **event)
        daemon.tick()


# ---- the pure projection, against plain local jobs

def test_no_verdict_before_enough_observation(daemon, clock, make_spec):
    daemon.submit(make_spec(est_runtime=600))
    daemon.tick()  # attempt starts now
    clock.advance(100)
    emit(daemon, 1, event="progress", step=1000, total_steps=5000)  # wildly behind pace
    daemon.tick()  # the report lands, but it is the first one this attempt
    job = daemon.job(1)
    atts = daemon.store.attempts(1)
    assert clock() - daemon.store.first_event(1, "progress", 1)["ts"] < MIN_OBSERVATION
    assert needs_more_time(daemon.store, job, atts, clock(), 600) is None


def test_no_verdict_from_a_single_report(daemon, clock, make_spec):
    # One report is one data point. A reporting bug or a botched resume (step 1 of a million,
    # long into the run) clears the observation window on its own and looks exactly like a job
    # genuinely crawling — and the projection it implies would size an extension somebody is
    # asked to approve. Until the attempt has a cadence, there is no verdict.
    daemon.submit(make_spec(est_runtime=600))
    daemon.tick()
    clock.advance(1000)
    emit(daemon, 1, event="progress", step=1, total_steps=1_000_000)
    daemon.tick()
    clock.advance(400)  # well past the observation window, still only one report
    job = daemon.job(1)
    atts = daemon.store.attempts(1)
    assert clock() - daemon.store.first_event(1, "progress", 1)["ts"] > MIN_OBSERVATION
    assert daemon.store.count_events(1, "progress", 1) < MIN_REPORTS
    assert needs_more_time(daemon.store, job, atts, clock(), 600) is None


def test_projects_an_overrun_from_the_jobs_own_pace(daemon, clock, make_spec):
    daemon.submit(make_spec(est_runtime=600))
    daemon.tick()  # attempt starts at t0
    steady_reports(daemon, clock, 1, clock.t, step=1000, total=5000, over=400)
    clock.advance(320)  # past the 300s observation window
    job = daemon.job(1)
    atts = daemon.store.attempts(1)
    # 1000 steps in 400s -> 4000 to go costs 1600s from the report; 320s of that has already
    # passed, so 1280s left; total projected attempt length (720 elapsed + 1280 left) is 2000s,
    # 1400s past the 600s approved here.
    assert needs_more_time(daemon.store, job, atts, clock(), 600) == pytest.approx(1400)


def test_no_overrun_when_the_job_is_ahead_of_its_estimate(daemon, clock, make_spec):
    daemon.submit(make_spec(est_runtime=3600))
    daemon.tick()
    # way ahead of pace: 4000 of 5000 steps in 400s
    steady_reports(daemon, clock, 1, clock.t, step=4000, total=5000, over=400)
    clock.advance(320)
    job = daemon.job(1)
    atts = daemon.store.attempts(1)
    assert needs_more_time(daemon.store, job, atts, clock(), 3600) is None


def test_no_verdict_from_an_estimate_only_projection(daemon, clock, make_spec):
    # No total_steps reported: eta.remaining_time falls back to "estimate", not "progress" —
    # there is no pace of the job's own to project from, so there is nothing to warn about.
    daemon.submit(make_spec(est_runtime=60))
    daemon.tick()
    steady_reports(daemon, clock, 1, clock.t, step=10, total=None, over=400)
    clock.advance(320)
    job = daemon.job(1)
    atts = daemon.store.attempts(1)
    assert needs_more_time(daemon.store, job, atts, clock(), 60) is None


def test_no_verdict_once_the_attempt_has_ended(daemon, clock, make_spec, executor):
    daemon.submit(make_spec(est_runtime=600))
    daemon.tick()
    steady_reports(daemon, clock, 1, clock.t, step=1000, total=5000, over=400)
    clock.advance(320)
    executor.exit("pasar-job-1-1", code=0)
    daemon.tick()
    job = daemon.job(1)
    atts = daemon.store.attempts(1)
    assert atts[-1].end_time is not None
    assert needs_more_time(daemon.store, job, atts, clock(), 600) is None


# ---- the daemon: marking a running job, and extending it

def cloud_spec(cwd, **kw):
    base = {"command": "python -c 'pass'", "est_runtime": 600, "cwd": cwd,
            "target": "fake", "gpu": "H100"}
    return JobSpec(**{**base, **kw})


def start(daemon, cwd, **kw):
    """Submit, approve and launch one cloud job; returns its id."""
    job = daemon.submit(cloud_spec(cwd, **kw))
    daemon.approve(job.id)
    daemon.tick()
    return job.id


def handle_of(daemon, job_id):
    return parse_unit(daemon.store.current_attempt(job_id).unit)[1]


def lag_the_job(daemon, clock, job_id):
    """Report pace slow enough, often enough and long enough ago to project a large overrun:
    `MIN_REPORTS` reports on the line 1000 of 5000 steps in 400s, the last read 320s past the
    300s threshold. Mirrors the pure projection tests above, so the same arithmetic (a 2000s
    projected attempt against a 900s window) applies here too."""
    att = daemon.store.current_attempt(job_id)
    steady_reports(daemon, clock, job_id, att.start_time, step=1000, total=5000, over=400)
    clock.advance(320)
    daemon.tick()


def test_a_running_job_on_pace_to_overrun_is_marked(cloud_daemon, cloud_provider, cloud_cwd,
                                                     clock):
    daemon = cloud_daemon
    job_id = start(daemon, cloud_cwd)  # est_runtime=600, timeout_factor default 1.5 -> window 900
    assert daemon.needs_more_time(daemon.job(job_id)) is None  # nothing reported yet
    lag_the_job(daemon, clock, job_id)
    job = daemon.job(job_id)
    overrun = daemon.needs_more_time(job)
    assert overrun == pytest.approx(1100)  # 2000s projected - 900s window
    assert job.state == State.RUNNING  # marking never stops the job
    assert cloud_provider.stopped == []


def test_needs_more_time_view_field_matches_the_daemon(cloud_daemon, cloud_provider, cloud_cwd,
                                                        clock):
    daemon = cloud_daemon
    job_id = start(daemon, cloud_cwd)
    lag_the_job(daemon, clock, job_id)
    v = job_view(daemon, daemon.job(job_id), clock(), {})
    assert v["cloud"]["needs_more_time"] == pytest.approx(
        daemon.needs_more_time(daemon.job(job_id)))


def test_extending_raises_the_limit_and_the_job_is_not_paused_at_the_old_one(
        cloud_daemon, cloud_provider, cloud_cwd, clock):
    daemon = cloud_daemon
    job_id = start(daemon, cloud_cwd)
    start_t = clock.t
    h = handle_of(daemon, job_id)
    lag_the_job(daemon, clock, job_id)  # now start_t + 720; overrun == 1100s
    assert daemon.needs_more_time(daemon.job(job_id)) is not None

    extended = daemon.approve(job_id, extend=True)
    assert extended.state == State.RUNNING
    assert len(daemon.store.approvals(job_id)) == 1  # the same attempt's row, not a new one

    # the old deadline (900s from the start) passes without the job being told to stop
    clock.t = start_t + 901
    daemon.tick()
    assert cloud_provider.stopped == []
    assert daemon.job(job_id).state == State.RUNNING

    # the new ceiling still ends the attempt eventually
    target = daemon.cfg.clouds["fake"]
    att = daemon.store.current_attempt(job_id)
    new_deadline = start_t + daemon._attempt_seconds(daemon.job(job_id), att, target)
    assert new_deadline > start_t + 900  # it really did raise the limit
    clock.t = new_deadline + 1
    daemon.tick()
    assert cloud_provider.stopped == [h]
    assert daemon.job(job_id).state == State.STOPPING


def test_extending_refuses_to_exceed_max_cost(cloud_daemon, cloud_provider, cloud_cwd, clock):
    daemon = cloud_daemon
    rate = hourly_rate(cloud_provider.rates(), "h100", 1)
    window_cost = estimate(rate, 900)  # the uncapped window this job would otherwise get
    # A cap above the window's own cost so the first approval is not itself capped, but too low
    # to cover the (much larger) ceiling an extension would need.
    job_id = start(daemon, cloud_cwd, max_cost=window_cost + 0.5)
    before = dict(daemon.store.approvals(job_id)[0])
    lag_the_job(daemon, clock, job_id)
    assert daemon.needs_more_time(daemon.job(job_id)) is not None

    with pytest.raises(Conflict, match="max-cost"):
        daemon.approve(job_id, extend=True)

    # nothing changed: not the approval row, not the ledger, not the job
    after = dict(daemon.store.approvals(job_id)[0])
    assert after == before
    assert daemon.job(job_id).state == State.RUNNING
    assert cloud_provider.stopped == []


def test_extending_refuses_to_reach_past_the_targets_own_limit(cloud_daemon, cloud_provider,
                                                                 cloud_cwd, clock):
    # A job reporting almost no progress projects an overrun of years. No attempt on the target
    # can run that long — the sandbox itself is launched with `max_runtime` as its limit — so the
    # extension is refused rather than written into the approvals row, where `Ledger.committed()`
    # would read the imaginary figure and shut every other job on the target out of the budget.
    daemon = cloud_daemon
    job_id = start(daemon, cloud_cwd)  # no --max-cost: nothing else bounds the ceiling
    target = daemon.cfg.clouds["fake"]
    att = daemon.store.current_attempt(job_id)
    before = dict(daemon.store.approvals(job_id)[0])
    steady_reports(daemon, clock, job_id, att.start_time, step=3, total=1_000_000, over=400)
    clock.advance(320)
    daemon.tick()
    assert daemon.needs_more_time(daemon.job(job_id)) > target.max_runtime
    committed = daemon.ledger.committed("fake")  # read at this instant: it rises with elapsed time

    with pytest.raises(Conflict) as excinfo:
        daemon.approve(job_id, extend=True)
    assert "24h00m" in str(excinfo.value)  # what the target allows one attempt
    assert "--time" in str(excinfo.value)  # and what to do about it
    # nothing was committed on the strength of that projection
    assert dict(daemon.store.approvals(job_id)[0]) == before
    assert daemon.ledger.committed("fake") == pytest.approx(committed)
    assert daemon.job(job_id).state == State.RUNNING
    assert cloud_provider.stopped == []


def test_extending_when_nothing_is_overdue_is_refused(cloud_daemon, cloud_cwd):
    daemon = cloud_daemon
    job_id = start(daemon, cloud_cwd)
    with pytest.raises(Conflict, match="not projected"):
        daemon.approve(job_id, extend=True)


def test_extending_a_job_that_is_not_running_is_refused(cloud_daemon, cloud_cwd):
    daemon = cloud_daemon
    job = daemon.submit(cloud_spec(cloud_cwd))  # still awaiting, never launched
    with pytest.raises(Conflict):
        daemon.approve(job.id, extend=True)


def test_the_mark_clears_as_soon_as_it_is_extended(cloud_daemon, cloud_cwd, clock):
    # The mark is never stored; it is recomputed from the store on every read. Extending raises
    # the ceiling enough to cover the current projection, so the very next read already sees
    # nothing to flag — there is no separate "clear" step.
    daemon = cloud_daemon
    job_id = start(daemon, cloud_cwd)
    lag_the_job(daemon, clock, job_id)
    assert daemon.needs_more_time(daemon.job(job_id)) is not None
    daemon.approve(job_id, extend=True)
    assert daemon.needs_more_time(daemon.job(job_id)) is None


def test_the_mark_clears_when_the_jobs_pace_recovers(cloud_daemon, cloud_cwd, clock):
    daemon = cloud_daemon
    job_id = start(daemon, cloud_cwd)
    lag_the_job(daemon, clock, job_id)
    assert daemon.needs_more_time(daemon.job(job_id)) is not None
    # A later report at a much faster rate swings the same projection back under the ceiling,
    # with no extension and no daemon-side bookkeeping to reset.
    emit(daemon, job_id, event="progress", step=4900, total_steps=5000)
    daemon.tick()
    assert daemon.needs_more_time(daemon.job(job_id)) is None


def test_a_job_can_be_extended_again_after_falling_further_behind(cloud_daemon, cloud_provider,
                                                                   cloud_cwd, clock):
    daemon = cloud_daemon
    job_id = start(daemon, cloud_cwd)
    lag_the_job(daemon, clock, job_id)
    daemon.approve(job_id, extend=True)
    first_ceiling = daemon.store.approvals(job_id)[0]["max_cost"]
    assert daemon.needs_more_time(daemon.job(job_id)) is None

    # Pace worsens again: a much slower report than before.
    clock.advance(400)
    emit(daemon, job_id, event="progress", step=1050, total_steps=5000)
    daemon.tick()
    clock.advance(320)
    daemon.tick()
    assert daemon.needs_more_time(daemon.job(job_id)) is not None

    daemon.approve(job_id, extend=True)
    second_ceiling = daemon.store.approvals(job_id)[0]["max_cost"]
    assert second_ceiling > first_ceiling
    assert len(daemon.store.approvals(job_id)) == 1  # still the one attempt's row
    assert daemon.needs_more_time(daemon.job(job_id)) is None
    assert cloud_provider.stopped == []
