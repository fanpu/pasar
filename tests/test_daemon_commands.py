import json
import shutil

import pytest

from pasar.daemon import Conflict, NotFound
from pasar.models import State
from pasar.units import GiB


def test_submit_writes_job_files(daemon, make_spec):
    job = daemon.submit(make_spec(env={"HF_TOKEN": "x"}, note="why"))
    assert job.id == 1 and job.state == State.QUEUED and job.spec.name == "train"
    d = daemon.job_dir(1)
    assert json.loads((d / "env.json").read_text()) == {"HF_TOKEN": "x"}
    assert (d / "env.json").stat().st_mode & 0o777 == 0o600
    assert "HF_TOKEN" not in (d / "spec.json").read_text()
    assert daemon.job(1).spec.env is None


@pytest.mark.parametrize("kw,msg", [
    ({"command": "  "}, "command"),
    ({"est_runtime": 0}, "runtime"),
    ({"mem_request": 200 * GiB}, "pool"),
    ({"bid": -1}, "negative"),
    ({"cwd": "/does/not/exist"}, "directory"),
])
def test_submit_validation(daemon, make_spec, kw, msg):
    with pytest.raises(ValueError, match=msg):
        daemon.submit(make_spec(**kw))


def test_cancel_queued_job(daemon, make_spec):
    daemon.submit(make_spec())
    job = daemon.cancel(1)
    assert job.state == State.CANCELLED and job.reason == "cancelled"
    with pytest.raises(Conflict):
        daemon.cancel(1)
    with pytest.raises(NotFound):
        daemon.cancel(99)


def test_cancel_running_job_stops_its_unit(daemon, executor, make_spec):
    daemon.submit(make_spec())
    daemon.tick()
    job = daemon.cancel(1)
    assert job.state == State.STOPPING and job.stop_requested == "cancel"
    assert executor.stopped == ["pasar-job-1-1"]


def test_set_bid(daemon, make_spec):
    daemon.submit(make_spec())
    assert daemon.set_bid(1, 1500).bid == 1500
    with pytest.raises(ValueError):
        daemon.set_bid(1, -5)


def test_restart_only_finished_jobs_and_applies_overrides(daemon, clock, make_spec):
    daemon.submit(make_spec(mem_request=10 * GiB))
    with pytest.raises(Conflict):
        daemon.restart(1)
    daemon.cancel(1)
    clock.advance(100)
    job = daemon.restart(1, mem_request=20 * GiB, est_runtime=60, bid=1200, retries=2)
    assert job.state == State.QUEUED and job.queue_time == clock.t and job.bid == 1200
    assert job.spec.mem_request == 20 * GiB and job.spec.est_runtime == 60 and job.spec.retries == 2
    job = daemon.cancel(1)
    job = daemon.restart(1, mem_request=None)
    assert job.spec.mem_request is None


# ---- controller decisions: focused tests for daemon.py behaviour beyond the brief


def test_whole_gpu_job_launches_despite_external_gpu_usage(daemon, executor, probe, make_spec):
    # A foreign GPU process (not owned by any pasar job) is using memory. A queued whole-GPU
    # job (mem_request=None) must still be sized against pool - external, not the raw pool,
    # or it would never fit and would be blocked forever. But the cgroup cap it actually
    # launches with (MemoryMax) must stay the stable full pool, not pool - external, or the
    # cap would shrink (or hit zero) whenever a foreign process is using GPU memory.
    probe.gpu = {424242: 5 * GiB}
    daemon.submit(make_spec())
    daemon.tick()
    job = daemon.job(1)
    assert job.state == State.RUNNING
    assert executor.launched
    assert executor.launched[0].mem_max == daemon.pool


def test_restart_raises_conflict_when_job_directory_is_gone(daemon, make_spec):
    daemon.submit(make_spec())
    daemon.cancel(1)
    shutil.rmtree(daemon.job_dir(1))
    with pytest.raises(Conflict, match="resubmit"):
        daemon.restart(1)


def test_restart_validates_overrides_like_submit(daemon, make_spec):
    daemon.submit(make_spec())
    daemon.cancel(1)
    with pytest.raises(ValueError, match="negative"):
        daemon.restart(1, bid=-5)
    with pytest.raises(ValueError, match="pool"):
        daemon.restart(1, mem_request=200 * GiB)
    assert daemon.job(1).state == State.CANCELLED


def test_launch_fails_when_cwd_no_longer_exists(daemon, executor, make_spec, tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    daemon.submit(make_spec(cwd=str(gone)))
    gone.rmdir()
    daemon.tick()
    job = daemon.job(1)
    assert job.state == State.FAILED and job.reason == "launch_error"
    assert "gone" in job.summary
    assert executor.launched == []


def test_launch_env_gets_a_minimal_base_when_no_captured_env(daemon, make_spec):
    daemon.submit(make_spec())  # env not provided -> stored env.json is {}
    daemon.tick()
    launch = json.loads((daemon.job_dir(1) / "launch.json").read_text())
    assert "PATH" in launch["env"]


def test_launch_missing_env_json_fails_the_attempt_instead_of_raising(daemon, executor, make_spec):
    daemon.submit(make_spec())
    (daemon.job_dir(1) / "env.json").unlink()
    daemon.tick()  # must not raise
    job = daemon.job(1)
    assert job.state == State.FAILED and job.reason == "launch_error"
    assert executor.launched == []
