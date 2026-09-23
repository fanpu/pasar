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
