import json
import time
import uuid

import pytest

from pasar.executor.base import LaunchRequest
from pasar.executor.systemd import SystemdExecutor

pytestmark = pytest.mark.systemd


def start(tmp_path, command):
    unit = f"pasar-job-test-{uuid.uuid4().hex[:8]}"
    (tmp_path / "launch.json").write_text(json.dumps({
        "command": command, "cwd": str(tmp_path), "env": {"PATH": "/usr/bin:/bin"},
    }))
    ex = SystemdExecutor()
    ex.launch(LaunchRequest(unit, str(tmp_path), str(tmp_path / "out.log"), 1 << 30, grace=5))
    return ex, unit


def wait_for(fn, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("timed out")


def test_success_is_kept_for_inspection(tmp_path):
    ex, unit = start(tmp_path, "echo hi")
    st = wait_for(lambda: (s := ex.status(unit)) and s.exited and s)
    assert st.exit_code == 0 and st.signal is None and st.result == "success"
    assert (tmp_path / "out.log").read_text() == "hi\n"
    ex.cleanup(unit)
    assert ex.status(unit) is None


def test_failure_exit_code(tmp_path):
    ex, unit = start(tmp_path, "exit 3")
    st = wait_for(lambda: (s := ex.status(unit)) and s.exited and s)
    assert st.exit_code == 3 and st.result == "exit-code"
    ex.cleanup(unit)


def test_running_then_stop_removes_unit(tmp_path):
    ex, unit = start(tmp_path, "sleep 60")
    st = wait_for(lambda: ex.status(unit))
    assert not st.exited and st.control_group.endswith(f"{unit}.service")
    assert unit in ex.list_units()
    ex.stop(unit)
    wait_for(lambda: ex.status(unit) is None)


def test_kill_reports_signal(tmp_path):
    ex, unit = start(tmp_path, "sleep 60")
    wait_for(lambda: ex.status(unit))
    ex.kill(unit)
    st = wait_for(lambda: (s := ex.status(unit)) and s.exited and s)
    assert st.signal == "SIGKILL"
    ex.cleanup(unit)
