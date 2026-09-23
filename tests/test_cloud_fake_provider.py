import pytest

from pasar.cloud.base import Phase
from tests.fakes_cloud import FakeProvider, launch_request


def test_launch_then_read_output_incrementally(tmp_path):
    p = FakeProvider()
    h = p.launch(launch_request(tmp_path))
    assert p.status(h).phase is Phase.PENDING
    p.start(h)
    p.emit(h, "hello\n")
    data, cursor = p.read_output(h, 0)
    assert data == b"hello\n"
    p.emit(h, "world\n")
    data, cursor = p.read_output(h, cursor)
    assert data == b"world\n"


def test_replay_from_zero_after_a_restart(tmp_path):
    p = FakeProvider()
    h = p.launch(launch_request(tmp_path))
    p.start(h)
    p.emit(h, "a\nb\n")
    p.read_output(h, 0)
    assert p.read_output(h, 0)[0] == b"a\nb\n"


def test_graceful_stop_then_exit(tmp_path):
    p = FakeProvider()
    h = p.launch(launch_request(tmp_path))
    p.start(h)
    p.request_stop(h)
    assert p.stopped == [h]
    p.finish(h, 143)
    st = p.status(h)
    assert st.phase is Phase.EXITED and st.exit_code == 143 and not st.ended_by_provider


def test_provider_reclaim_is_flagged(tmp_path):
    p = FakeProvider()
    h = p.launch(launch_request(tmp_path))
    p.start(h)
    p.reclaim(h)
    st = p.status(h)
    assert st.phase is Phase.EXITED and st.ended_by_provider


def test_terminate_after_exit_is_a_noop(tmp_path):
    p = FakeProvider()
    h = p.launch(launch_request(tmp_path))
    p.start(h)
    p.reclaim(h)
    p.terminate(h)
    assert p.terminated == [h]
    st = p.status(h)
    assert st.phase is Phase.EXITED and st.exit_code == 137 and st.ended_by_provider


def test_forgotten_handle_reports_gone(tmp_path):
    p = FakeProvider()
    h = p.launch(launch_request(tmp_path))
    p.start(h)
    p.emit(h, "hi\n")
    p.forget(h)
    assert p.status(h).phase is Phase.GONE
    assert p.read_output(h, 3) == (b"", 3)
    assert h not in [handle for handle, _ in p.list()]


def test_failed_launch_creates_no_box(tmp_path):
    p = FakeProvider()
    p.fail_launch = "capacity"
    with pytest.raises(RuntimeError):
        p.launch(launch_request(tmp_path))
    assert p.boxes == {}
    assert p.list() == []
