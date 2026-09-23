"""ModalProvider against a fake SDK: no network, no credentials, no money."""

import time

import pytest

from pasar.cloud import container as c
from pasar.cloud.base import Phase
from pasar.cloud.bundle import EnvSpec
from pasar.cloud.modal_provider import HANDLE_TAG, ModalProvider
from pasar.config import CloudTarget
from tests.fakes_cloud import launch_request
from tests.fakes_modal import FakeSDK


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
