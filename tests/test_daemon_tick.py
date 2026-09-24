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
    env = json.loads((daemon.job_dir(1) / "launch.json").read_text())["env"]
    assert env["A"] == "1" and env["PASAR_ATTEMPT"] == "1" and env["PASAR_RESUMING"] == "0"
    assert env["PASAR_MEM_LIMIT_BYTES"] == str(reservation(24 * GiB, daemon.pool, daemon.cfg))
    assert (daemon.job_dir(1) / "output.log").read_text().startswith("──── attempt 1")


def test_shared_job_gets_the_whole_gpu_cgroup_cap(daemon, executor, make_spec):
    # A shared job's reservation is what it is scheduled and watched against, not a hard cap:
    # its MemoryMax is the same machine-safety cap a whole-GPU job gets, so the kernel does not
    # kill it the moment it runs past its estimate while the machine has room.
    daemon.submit(make_spec(mem_request=24 * GiB))
    daemon.submit(make_spec())
    daemon.tick()
    executor.exit("pasar-job-1-1", code=0)
    daemon.tick()
    daemon.tick()
    shared, whole = executor.launched
    assert shared.unit == "pasar-job-1-1" and whole.unit == "pasar-job-2-1"
    assert shared.mem_max == whole.mem_max == daemon.pool
    assert shared.mem_max > daemon.limit(daemon.job(1))


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


def test_out_of_range_step_does_not_crash_the_tick(daemon, executor, make_spec):
    # A step outside signed 64-bit range used to reach Store.add_event and raise OverflowError
    # on every tick, wedging all scheduling; events.py now drops such a step to None instead.
    daemon.submit(make_spec())
    daemon.tick()
    (daemon.job_dir(1) / "events.jsonl").write_text(f'{{"event":"checkpoint","step":{2**63}}}\n')
    daemon.tick()
    assert daemon.job(1).state == State.RUNNING
    assert daemon.store.events(1)[0]["step"] is None


def test_poll_isolates_a_failing_jobs_exception(daemon, executor, make_spec, caplog):
    daemon.submit(make_spec(mem_request=10 * GiB))
    daemon.submit(make_spec(mem_request=10 * GiB))
    daemon.tick()  # both launch (room for both, sharing the GPU)

    orig = daemon._read_events

    def boom(job, att):
        if job.id == 1:
            raise RuntimeError("boom")
        return orig(job, att)

    daemon._read_events = boom
    executor.exit("pasar-job-2-1", code=0)
    with caplog.at_level("ERROR"):
        daemon.tick()
    assert daemon.job(1).state == State.RUNNING  # untouched by job 2's poll continuing
    assert daemon.job(2).state == State.COMPLETED
    assert "poll failed for job 1" in caplog.text


def test_corrupt_env_json_fails_launch_instead_of_crashing_the_tick(daemon, executor, make_spec):
    daemon.submit(make_spec())
    (daemon.job_dir(1) / "env.json").write_text("{not valid json")
    daemon.tick()  # must not raise
    job = daemon.job(1)
    assert job.state == State.FAILED and job.reason == "launch_error"


def test_stray_pasar_env_keys_are_dropped_before_relaunch(daemon, executor, make_spec):
    # A PASAR_* key surviving in the submitter's captured environment (e.g. a stale
    # PASAR_MEM_LIMIT_BYTES from a previous shared-memory job) must not leak into a fresh
    # whole-GPU job's launch environment.
    daemon.submit(make_spec(env={"PASAR_MEM_LIMIT_BYTES": "999", "KEEP": "1"}))
    daemon.tick()
    env = json.loads((daemon.job_dir(1) / "launch.json").read_text())["env"]
    assert env["KEEP"] == "1"
    assert "PASAR_MEM_LIMIT_BYTES" not in env  # whole GPU: pasar doesn't set this key itself
