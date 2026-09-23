"""ModalProvider against a fake SDK: no network, no credentials, no money."""

import threading
import time

import pytest

from pasar.cloud import container as c
from pasar.cloud import modal_provider
from pasar.cloud.base import Phase
from pasar.cloud.bundle import EnvSpec
from pasar.cloud.modal_provider import HANDLE_TAG, ModalProvider
from pasar.config import CloudTarget
from tests.fakes_cloud import launch_request
from tests.fakes_modal import FakeSDK, FileEntryType


def _target(**kw):
    return CloudTarget(name="modal", provider="modal", daily_budget=10.0, monthly_budget=100.0,
                       **kw)


@pytest.fixture
def provider(tmp_path):
    sdk = FakeSDK()
    p = ModalProvider(_target(), tmp_path / "state", sdk=sdk)
    yield p, sdk
    p.close()


def _wait(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _running(p, sdk, tmp_path, **kw):
    """A launched attempt whose sandbox exists and whose watcher is following it."""
    p.prepare_image(EnvSpec({}, "img"))
    req = launch_request(tmp_path, **kw)
    # `list()` finds a target's sandboxes by this tag, and fakes_cloud does not set it: the
    # executor is what adds it in production (CloudExecutor.launch), not the request builder.
    req.tags["pasar_target"] = "modal"
    handle = p.launch(req)
    box = sdk.wait_for_sandbox()
    assert _wait(lambda: p.status(handle).phase is Phase.RUNNING)
    return handle, box


class _FakeClock:
    """A clock the provider's own sleeps advance, so a two-minute wait costs no real time."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


# ---- preparing to run
def test_prepare_image_writes_the_env_files_and_caches_by_key(provider, tmp_path):
    p, _sdk = provider
    env = EnvSpec({"pyproject.toml": b"[project]", "packages/x/pyproject.toml": b"[project]"},
                  "k1")
    assert p.prepare_image(env) == "k1"
    written = tmp_path / "state" / "env" / "k1"
    assert (written / "pyproject.toml").read_bytes() == b"[project]"
    assert (written / "packages" / "x" / "pyproject.toml").read_bytes() == b"[project]"
    first = p._images["k1"]
    assert p.prepare_image(env) == "k1"
    assert p._images["k1"] is first  # cached: a second job on the same lockfile rebuilds nothing


def test_launching_an_image_nobody_prepared_refuses_before_anything_is_created(provider,
                                                                               tmp_path):
    """The executor can handle a refusal it sees synchronously; an attempt that failed inside
    the watcher thread has already been recorded, and costs a sandbox to find out."""
    p, sdk = provider
    with pytest.raises(KeyError):
        p.launch(launch_request(tmp_path))
    assert sdk.sandboxes == []
    assert p.status("h-1-1-nothing").phase is Phase.GONE


# ---- launching
def test_launch_returns_before_any_network_call(provider, tmp_path):
    """The daemon's tick runs on the loop that serves the web UI, and a cold image build takes
    minutes; a launch that waited for one would freeze the dashboard."""
    p, sdk = provider
    p.prepare_image(EnvSpec({}, "img"))
    p._pause_before_create.set()   # hold the watcher thread where a real image build would be
    try:
        handle = p.launch(launch_request(tmp_path, job_id=7, attempt=2))
        assert handle.startswith("h-7-2-")
        assert p.status(handle).phase is Phase.PENDING
        assert sdk.sandboxes == []
        time.sleep(0.05)           # and it is the hook holding it, not just the head start
        assert sdk.sandboxes == []
    finally:
        p._pause_before_create.clear()


def test_launch_tags_the_sandbox_with_our_handle(provider, tmp_path):
    p, sdk = provider
    p.prepare_image(EnvSpec({}, "img"))
    handle = p.launch(launch_request(tmp_path))
    box = sdk.wait_for_sandbox()
    assert box.tags[HANDLE_TAG] == handle
    assert box.tags["pasar_job"] == "1"


def test_launch_passes_the_gpu_count_persist_volume_and_entry_script(provider, tmp_path):
    p, sdk = provider
    p.prepare_image(EnvSpec({}, "img"))
    req = launch_request(tmp_path)
    req.gpu, req.gpu_count, req.timeout = "H100", 4, 3600
    req.volumes = {"/data": "datasets"}
    p.launch(req)
    box = sdk.wait_for_sandbox()
    assert box.kwargs["gpu"] == "H100:4"
    assert box.kwargs["timeout"] == 3600
    assert set(box.kwargs["volumes"]) == {c.PERSIST, "/data"}
    assert box.kwargs["env"]["PASAR_PERSIST_DIR"] == "/pasar/persist/1"
    assert box.args[0] == "bash" and box.args[1] == "-c"
    assert "tar -xf" in box.args[2]


def test_a_sandbox_with_no_gpu_asks_modal_for_none(provider, tmp_path):
    """A CPU-only attempt must not be billed for an accelerator it never asked for."""
    p, sdk = provider
    p.prepare_image(EnvSpec({}, "img"))
    req = launch_request(tmp_path)
    req.gpu, req.gpu_count = "", 0
    p.launch(req)
    box = sdk.wait_for_sandbox()
    assert "gpu" not in box.kwargs


def test_the_persist_volume_is_created_but_a_named_one_is_not(provider, tmp_path):
    """Creating a volume the target named but the workspace does not have would mount an empty
    directory where a dataset was meant to be, and the job would fail minutes in, having paid."""
    p, sdk = provider
    p.prepare_image(EnvSpec({}, "img"))
    req = launch_request(tmp_path)
    req.volumes = {"/data": "datasets"}
    p.launch(req)
    sdk.wait_for_sandbox()
    by_name = {v.name: v.create_if_missing for v in sdk.volumes}
    assert by_name["pasar-modal"] is True
    assert by_name["datasets"] is False


def test_a_launch_that_fails_ends_the_attempt_with_the_reason_in_its_log(provider, tmp_path):
    p, sdk = provider
    sdk.create_error = "no capacity for H100"
    p.prepare_image(EnvSpec({}, "img"))
    handle = p.launch(launch_request(tmp_path))
    assert _wait(lambda: p.status(handle).phase is Phase.EXITED)
    assert p.status(handle).exit_code != 0
    data, _ = p.read_output(handle, 0)
    assert b"no capacity for H100" in data
    assert data.endswith(b"\n")  # the pump only ever consumes whole lines


def test_a_watcher_that_dies_still_ends_the_attempt(provider, tmp_path):
    """A box left in a phase nothing will move again wedges the job for ever: the daemon sees
    `done=False` on every tick while the sandbox goes on billing to its own timeout."""
    p, sdk = provider

    def no_threads_left(box, sb):
        raise RuntimeError("can't start new thread")

    p._stream = no_threads_left
    p.prepare_image(EnvSpec({}, "img"))
    handle = p.launch(launch_request(tmp_path))
    box = sdk.wait_for_sandbox()
    assert _wait(lambda: p.status(handle).phase is Phase.EXITED)
    assert _wait(lambda: box.terminated)
    assert b"can't start new thread" in p.read_output(handle, 0)[0]


# ---- following an attempt's output
def test_output_is_served_from_exactly_the_cursor_asked_for(provider, tmp_path):
    p, sdk = provider
    handle, box = _running(p, sdk, tmp_path)
    box.emit("alpha\nbeta\n")
    assert _wait(lambda: p.read_output(handle, 0)[0] == b"alpha\nbeta\n")
    assert p.read_output(handle, 6)[0] == b"beta\n"
    assert p.read_output(handle, 11)[0] == b""
    box.emit("gamma\n")
    assert _wait(lambda: p.read_output(handle, 11)[0] == b"gamma\n")
    assert p.read_output(handle, 17) == (b"", 17)


def test_reading_forward_then_back_to_an_unconsumed_tail_still_works(provider, tmp_path):
    """The pump stops at the last complete line, so it re-asks for the tail it did not take."""
    p, sdk = provider
    handle, box = _running(p, sdk, tmp_path)
    box.emit("one\ntwo")
    assert _wait(lambda: p.read_output(handle, 0)[0] == b"one\n")
    box.emit(" more\n")
    assert _wait(lambda: p.read_output(handle, 4)[0] == b"two more\n")
    assert p.read_output(handle, 4)[0] == b"two more\n"   # asking twice returns the same bytes


def test_reading_behind_what_was_already_handed_over_refuses(provider, tmp_path):
    """Serving a window that starts anywhere but the cursor splices the job's log together out
    of order, silently; a cursor that has gone backwards is a bug worth hearing about."""
    p, sdk = provider
    handle, box = _running(p, sdk, tmp_path)
    box.emit("alpha\nbeta\n")
    assert _wait(lambda: p.read_output(handle, 6)[0] == b"beta\n")
    with pytest.raises(ValueError, match="behind"):
        p.read_output(handle, 0)


def test_stderr_lands_in_the_same_stream(provider, tmp_path):
    p, sdk = provider
    handle, box = _running(p, sdk, tmp_path)
    box.emit("Traceback\n", stream="stderr")
    assert _wait(lambda: b"Traceback\n" in p.read_output(handle, 0)[0])


def test_a_partial_line_never_splices_into_the_other_stream(provider, tmp_path):
    """Both streams share one buffer, and the wrapper's \x1e control lines are the only record
    of a job's exit status, its events and its GPU samples: half a line of stdout landing inside
    one loses all of it, and the attempt then ends with no account of itself."""
    p, sdk = provider
    handle, box = _running(p, sdk, tmp_path)
    box.emit("\x1epasar:control-")
    assert _wait(lambda: box.stdout.handed == 1)   # the reader has it, and is holding it
    assert p.read_output(handle, 0)[0] == b""
    box.emit("TRACEBACK\n", stream="stderr")
    assert _wait(lambda: p.read_output(handle, 0)[0] == b"TRACEBACK\n")
    box.emit("line\n")
    assert _wait(lambda: p.read_output(handle, 0)[0] == b"TRACEBACK\n\x1epasar:control-line\n")


def test_a_last_line_with_no_newline_still_reaches_the_log(provider, tmp_path):
    """The pump only consumes whole lines, so a job whose last write has no newline would
    otherwise have it dropped — including a traceback's last frame."""
    p, sdk = provider
    handle, box = _running(p, sdk, tmp_path)
    box.emit("no newline here")
    box.finish(0)
    assert _wait(lambda: p.status(handle).phase is Phase.EXITED)
    assert p.read_output(handle, 0)[0] == b"no newline here\n"


# ---- how it ended
def test_status_reports_the_exit_code_once_the_sandbox_ends(provider, tmp_path):
    p, sdk = provider
    handle, box = _running(p, sdk, tmp_path)
    box.finish(0)
    assert _wait(lambda: p.status(handle).phase is Phase.EXITED)
    st = p.status(handle)
    assert st.exit_code == 0 and st.ended_by_provider is False
    assert st.console_url.startswith("https://modal.test/")
    assert st.gpu_type == "H100"


def test_an_unexplained_137_reads_as_the_provider_ending_it(provider, tmp_path):
    """Modal reports 137 for every termination, so the executor needs to know when pasar did not
    ask — that is a reclaim, and a reclaim pauses the job instead of failing it."""
    p, sdk = provider
    handle, box = _running(p, sdk, tmp_path)
    box.finish(137)
    assert _wait(lambda: p.status(handle).phase is Phase.EXITED)
    assert p.status(handle).ended_by_provider is True


def test_a_137_pasar_asked_for_is_not_a_reclaim(provider, tmp_path):
    p, sdk = provider
    handle, _box = _running(p, sdk, tmp_path)
    p.terminate(handle)
    assert _wait(lambda: p.status(handle).phase is Phase.EXITED)
    st = p.status(handle)
    assert st.exit_code == 137 and st.ended_by_provider is False


def test_an_exit_status_that_never_arrives_is_a_failure_not_a_reclaim(tmp_path):
    """Pasar being unable to read a status is not evidence that Modal took the machine away.
    Recording a reclaim there pauses the job, asks a person to approve another paid attempt, and
    does not count toward PAUSE_LIMIT, so it can repeat without bound."""
    sdk = FakeSDK()
    clock = _FakeClock()
    p = ModalProvider(_target(), tmp_path / "state", sdk=sdk, clock=clock, sleep=clock.sleep)
    try:
        handle, box = _running(p, sdk, tmp_path)
        box.hangup()                      # output ends; no exit code is ever reported
        assert _wait(lambda: p.status(handle).phase is Phase.EXITED)
        st = p.status(handle)
        assert st.exit_code is None
        assert st.ended_by_provider is False
        assert b"never reported an exit status" in p.read_output(handle, 0)[0]
    finally:
        p.close()


def test_output_that_stops_early_says_so_in_the_job_log(provider, tmp_path):
    """A stream that drops cannot be reconnected, so the log simply stops. Unmarked, that reads
    exactly like a job that went quiet, which is hours of guessing on a long run."""
    p, sdk = provider
    handle, box = _running(p, sdk, tmp_path)
    box.stdout.fail("the stream was reset")
    assert _wait(lambda: b"modal stopped sending this attempt's stdout" in
                 p.read_output(handle, 0)[0])


# ---- stopping an attempt
def test_request_stop_signals_pid_one(provider, tmp_path):
    p, sdk = provider
    handle, box = _running(p, sdk, tmp_path)
    p.request_stop(handle)
    assert box.execs == [("bash", "-c", "kill -TERM 1")]


def test_terminate_before_the_sandbox_exists_still_ends_it(provider, tmp_path):
    """The window between launch() returning and Sandbox.create landing is exactly where a
    sandbox nobody is watching would go on billing to its own 24-hour timeout."""
    p, sdk = provider
    p.prepare_image(EnvSpec({}, "img"))
    p._pause_before_create.set()     # test hook: hold the thread just before Sandbox.create
    handle = p.launch(launch_request(tmp_path))
    p.terminate(handle)
    p._pause_before_create.clear()
    box = sdk.wait_for_sandbox()
    assert _wait(lambda: box.terminated)


def test_a_terminate_modal_refuses_reaches_the_caller(provider, tmp_path):
    """CloudExecutor._end turns a raise into a False and asks again at TERMINATE_RETRY. A
    refusal reported as success is a rented GPU billing on with pasar sure it has ended."""
    p, sdk = provider
    handle, box = _running(p, sdk, tmp_path)
    box.terminate_error = "503 from the control plane"
    with pytest.raises(RuntimeError, match="503"):
        p.terminate(handle)
    assert box.terminated is False


def test_terminating_a_handle_this_process_never_launched_finds_it_by_tag(provider, tmp_path):
    """After a pasard restart the provider has no box for a live attempt; a terminate that
    quietly did nothing would leave it billing with nobody's name on it."""
    p, sdk = provider
    handle, box = _running(p, sdk, tmp_path)
    fresh = ModalProvider(_target(), tmp_path / "state2", sdk=sdk)
    try:
        fresh.terminate(handle)
        assert box.terminated is True
    finally:
        fresh.close()


def test_stopping_a_handle_this_process_never_launched_finds_it_by_tag(provider, tmp_path):
    p, sdk = provider
    handle, box = _running(p, sdk, tmp_path)
    fresh = ModalProvider(_target(), tmp_path / "state2", sdk=sdk)
    try:
        fresh.request_stop(handle)
        assert box.execs == [("bash", "-c", "kill -TERM 1")]
    finally:
        fresh.close()


# ---- re-attaching, listing, pricing
def test_status_of_an_unknown_handle_finds_it_by_tag(provider, tmp_path):
    """After a pasard restart the provider has never seen the handle, but the attempt is still
    running and still billing; the tag is how it is picked back up."""
    p, sdk = provider
    handle, box = _running(p, sdk, tmp_path)
    fresh = ModalProvider(_target(), tmp_path / "state2", sdk=sdk)
    try:
        assert _wait(lambda: fresh.status(handle).phase is Phase.RUNNING)
        box.emit("after the restart\n")
        assert _wait(lambda: b"after the restart\n" in fresh.read_output(handle, 0)[0])
    finally:
        fresh.close()


def test_status_of_a_handle_no_sandbox_carries_is_gone(provider, tmp_path):
    p, _sdk = provider
    assert _wait(lambda: p.status("h-9-1-deadbeef").phase is Phase.GONE)


def test_list_returns_our_handles_with_their_tags(provider, tmp_path):
    p, sdk = provider
    handle, box = _running(p, sdk, tmp_path)
    assert p.list() == [(handle, box.tags)]
    box.finish(0)
    assert p.list() == []  # a sandbox that has ended is not a stray to go and terminate


def test_rates_are_served_from_memory_after_the_first_call(provider, tmp_path):
    p, sdk = provider
    first = p.rates()
    assert first["gpu_hour_cost_t4"] == 0.59
    sdk.rates_value = {"gpu_hour_cost_t4": 99.0}
    assert p.rates()["gpu_hour_cost_t4"] == 0.59  # still cached; a thread refreshes it later


def test_rates_are_aliased_so_a_gpu_spelt_with_a_dash_is_priced(provider, tmp_path):
    """`--gpu A100-80GB` becomes the key `gpu_hour_cost_a100-80gb`, and Modal spells its own with
    underscores; an unpriced GPU refuses to estimate, so the job would never start."""
    p, _sdk = provider
    rates = p.rates()
    assert rates["gpu_hour_cost_a100-80gb"] == 2.5
    assert rates["gpu_hour_cost_a100_80gb"] == 2.5


# ---- one client per account
def test_the_provider_passes_its_own_client_to_every_call(tmp_path, monkeypatch):
    """Four accounts in one process: a call that forgets the client runs on whichever profile
    happens to be active in ~/.modal.toml, and bills the wrong person."""
    config = tmp_path / "modal.toml"
    config.write_text('[alice]\ntoken_id = "alice"\ntoken_secret = "alice-secret"\n')
    monkeypatch.setenv("MODAL_CONFIG_PATH", str(config))
    sdk = FakeSDK()
    target = _target(profile="alice")
    p = ModalProvider(target, tmp_path / "s", sdk=sdk)
    try:
        p.prepare_image(EnvSpec({}, "img"))
        req = launch_request(tmp_path)
        req.tags["pasar_target"] = "modal"
        p.launch(req)
        box = sdk.wait_for_sandbox()
        assert box.kwargs["client"] is sdk.clients["alice"]
        assert sdk.app_clients == [sdk.clients["alice"]]
        assert all(c is sdk.clients["alice"] for c in sdk.volume_clients)
        p.rates()
        assert sdk.workspace_clients == [sdk.clients["alice"]]
    finally:
        p.close()


def test_a_target_with_no_profile_passes_no_client(provider, tmp_path):
    """Single-account setups are unchanged: no `profile` means no client, which is what the SDK
    does on its own when the argument is left out entirely."""
    p, sdk = provider
    p.prepare_image(EnvSpec({}, "img"))
    p.launch(launch_request(tmp_path))
    box = sdk.wait_for_sandbox()
    assert box.kwargs["client"] is None
    assert sdk.app_clients == [None]


def test_persist_usage_asks_for_the_volume_with_the_target_client(tmp_path, monkeypatch):
    """persist_usage, download_persist and delete_persist all go through `_volume`, so this one
    stands in for all three: whichever volume they fetch has to carry the target's client too."""
    config = tmp_path / "modal.toml"
    config.write_text('[alice]\ntoken_id = "alice"\ntoken_secret = "alice-secret"\n')
    monkeypatch.setenv("MODAL_CONFIG_PATH", str(config))
    sdk = FakeSDK()
    target = _target(profile="alice")
    p = ModalProvider(target, tmp_path / "s", sdk=sdk)
    try:
        assert p.persist_usage(1) == (0, 0)
        assert sdk.volume_clients == [sdk.clients["alice"]]
    finally:
        p.close()


def test_upload_says_it_is_not_built(provider):
    p, _ = provider
    with pytest.raises(NotImplementedError):
        p.upload(["/tmp/x"], "k")
    assert p.billed_cost(["h-1-1-aa"], 0.0) is None


# ---- a job's persist dir
def test_persist_usage_of_a_job_with_no_directory_is_zero(provider, tmp_path):
    p, _sdk = provider
    assert p.persist_usage(1) == (0, 0)


def test_persist_usage_counts_files_and_bytes_under_the_job(provider, tmp_path):
    p, _sdk = provider
    volume = p._volume("pasar-modal", create=False)
    volume.files["1/ckpt.txt"] = b"checkpoint bytes"
    volume.files["1/sub/log.txt"] = b"a log line\n" * 50
    assert p.persist_usage(1) == (2, len(b"checkpoint bytes") + len(b"a log line\n" * 50))
    # a sibling job's files are never counted
    volume.files["2/other.txt"] = b"not this job's"
    assert p.persist_usage(1) == (2, len(b"checkpoint bytes") + len(b"a log line\n" * 50))


def test_download_persist_writes_every_file_with_its_bytes_intact(provider, tmp_path):
    p, _sdk = provider
    volume = p._volume("pasar-modal", create=False)
    ckpt, log = b"checkpoint bytes", b"a log line\n" * 50
    volume.files["1/ckpt.txt"] = ckpt
    volume.files["1/log.txt"] = log
    dest = tmp_path / "out"
    counts = p.download_persist(1, dest)
    assert counts == (2, len(ckpt) + len(log))
    assert (dest / "ckpt.txt").read_bytes() == ckpt
    assert (dest / "log.txt").read_bytes() == log


def test_download_persist_of_no_directory_writes_nothing(provider, tmp_path):
    p, _sdk = provider
    dest = tmp_path / "out"
    assert p.download_persist(1, dest) == (0, 0)
    assert not dest.exists()


def test_download_persist_of_a_nested_path_recreates_the_nesting(provider, tmp_path):
    p, _sdk = provider
    volume = p._volume("pasar-modal", create=False)
    volume.files["1/a/b/c.txt"] = b"deep"
    dest = tmp_path / "out"
    assert p.download_persist(1, dest) == (1, 4)
    assert (dest / "a" / "b" / "c.txt").read_bytes() == b"deep"


def test_delete_persist_removes_only_that_jobs_directory(provider, tmp_path):
    p, _sdk = provider
    volume = p._volume("pasar-modal", create=False)
    volume.files["1/ckpt.txt"] = b"job one"
    volume.files["2/ckpt.txt"] = b"job two"
    p.delete_persist(1)
    assert p.persist_usage(1) == (0, 0)
    assert volume.files == {"2/ckpt.txt": b"job two"}


def test_delete_persist_of_an_absent_directory_does_not_raise(provider, tmp_path):
    p, _sdk = provider
    p.delete_persist(1)  # nothing was ever written; no exception


def test_download_persist_raises_if_a_file_writes_short(provider, tmp_path):
    """The caller compares this method's return against `persist_usage`'s before deleting the
    only copy; that check is worthless if the return is just the remote listing's sizes copied
    back, rather than a count of what was actually written to disk."""
    p, _sdk = provider
    volume = p._volume("pasar-modal", create=False)
    volume.files["1/ckpt.txt"] = b"checkpoint bytes"
    volume.short_reads.add("1/ckpt.txt")  # read_file yields one byte fewer than listdir reported
    with pytest.raises(RuntimeError):
        p.download_persist(1, tmp_path / "out")


def test_persist_usage_raises_on_an_entry_it_does_not_understand(provider, tmp_path):
    """A symlink (e.g. a `latest -> step_1000` checkpoint pointer) silently dropped from the
    count would still be destroyed, unnoticed, by a later recursive delete."""
    p, _sdk = provider
    volume = p._volume("pasar-modal", create=False)
    volume.files["1/ckpt.txt"] = b"checkpoint bytes"
    volume.special["1/latest"] = FileEntryType.SYMLINK
    with pytest.raises(RuntimeError):
        p.persist_usage(1)


def test_download_persist_raises_on_an_entry_it_does_not_understand(provider, tmp_path):
    p, _sdk = provider
    volume = p._volume("pasar-modal", create=False)
    volume.files["1/ckpt.txt"] = b"checkpoint bytes"
    volume.special["1/latest"] = FileEntryType.SYMLINK
    with pytest.raises(RuntimeError):
        p.download_persist(1, tmp_path / "out")


def test_volume_cache_keys_on_the_create_flag(provider, tmp_path):
    """A non-creating handle fetched for persist_usage must not be handed back to launch(),
    which needs a creating one — see `_volume`."""
    p, sdk = provider
    p.persist_usage(1)  # caches a non-creating handle for "pasar-modal"
    p.prepare_image(EnvSpec({}, "img"))
    p.launch(launch_request(tmp_path))
    box = sdk.wait_for_sandbox()
    assert box.kwargs["volumes"][c.PERSIST].create_if_missing is True


@pytest.mark.parametrize("bad", [0, -1, "1", True, 1.5])
def test_persist_methods_reject_a_bad_job_id(provider, tmp_path, bad):
    """Stringified into a recursive delete, an empty, negative or otherwise wrong job_id could
    land on or above the volume root; `bool` is an `int` subclass, so it must be rejected too."""
    p, _sdk = provider
    with pytest.raises(ValueError):
        p.persist_usage(bad)
    with pytest.raises(ValueError):
        p.download_persist(bad, tmp_path / "out")
    with pytest.raises(ValueError):
        p.delete_persist(bad)


def test_download_persist_rejects_a_path_that_escapes_the_job_dir(provider, tmp_path):
    """`PurePosixPath.relative_to` does not normalise `..`, so a server-supplied entry path
    like `1/../../escape.txt` must be rejected rather than written outside `dest`."""
    p, _sdk = provider
    volume = p._volume("pasar-modal", create=False)
    volume.files["1/../../escape.txt"] = b"nope"
    with pytest.raises(ValueError):
        p.download_persist(1, tmp_path / "out")


# ---- shutting down
def test_close_stops_following_and_late_output_goes_nowhere(provider, tmp_path):
    """A reader thread cannot be interrupted, so the one thing close() can promise is that a
    reader still delivering chunks grows nothing nobody will ever read."""
    p, sdk = provider
    handle, box = _running(p, sdk, tmp_path)
    box.emit("before\n")
    assert _wait(lambda: p.read_output(handle, 0)[0] == b"before\n")
    p.close()
    box.emit("after\n")
    time.sleep(0.1)
    assert p.read_output(handle, 7)[0] == b""


def test_close_spends_one_join_budget_on_every_watcher_not_one_each(provider, tmp_path,
                                                                    monkeypatch):
    """A watcher parked inside Sandbox.create cannot notice `_closing` at all. Waiting each of
    those out in turn stalls a systemd stop until it turns into a SIGKILL — which is itself how
    a sandbox is orphaned."""
    p, sdk = provider
    monkeypatch.setattr(modal_provider, "JOIN_TIMEOUT", 0.3)
    gate = threading.Event()
    real_create = sdk.Sandbox.create

    def slow_create(*args, **kwargs):
        gate.wait(5.0)      # a cold image build, as far as the watcher can tell
        return real_create(*args, **kwargs)

    monkeypatch.setattr(sdk.Sandbox, "create", staticmethod(slow_create))
    p.prepare_image(EnvSpec({}, "img"))
    for _ in range(3):
        p.launch(launch_request(tmp_path))
    started = time.monotonic()
    p.close()
    elapsed = time.monotonic() - started
    gate.set()
    assert elapsed < 0.9    # one 0.3s budget for all three, not 0.3s each
