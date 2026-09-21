import json

from pasar.memory import reservation
from pasar.models import EndKind, State
from pasar.units import GiB


def test_launch_sets_limit_env_and_log_separator(daemon, executor, make_spec):
    daemon.submit(make_spec(mem_request=24 * GiB, env={"A": "1"}))
    daemon.tick()
    assert daemon.job(1).state == State.RUNNING
    req = executor.launched[0]
    assert req.unit == "pasar-job-1-1"
    assert req.mem_max == reservation(24 * GiB, daemon.pool, daemon.cfg)
    env = json.loads((daemon.job_dir(1) / "launch.json").read_text())["env"]
    assert env["A"] == "1" and env["PASAR_ATTEMPT"] == "1" and env["PASAR_RESUMING"] == "0"
    assert env["PASAR_MEM_LIMIT_BYTES"] == str(req.mem_max)
    assert (daemon.job_dir(1) / "output.log").read_text().startswith("──── attempt 1")


def test_whole_gpu_job_has_no_mem_limit_env(daemon, make_spec):
    daemon.submit(make_spec())
    daemon.tick()
    env = json.loads((daemon.job_dir(1) / "launch.json").read_text())["env"]
    assert "PASAR_MEM_LIMIT_BYTES" not in env


def test_completion(daemon, executor, make_spec):
    daemon.submit(make_spec())
    daemon.tick()
    executor.exit("pasar-job-1-1", code=0)
    daemon.tick()
    assert daemon.job(1).state == State.COMPLETED
    assert daemon.store.attempts(1)[0].end_kind == EndKind.COMPLETED
    assert "pasar-job-1-1" not in executor.units


def test_failure_is_diagnosed_from_log(daemon, executor, make_spec):
    daemon.submit(make_spec())
    daemon.tick()
    with (daemon.job_dir(1) / "output.log").open("a") as f:
        f.write("Traceback (most recent call last):\nValueError: bad shape\n")
    executor.exit("pasar-job-1-1", code=1)
    daemon.tick()
    job = daemon.job(1)
    assert job.state == State.FAILED and job.reason == "exit" and job.summary == "ValueError: bad shape"
    assert "ValueError: bad shape" in daemon.store.attempts(1)[0].log_tail


def test_retries_requeue_and_relaunch(daemon, executor, make_spec):
    daemon.submit(make_spec(retries=1))
    daemon.tick()
    executor.exit("pasar-job-1-1", code=1)
    daemon.tick()  # fails, requeues, relaunches in the same pass
    job = daemon.job(1)
    assert job.state == State.RUNNING and job.retries_used == 1
    env = json.loads((daemon.job_dir(1) / "launch.json").read_text())["env"]
    assert env["PASAR_ATTEMPT"] == "2" and env["PASAR_RESUMING"] == "1"
    executor.exit("pasar-job-1-2", code=1)
    daemon.tick()
    assert daemon.job(1).state == State.FAILED


def test_vanished_unit_is_lost(daemon, executor, make_spec):
    daemon.submit(make_spec())
    daemon.tick()
    executor.units.clear()
    daemon.tick()
    job = daemon.job(1)
    assert job.state == State.FAILED and job.reason == "lost"


def test_launch_error(daemon, executor, make_spec):
    executor.fail_launch = "Unit pasar-job-1-1.service already exists"
    daemon.submit(make_spec())
    daemon.tick()
    job = daemon.job(1)
    assert job.state == State.FAILED and job.reason == "launch_error"
    assert "already exists" in job.summary


def test_events_are_recorded_with_attempt(daemon, clock, make_spec):
    daemon.submit(make_spec())
    daemon.tick()
    (daemon.job_dir(1) / "events.jsonl").write_text(
        '{"event":"progress","step":5,"total_steps":50}\nnot json\n')
    clock.advance(3)
    daemon.tick()
    events = daemon.store.events(1)
    assert [(e["kind"], e["attempt"], e["ts"]) for e in events] == [("progress", 1, clock.t)]
    assert daemon.job(1).events_offset > 0


def test_usage_and_external_memory(daemon, executor, probe, make_spec):
    daemon.submit(make_spec(mem_request=10 * GiB))
    daemon.tick()
    cg = executor.units["pasar-job-1-1"].control_group
    probe.cg_mem[cg] = 1 * GiB
    probe.cg_pids[cg] = [100, 101]
    probe.gpu = {100: 4 * GiB, 101: 1 * GiB, 999: 3 * GiB}
    daemon.tick()
    assert daemon.usage[1] == 6 * GiB and daemon.external == 3 * GiB
    assert daemon.store.current_attempt(1).peak_mem == 6 * GiB
