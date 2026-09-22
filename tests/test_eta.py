import json

from fastapi.testclient import TestClient

from pasar.api import create_app
from pasar.eta import progress_remaining


def emit(daemon, job_id, **event):
    with (daemon.job_dir(job_id) / "events.jsonl").open("a") as f:
        f.write(json.dumps(event) + "\n")


def view(daemon, job_id):
    c = TestClient(create_app(daemon, allowed_hosts=["testserver"]))
    return c.get(f"/api/jobs/{job_id}").json()


def left(daemon, job_id):
    return progress_remaining(daemon.store, job_id, daemon.store.attempts(job_id), daemon.clock())


def test_first_attempt_extrapolates_from_start(daemon, clock, make_spec):
    daemon.submit(make_spec(est_runtime=600))
    daemon.tick()  # starts at t0
    clock.advance(720)
    emit(daemon, 1, event="progress", step=1000, total_steps=5000)
    daemon.tick()  # read at t0 + 720: 1000 steps in 12 min, 4000 to go -> 48 min
    assert left(daemon, 1) == 2880
    v = view(daemon, 1)
    assert v["eta_source"] == "progress"
    assert v["remaining"] == 2880 and v["expected_runtime"] == 3600
    assert v["projected"][0][1] == clock() + 2880


def test_counts_down_between_reports(daemon, clock, make_spec):
    daemon.submit(make_spec())
    daemon.tick()
    clock.advance(100)
    emit(daemon, 1, event="progress", step=10, total_steps=20)
    daemon.tick()
    clock.advance(30)
    assert left(daemon, 1) == 70


def test_falls_back_to_estimate_without_total_or_steps(daemon, clock, make_spec):
    daemon.submit(make_spec(est_runtime=600))
    daemon.tick()
    clock.advance(60)
    assert left(daemon, 1) is None
    emit(daemon, 1, event="progress", step=10)
    daemon.tick()
    assert left(daemon, 1) is None
    v = view(daemon, 1)
    assert v["eta_source"] == "estimate" and v["remaining"] == 540


def test_resumed_attempt_counts_from_its_resume_step(daemon, executor, clock, make_spec):
    daemon.submit(make_spec())
    daemon.tick()
    clock.advance(100)
    emit(daemon, 1, event="checkpoint", step=3000)
    daemon.tick()
    daemon.store.update_job(1, state="queued")  # as if preempted: requeue for a second attempt
    executor.exit("pasar-job-1-1", code=0)
    att = daemon.store.current_attempt(1)
    daemon.store.update_attempt(1, att.n, end_time=clock())
    daemon.tick()  # attempt 2 starts now
    assert daemon.store.current_attempt(1).n == 2
    start = daemon.store.current_attempt(1).start_time
    clock.advance(200)
    emit(daemon, 1, event="progress", step=4000, total_steps=5000)
    daemon.tick()
    # 1000 steps since the checkpoint in (now - start); 1000 to go -> the same again
    assert left(daemon, 1) == clock() - start
    emit(daemon, 1, event="resumed", step=3500)
    daemon.tick()
    # an explicit resumed step wins: 500 steps done, 1000 to go -> twice the time
    assert left(daemon, 1) == 2 * (clock() - start)


def test_overdue_job_is_projected_to_finish_imminently(daemon, clock, make_spec):
    daemon.submit(make_spec())
    daemon.tick()
    clock.advance(100)
    emit(daemon, 1, event="progress", step=90, total_steps=100)
    daemon.tick()
    clock.advance(1000)  # went quiet long past its predicted finish
    assert left(daemon, 1) < 0
    v = view(daemon, 1)
    assert v["remaining"] == 0
    assert v["projected"][0][1] == clock() + 60  # projection's minimum remaining time
