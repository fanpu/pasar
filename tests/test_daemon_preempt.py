import os

from pasar.executor.base import UnitState
from pasar.models import EndKind, State
from pasar.units import GiB


def test_higher_bid_preempts_and_victim_requeues(daemon, executor, clock, make_spec):
    daemon.submit(make_spec(mem_request=60 * GiB))
    daemon.tick()
    clock.advance(10)
    daemon.submit(make_spec(bid=1500))  # whole GPU
    daemon.tick()
    assert daemon.job(1).state == State.STOPPING and executor.stopped == ["pasar-job-1-1"]
    assert daemon.job(2).state == State.QUEUED and daemon.decision.waiting == [2]
    daemon.tick()  # still stopping: no second stop request
    assert executor.stopped == ["pasar-job-1-1"]
    executor.finish_stop("pasar-job-1-1")
    daemon.tick()
    low = daemon.job(1)
    assert low.state == State.QUEUED and low.reason == "preempted" and low.queue_time == 1000.0
    assert daemon.store.attempts(1)[0].end_kind == EndKind.PREEMPTED
    assert daemon.job(2).state == State.RUNNING


def test_raising_a_bid_triggers_preemption(daemon, executor, make_spec):
    daemon.submit(make_spec(mem_request=60 * GiB))
    daemon.tick()
    daemon.submit(make_spec(mem_request=60 * GiB))
    daemon.tick()
    assert daemon.job(2).state == State.QUEUED and executor.stopped == []
    daemon.set_bid(2, 1001)
    daemon.tick()
    assert executor.stopped == ["pasar-job-1-1"]


def test_non_preemptible_job_is_left_alone(daemon, executor, make_spec):
    daemon.submit(make_spec(mem_request=60 * GiB, preemptible=False))
    daemon.tick()
    daemon.submit(make_spec(bid=5000))
    daemon.tick()
    assert executor.stopped == [] and daemon.decision.blocked == [2]


def test_cancel_during_preemption_wins(daemon, executor, make_spec):
    daemon.submit(make_spec(mem_request=60 * GiB))
    daemon.tick()
    daemon.submit(make_spec(bid=1500))
    daemon.tick()
    daemon.cancel(1)
    executor.finish_stop("pasar-job-1-1")
    daemon.tick()
    assert daemon.job(1).state == State.CANCELLED


def test_lost_time_from_checkpoint_and_resume(daemon, executor, clock, make_spec):
    daemon.submit(make_spec(mem_request=60 * GiB))
    daemon.tick()  # attempt 1 starts at 1000
    clock.advance(100)
    (daemon.job_dir(1) / "events.jsonl").write_text('{"event":"checkpoint","step":10}\n')
    daemon.tick()  # checkpoint recorded at 1100
    clock.advance(50)
    daemon.submit(make_spec(bid=2000))
    daemon.tick()  # preempt requested at 1150
    executor.finish_stop("pasar-job-1-1")
    daemon.tick()  # attempt 1 ends at 1150; job 2 launches
    assert daemon.store.attempts(1)[0].wasted_work == 50
    executor.exit("pasar-job-2-1", code=0)
    daemon.tick()  # job 2 done; job 1 attempt 2 starts at 1150
    clock.advance(20)
    with (daemon.job_dir(1) / "events.jsonl").open("a") as f:
        f.write('{"event":"resumed","step":10}\n')
    daemon.tick()
    assert daemon.store.attempts(1)[1].restart_cost == 20


def test_lost_time_unknown_for_jobs_without_events(daemon, executor, make_spec):
    daemon.submit(make_spec(mem_request=60 * GiB))
    daemon.tick()
    daemon.submit(make_spec(bid=2000))
    daemon.tick()
    executor.finish_stop("pasar-job-1-1")
    daemon.tick()
    assert daemon.store.attempts(1)[0].wasted_work is None


def test_over_limit_job_killed_only_under_sustained_pressure(daemon, executor, probe, clock,
                                                             make_spec):
    daemon.submit(make_spec(mem_request=10 * GiB))  # limit 12 GiB
    daemon.tick()
    cg = executor.units["pasar-job-1-1"].control_group
    probe.cg_mem[cg] = 1 * GiB
    probe.cg_pids[cg] = [4242]
    probe.gpu = {4242: 14 * GiB}
    clock.advance(60)
    daemon.tick()
    assert executor.killed == []  # over limit, but no pressure
    probe.psi = 25.0
    daemon.tick()
    clock.advance(31)
    daemon.tick()
    assert executor.killed == ["pasar-job-1-1"]
    daemon.tick()
    job = daemon.job(1)
    assert job.state == State.FAILED and job.reason == "oom"
    assert "12.0 GiB limit" in job.summary and "15.0 GiB" in job.summary
    kinds = [e["kind"] for e in daemon.store.machine_events()]
    assert "oom_kill" in kinds and "pressure" in kinds


def test_reconcile_notes_stray_units(daemon, executor):
    executor.units["pasar-job-77-1"] = UnitState("pasar-job-77-1", False, None, None, None, None)
    daemon.reconcile()
    assert daemon.store.machine_events()[0]["kind"] == "stray_unit"


def test_housekeeping_removes_old_job_files(daemon, executor, clock, make_spec):
    daemon.submit(make_spec())
    daemon.tick()
    executor.exit("pasar-job-1-1", code=0)
    daemon.tick()
    daemon.submit(make_spec())
    clock.advance(31 * 86400)
    daemon.housekeep()
    assert not daemon.job_dir(1).exists()
    assert daemon.job_dir(2).exists()  # still queued
    assert os.path.exists(daemon.data_dir / "pasar.db")
