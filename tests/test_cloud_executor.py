import json
import shutil

import pytest

from pasar.cloud.base import Phase
from pasar.cloud.bundle import Bundle, EnvSpec
from pasar.cloud.executor import (
    STOP_MARGIN,
    TERMINATE_RETRY,
    TIMEOUT_MARGIN,
    CloudExecutor,
    parse_unit,
)
from pasar.config import CloudTarget
from pasar.db import Store
from pasar.executor.base import CloudLaunchInfo, LaunchError, LaunchRequest
from tests.fakes import FakeClock
from tests.fakes_cloud import FakeProvider

TOKEN = "tok"
GRACE = 120
LIMIT = 3600


def ctl(obj):
    return "\x1epasar:" + TOKEN + " " + json.dumps(obj) + "\n"


def state_path(tmp_path, job_id=1, attempt=1):
    return tmp_path / "jobs" / str(job_id) / f"cloud-{attempt}.json"


def _raise(*args, **kw):
    raise RuntimeError("bookkeeping is broken")


class RefusesToTerminate(FakeProvider):
    """A provider that fails the first `failures` terminate calls, then behaves."""

    def __init__(self, failures=1):
        super().__init__()
        self.failures = failures

    def terminate(self, handle):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("the provider is having a moment")
        super().terminate(handle)


def make_target(**kw):
    base = {"name": "fake", "provider": "fake", "daily_budget": 10.0, "monthly_budget": 100.0}
    return CloudTarget(**{**base, **kw})


def make_executor(tmp_path, clock=None, provider=None, **target_kw):
    store = Store(tmp_path / "pasar.db")
    provider = provider or FakeProvider()
    return CloudExecutor(provider, make_target(**target_kw), store, clock or FakeClock(),
                         lambda job_id: tmp_path / "jobs" / str(job_id)), provider, store


def make_request(tmp_path, job_id=1, attempt=1, command="python train.py --lr 1e-3",
                 limit=LIMIT, gpu="H100", env=None, token=TOKEN, grace=GRACE):
    d = tmp_path / "jobs" / str(job_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "launch.json").write_text(json.dumps({"command": command, "cwd": "/repo", "env": {}}))
    bundle = Bundle(d / "b.tar", "/repo", "sub", EnvSpec({"uv.lock": b"x"}, "envkey"), 10)
    cloud = CloudLaunchInfo(job_id=job_id, attempt=attempt, bundle=bundle, gpu=gpu,
                            env=env if env is not None else {"WANDB_API_KEY": "k"},
                            limit=limit, token=token)
    return LaunchRequest(f"cloud:{make_target().name}:pending", str(d), str(d / "output.log"),
                         None, grace, cloud=cloud)


def launched(tmp_path, **kw):
    """Launch one attempt and return (executor, provider, store, unit)."""
    ex, provider, store = make_executor(tmp_path, kw.pop("clock", None), kw.pop("provider", None))
    req = make_request(tmp_path, **kw)
    ex.launch(req)
    return ex, provider, store, ex.unit_of(req.cloud.job_id, req.cloud.attempt)


def test_launch_prepares_image_and_tags_the_handle(tmp_path):
    _, provider, _, unit = launched(tmp_path)
    assert unit == "cloud:fake:sb-1"
    assert parse_unit(unit) == ("fake", "sb-1")
    assert provider.images == {"envkey": "img-envkey"}
    req = provider.boxes["sb-1"].req
    assert req.image_key == "img-envkey"
    assert (req.gpu, req.gpu_count) == ("H100", 1)
    assert req.rel_cwd == "sub" and req.env == {"WANDB_API_KEY": "k"}
    assert req.timeout == LIMIT + GRACE + TIMEOUT_MARGIN
    assert req.tags == {"pasar_job": "1", "pasar_attempt": "1", "pasar_target": "fake"}


def test_launch_runs_the_job_under_the_wrapper_with_this_attempts_token(tmp_path):
    _, provider, _, _ = launched(tmp_path)
    command = provider.boxes["sb-1"].req.command
    assert command.startswith("python -m pasar_job.run ")
    assert f"--token {TOKEN}" in command
    assert f"--limit {LIMIT}" in command and f"--grace {GRACE}" in command
    # One quoted word after --, so the wrapper hands the job's command line to bash unchanged.
    assert command.endswith(" -- 'python train.py --lr 1e-3'")


def test_each_attempts_token_is_unpredictable():
    spec = {"job_id": 1, "attempt": 1, "bundle": None, "gpu": "H100", "env": {}, "limit": 1}
    a, b = CloudLaunchInfo(**spec), CloudLaunchInfo(**spec)
    assert a.token != b.token and len(a.token) >= 16


def test_launch_caps_the_timeout_at_the_targets_max_runtime(tmp_path):
    """The wrapper's own limit has to be cut with the timeout. A sandbox killed before the
    wrapper reaches its limit leaves no exit line and reads as a reclaim, which pauses the job
    and pays to run it again from the start, over and over."""
    ex, provider, _ = make_executor(tmp_path, max_runtime=7200)
    ex.launch(make_request(tmp_path, limit=86400))
    req = provider.boxes["sb-1"].req
    assert req.timeout == 7200
    assert f"--limit {7200 - GRACE - TIMEOUT_MARGIN}" in req.command


def test_an_uncapped_limit_reaches_the_wrapper_whole(tmp_path):
    ex, provider, _ = make_executor(tmp_path, max_runtime=86400)
    ex.launch(make_request(tmp_path, limit=LIMIT))
    req = provider.boxes["sb-1"].req
    assert f"--limit {LIMIT}" in req.command
    assert req.timeout == LIMIT + GRACE + TIMEOUT_MARGIN


def test_a_max_runtime_with_no_room_for_the_grace_is_refused(tmp_path):
    ex, provider, _ = make_executor(tmp_path, max_runtime=60)
    with pytest.raises(LaunchError, match="too little"):
        ex.launch(make_request(tmp_path, limit=3600))
    assert provider.boxes == {}


def test_status_maps_phases_to_unit_state(tmp_path):
    ex, provider, _, unit = launched(tmp_path)
    for phase in (Phase.PENDING, Phase.STARTING, Phase.RUNNING):
        provider.boxes["sb-1"].phase = phase
        st = ex.status(unit)
        assert st.exited is False and st.result == phase.value
        assert st.exit_code is None and st.signal is None and st.control_group is None
    provider.emit("sb-1", ctl({"t": "exit", "code": 0, "signal": None}))
    provider.finish("sb-1", 0)
    ex.poll_output()
    st = ex.status(unit)
    assert st.exited and st.exit_code == 0 and st.signal is None and st.result == "success"


def test_exit_code_prefers_the_wrapper_over_the_provider(tmp_path):
    ex, provider, _, unit = launched(tmp_path)
    provider.start("sb-1")
    provider.emit("sb-1", ctl({"t": "exit", "code": 3, "signal": None}))
    provider.finish("sb-1", 137)
    ex.poll_output()
    st = ex.status(unit)
    assert st.exited and st.exit_code == 3 and st.result == "exit-code"


def test_signal_from_the_wrapper_is_reported(tmp_path):
    ex, provider, _, unit = launched(tmp_path)
    provider.start("sb-1")
    provider.emit("sb-1", ctl({"t": "exit", "code": None, "signal": "SIGSEGV"}))
    provider.finish("sb-1", 139)
    ex.poll_output()
    st = ex.status(unit)
    assert st.exited and st.exit_code is None and st.signal == "SIGSEGV" and st.result == "signal"


def test_unexplained_provider_exit_is_flagged_as_reclaim(tmp_path):
    ex, provider, _, unit = launched(tmp_path)
    provider.start("sb-1")
    provider.reclaim("sb-1")
    ex.poll_output()
    st = ex.status(unit)
    assert st.exited and st.result == "reclaimed" and st.exit_code == 137


def test_a_stop_we_never_asked_for_is_a_reclaim_even_when_the_wrapper_saw_the_sigterm(tmp_path):
    """A spot reclaim reaches the wrapper as a SIGTERM, so its own reason says "stopped"; only
    pasar's record of whether it asked can tell the two apart."""
    ex, provider, _, unit = launched(tmp_path)
    provider.start("sb-1")
    provider.emit("sb-1", ctl({"t": "exit", "code": None, "signal": "SIGKILL",
                               "reason": "stopped"}))
    provider.finish("sb-1", 137, by_provider=True)
    ex.poll_output()
    assert ex.status(unit).result == "reclaimed"


def test_a_clean_wrapper_exit_stays_a_success_when_the_provider_ended_it(tmp_path):
    """The provider reports 137 and "I ended it" for a sandbox it reaped after the job was
    already done. Calling that a reclaim would pause a finished job and pay to run it again."""
    ex, provider, _, unit = launched(tmp_path)
    provider.start("sb-1")
    provider.emit("sb-1", ctl({"t": "exit", "code": 0, "signal": None}))
    ex.poll_output()
    provider.finish("sb-1", 137, by_provider=True)
    st = ex.status(unit)
    assert st.exited and st.exit_code == 0 and st.result == "success"


def test_a_failing_wrapper_exit_is_not_reclassified_by_the_provider(tmp_path):
    ex, provider, _, unit = launched(tmp_path)
    provider.start("sb-1")
    provider.emit("sb-1", ctl({"t": "exit", "code": 1, "signal": None}))
    ex.poll_output()
    provider.finish("sb-1", 137, by_provider=True)
    assert ex.status(unit).result == "exit-code"


def test_the_wrappers_time_limit_surfaces_as_time_limit(tmp_path):
    ex, provider, _, unit = launched(tmp_path)
    provider.start("sb-1")
    provider.emit("sb-1", ctl({"t": "exit", "code": None, "signal": "SIGKILL",
                               "reason": "time_limit"}))
    provider.finish("sb-1", 137)
    ex.poll_output()
    assert ex.status(unit).result == "time_limit"


def test_stop_asks_gracefully_then_terminates_after_the_grace(tmp_path):
    clock = FakeClock()
    ex, provider, _, unit = launched(tmp_path, clock=clock)
    provider.start("sb-1")
    ex.stop(unit)
    assert provider.stopped == ["sb-1"] and provider.terminated == []
    clock.advance(GRACE)
    assert ex.status(unit).exited is False and provider.terminated == []
    clock.advance(STOP_MARGIN)
    st = ex.status(unit)
    assert provider.terminated == ["sb-1"]
    assert st.exited and st.result == "stopped"


def test_a_stop_the_job_honours_is_never_terminated(tmp_path):
    clock = FakeClock()
    ex, provider, _, unit = launched(tmp_path, clock=clock)
    provider.start("sb-1")
    ex.stop(unit)
    provider.emit("sb-1", ctl({"t": "exit", "code": 143, "signal": None, "reason": "stopped"}))
    provider.finish("sb-1", 143)
    ex.poll_output()
    clock.advance(GRACE + STOP_MARGIN + 60)
    st = ex.status(unit)
    assert provider.terminated == []
    assert st.exited and st.exit_code == 143 and st.result == "stopped"


def test_the_grace_deadline_fires_once_not_every_tick(tmp_path):
    clock = FakeClock()
    ex, provider, _, unit = launched(tmp_path, clock=clock)
    provider.start("sb-1")
    ex.stop(unit)
    clock.advance(GRACE + STOP_MARGIN)
    ex.status(unit)
    ex.status(unit)
    assert provider.terminated == ["sb-1"]


def test_a_terminate_the_provider_refuses_is_asked_again(tmp_path):
    clock = FakeClock()
    ex, provider, _, unit = launched(tmp_path, clock=clock, provider=RefusesToTerminate())
    provider.start("sb-1")
    ex.stop(unit)
    clock.advance(GRACE + STOP_MARGIN)
    assert ex.status(unit).exited is False  # the provider refused; nothing has ended it
    clock.advance(TERMINATE_RETRY)
    st = ex.status(unit)
    assert provider.status("sb-1").phase is Phase.EXITED
    assert st.exited and st.result == "stopped"


def test_kill_terminates_immediately(tmp_path):
    ex, provider, _, unit = launched(tmp_path)
    provider.start("sb-1")
    ex.kill(unit)
    assert provider.terminated == ["sb-1"]
    st = ex.status(unit)
    assert st.exited and st.result == "stopped"


def test_stop_and_kill_on_an_unknown_unit_do_nothing(tmp_path):
    ex, provider, _ = make_executor(tmp_path)
    ex.stop("cloud:fake:nope")
    ex.kill("cloud:fake:nope")
    assert provider.stopped == [] and provider.terminated == []
    assert ex.status("cloud:fake:nope") is None


def test_list_units_returns_live_handles_with_their_tags(tmp_path):
    ex, provider, _ = make_executor(tmp_path)
    ex.launch(make_request(tmp_path, job_id=1, attempt=2))
    ex.launch(make_request(tmp_path, job_id=7, attempt=1))
    assert ex.list_units() == ["cloud:fake:sb-1", "cloud:fake:sb-2"]
    tags = dict(provider.list())
    assert tags["sb-1"]["pasar_job"] == "1" and tags["sb-1"]["pasar_attempt"] == "2"
    assert tags["sb-2"]["pasar_job"] == "7"
    provider.forget("sb-1")
    assert ex.list_units() == ["cloud:fake:sb-2"]


def test_list_units_ignores_handles_belonging_to_another_target(tmp_path):
    provider = FakeProvider()
    ex, _, _ = make_executor(tmp_path, provider=provider)
    ex.launch(make_request(tmp_path))
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    other, _, _ = make_executor(elsewhere, provider=provider, name="spare")
    other.launch(make_request(elsewhere, job_id=2))
    assert ex.list_units() == ["cloud:fake:sb-1"]
    assert other.list_units() == ["cloud:spare:sb-2"]


def test_launch_failure_raises_LaunchError(tmp_path):
    ex, provider, _ = make_executor(tmp_path)
    provider.fail_launch = "no capacity for H100"
    with pytest.raises(LaunchError, match="no capacity"):
        ex.launch(make_request(tmp_path))
    assert ex.list_units() == [] and ex.unit_of(1, 1) is None


def test_a_bad_gpu_spec_raises_LaunchError_before_anything_is_started(tmp_path):
    ex, provider, _ = make_executor(tmp_path)
    with pytest.raises(LaunchError):
        ex.launch(make_request(tmp_path, gpu="H100:zero"))
    assert provider.boxes == {} and provider.images == {}


def test_a_missing_launch_json_raises_LaunchError(tmp_path):
    ex, provider, _ = make_executor(tmp_path)
    req = make_request(tmp_path)
    (tmp_path / "jobs" / "1" / "launch.json").unlink()
    with pytest.raises(LaunchError):
        ex.launch(req)
    assert provider.boxes == {}


def test_a_cloud_executor_refuses_a_local_launch_request(tmp_path):
    ex, _, _ = make_executor(tmp_path)
    with pytest.raises(LaunchError):
        ex.launch(LaunchRequest("pasar-job-1-1", str(tmp_path), str(tmp_path / "log"), None, 120))


def test_poll_output_writes_the_log_events_and_samples(tmp_path):
    clock = FakeClock()
    ex, provider, store, _ = launched(tmp_path, clock=clock)
    provider.start("sb-1")
    provider.emit("sb-1", "epoch 1\n")
    provider.emit("sb-1", ctl({"t": "event", "e": {"event": "progress", "step": 3}}))
    provider.emit("sb-1", ctl({"t": "sample", "gpus": [[0, 55, 1024, 81920, 240.5, 61]]}))
    ex.poll_output()
    d = tmp_path / "jobs" / "1"
    assert (d / "output.log").read_text() == "epoch 1\n"
    assert json.loads((d / "events.jsonl").read_text())["step"] == 3
    rows = store.gpu_samples(1, 1)
    assert rows == [(clock.t, 0, 55.0, 1024.0, 81920.0, 240.5, 61.0)]


def test_poll_output_keeps_going_when_one_attempt_fails(tmp_path):
    ex, provider, _ = make_executor(tmp_path)
    ex.launch(make_request(tmp_path, job_id=1))
    ex.launch(make_request(tmp_path, job_id=2))
    provider.emit("sb-1", "doomed\n")
    provider.emit("sb-2", "fine\n")
    shutil.rmtree(tmp_path / "jobs" / "1")  # its log can no longer be written
    ex.poll_output()
    assert (tmp_path / "jobs" / "2" / "output.log").read_text() == "fine\n"


def test_poll_output_is_resumable_across_a_daemon_restart(tmp_path):
    provider = FakeProvider()
    ex, _, _, unit = launched(tmp_path, provider=provider)
    provider.start("sb-1")
    provider.emit("sb-1", "one\n")
    ex.poll_output()
    provider.emit("sb-1", "partial")
    ex.poll_output()

    resumed, _, _ = make_executor(tmp_path, provider=provider)
    assert resumed.adopt(1, unit) is True
    provider.emit("sb-1", " line\ntwo\n")
    resumed.poll_output()
    resumed.poll_output()
    assert (tmp_path / "jobs" / "1" / "output.log").read_text() == "one\npartial line\ntwo\n"


def test_an_exit_seen_before_a_restart_is_still_reported_after_it(tmp_path):
    provider = FakeProvider()
    ex, _, _, unit = launched(tmp_path, provider=provider)
    provider.start("sb-1")
    provider.emit("sb-1", ctl({"t": "exit", "code": 4, "signal": None}))
    ex.poll_output()
    provider.finish("sb-1", 137)

    resumed, _, _ = make_executor(tmp_path, provider=provider)
    assert resumed.adopt(1, unit) is True
    st = resumed.status(unit)
    assert st.exited and st.exit_code == 4 and st.result == "exit-code"


def test_adopt_declines_a_unit_it_has_no_record_of(tmp_path):
    _, _, _, unit = launched(tmp_path)
    resumed, _, _ = make_executor(tmp_path)
    assert resumed.adopt(1, "cloud:fake:sb-999") is False
    assert resumed.adopt(2, unit) is False
    assert resumed.status(unit) is None


@pytest.mark.parametrize("text", ["", "{not json", '["a list"]',
                                  '{"unit": "cloud:fake:sb-1"}',
                                  '{"unit": "cloud:fake:sb-1", "attempt": 1, "handle": "sb-1"}'])
def test_adopt_declines_damaged_saved_state(tmp_path, text):
    """One unusable file must not take out startup reconciliation for every other cloud job."""
    _, _, _, unit = launched(tmp_path)
    state_path(tmp_path).write_text(text)
    resumed, _, _ = make_executor(tmp_path)
    assert resumed.adopt(1, unit) is False
    assert resumed.status(unit) is None


def test_adopt_reads_past_an_unusable_file_from_another_attempt(tmp_path):
    ex, provider, _ = make_executor(tmp_path)
    ex.launch(make_request(tmp_path, attempt=1))
    ex.launch(make_request(tmp_path, attempt=2))
    state_path(tmp_path, attempt=1).write_text("{broken")
    resumed, _, _ = make_executor(tmp_path, provider=provider)
    assert resumed.adopt(1, "cloud:fake:sb-2") is True


def test_the_wrappers_exit_ends_the_attempt_even_while_the_sandbox_lingers(tmp_path):
    ex, provider, _, unit = launched(tmp_path)
    provider.start("sb-1")
    provider.emit("sb-1", ctl({"t": "exit", "code": 0, "signal": None}))
    ex.poll_output()
    st = ex.status(unit)
    assert provider.status("sb-1").phase is Phase.RUNNING
    assert st.exited and st.exit_code == 0 and st.result == "success"


def test_a_vanished_handle_reports_nothing_so_the_daemon_can_call_it_lost(tmp_path):
    ex, provider, _, unit = launched(tmp_path)
    provider.start("sb-1")
    provider.forget("sb-1")
    assert ex.status(unit) is None


def test_a_vanished_attempt_stops_being_tracked(tmp_path):
    """Nothing keeps reading a handle the provider has forgotten, and the attempt gets one last
    terminate in case the provider only seemed to forget a sandbox that is still billing."""
    ex, provider, _, unit = launched(tmp_path)
    provider.start("sb-1")
    provider.forget("sb-1")
    assert ex.status(unit) is None
    assert ex.unit_of(1, 1) is None and not state_path(tmp_path).exists()
    assert provider.terminated == ["sb-1"]
    ex.poll_output()  # nothing left to read from
    assert ex.status(unit) is None and provider.terminated == ["sb-1"]


def test_a_handle_that_vanishes_after_its_exit_still_reports_the_exit(tmp_path):
    ex, provider, _, unit = launched(tmp_path)
    provider.start("sb-1")
    provider.emit("sb-1", ctl({"t": "exit", "code": 0, "signal": None}))
    ex.poll_output()
    provider.forget("sb-1")
    st = ex.status(unit)
    assert st.exited and st.exit_code == 0 and st.result == "success"


def test_status_drains_the_last_output_before_reporting_an_exit(tmp_path):
    """The exit control line is usually the last thing written, so an attempt that exits
    between two ticks must not be reported before its own exit status is read."""
    ex, provider, _, unit = launched(tmp_path)
    provider.start("sb-1")
    ex.poll_output()
    provider.emit("sb-1", "last\n" + ctl({"t": "exit", "code": 2, "signal": None}))
    provider.finish("sb-1", 137)
    st = ex.status(unit)
    assert st.exited and st.exit_code == 2
    assert (tmp_path / "jobs" / "1" / "output.log").read_text() == "last\n"


def test_cleanup_forgets_the_unit_and_its_state_file(tmp_path):
    ex, provider, _, unit = launched(tmp_path)
    provider.finish("sb-1", 0)
    ex.cleanup(unit)
    assert ex.status(unit) is None and ex.unit_of(1, 1) is None
    assert not state_path(tmp_path).exists()
    # Ending an attempt that is already over costs nothing and leaves its exit alone.
    assert provider.terminated == ["sb-1"]
    assert provider.status("sb-1").exit_code == 0
    ex.cleanup(unit)  # a second cleanup is harmless


def test_cleanup_ends_a_sandbox_that_outlived_its_job(tmp_path):
    ex, provider, _, unit = launched(tmp_path)
    provider.start("sb-1")
    provider.emit("sb-1", ctl({"t": "exit", "code": 0, "signal": None}))
    ex.poll_output()
    assert ex.status(unit).exited
    ex.cleanup(unit)
    assert provider.terminated == ["sb-1"]


def test_the_state_file_is_private_to_the_user(tmp_path):
    launched(tmp_path)
    assert state_path(tmp_path).stat().st_mode & 0o077 == 0
    assert json.loads(state_path(tmp_path).read_text())["token"] == TOKEN


def test_a_leftover_temporary_file_cannot_widen_the_tokens_permissions(tmp_path):
    d = tmp_path / "jobs" / "1"
    d.mkdir(parents=True)
    leftover = d / "cloud-1.json.tmp"
    leftover.touch(mode=0o644)
    launched(tmp_path)
    assert state_path(tmp_path).stat().st_mode & 0o077 == 0


def test_each_attempt_saves_its_own_state(tmp_path):
    ex, _, _ = make_executor(tmp_path)
    ex.launch(make_request(tmp_path, attempt=1))
    ex.launch(make_request(tmp_path, attempt=2))
    assert json.loads(state_path(tmp_path, attempt=1).read_text())["handle"] == "sb-1"
    assert json.loads(state_path(tmp_path, attempt=2).read_text())["handle"] == "sb-2"
    ex.cleanup(ex.unit_of(1, 1))
    assert not state_path(tmp_path, attempt=1).exists()
    assert state_path(tmp_path, attempt=2).exists()


def test_a_launch_that_cannot_be_recorded_ends_the_sandbox(tmp_path, monkeypatch):
    ex, provider, _ = make_executor(tmp_path)
    monkeypatch.setattr(CloudExecutor, "_track", _raise)
    with pytest.raises(LaunchError):
        ex.launch(make_request(tmp_path))
    assert provider.terminated == ["sb-1"]
    assert ex.unit_of(1, 1) is None
