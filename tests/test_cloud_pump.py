from pasar.cloud.pump import Pump
from tests.fakes_cloud import FakeProvider, launch_request

TOKEN = "tok"


def pump(tmp_path, provider, handle, samples=None, exits=None):
    return Pump(provider, handle, TOKEN, tmp_path,
                on_sample=(samples if samples is not None else []).append,
                on_exit=(exits if exits is not None else []).append)


def ctl(obj):
    import json
    return "\x1epasar:" + TOKEN + " " + json.dumps(obj) + "\n"


def test_job_output_goes_to_the_log(tmp_path):
    p, samples, exits = FakeProvider(), [], []
    h = p.launch(launch_request(tmp_path)); p.start(h)
    pu = pump(tmp_path, p, h, samples, exits)
    p.emit(h, "epoch 1\n" + ctl({"t": "event", "e": {"event": "progress", "step": 3}}) + "epoch 2\n")
    pu.poll()
    assert (tmp_path / "output.log").read_text() == "epoch 1\nepoch 2\n"
    assert '"event": "progress"' in (tmp_path / "events.jsonl").read_text().replace('"event":"progress"', '"event": "progress"')


def test_partial_line_is_held_until_complete(tmp_path):
    p, samples, exits = FakeProvider(), [], []
    h = p.launch(launch_request(tmp_path)); p.start(h)
    pu = pump(tmp_path, p, h, samples, exits)
    p.emit(h, "half a li")
    pu.poll()
    assert not (tmp_path / "output.log").exists() or (tmp_path / "output.log").read_text() == ""
    p.emit(h, "ne\n")
    pu.poll()
    assert (tmp_path / "output.log").read_text() == "half a line\n"


def test_samples_and_exit_are_reported_not_logged(tmp_path):
    p, samples, exits = FakeProvider(), [], []
    h = p.launch(launch_request(tmp_path)); p.start(h)
    pu = pump(tmp_path, p, h, samples, exits)
    p.emit(h, ctl({"t": "sample", "gpus": [[0, 55, 1024, 81920, 240.5, 61]]}))
    p.emit(h, ctl({"t": "exit", "code": 0, "signal": None, "reason": None}))
    pu.poll()
    assert samples == [[[0, 55, 1024, 81920, 240.5, 61]]]
    assert exits == [{"t": "exit", "code": 0, "signal": None, "reason": None}]
    assert (tmp_path / "output.log").read_text() == ""


def test_resumes_from_its_cursor_without_duplicating(tmp_path):
    p, samples, exits = FakeProvider(), [], []
    h = p.launch(launch_request(tmp_path)); p.start(h)
    pu = pump(tmp_path, p, h, samples, exits)
    p.emit(h, "one\n")
    pu.poll()
    resumed = pump(tmp_path, p, h, samples, exits)
    resumed.cursor = pu.cursor
    p.emit(h, "two\n")
    resumed.poll()
    assert (tmp_path / "output.log").read_text() == "one\ntwo\n"


def test_unrecognized_control_kind_degrades_to_log_instead_of_vanishing(tmp_path):
    p, samples, exits = FakeProvider(), [], []
    h = p.launch(launch_request(tmp_path)); p.start(h)
    pu = pump(tmp_path, p, h, samples, exits)
    p.emit(h, ctl({"t": "wat", "foo": 1}))
    pu.poll()
    assert samples == [] and exits == [] and pu.exit_info is None
    assert (tmp_path / "output.log").read_text() == ctl({"t": "wat", "foo": 1})


def test_garbled_exit_line_does_not_look_like_a_clean_exit(tmp_path):
    p, samples, exits = FakeProvider(), [], []
    h = p.launch(launch_request(tmp_path)); p.start(h)
    pu = pump(tmp_path, p, h, samples, exits)
    p.emit(h, "\x1epasar:" + TOKEN + ' {"t": "exit", "code": 0, broken\n')
    pu.poll()
    assert exits == [] and pu.exit_info is None
    assert (tmp_path / "output.log").read_text() == \
        "\x1epasar:" + TOKEN + ' {"t": "exit", "code": 0, broken\n'


def test_restart_mid_line_reconstructs_full_line(tmp_path):
    """A crash between polls must not lose the prefix of a line that hadn't been completed
    yet: cursor only advances past complete lines, so a fresh Pump re-reads the prefix."""
    p, samples, exits = FakeProvider(), [], []
    h = p.launch(launch_request(tmp_path)); p.start(h)
    pu = pump(tmp_path, p, h, samples, exits)
    p.emit(h, "half a li")
    pu.poll()
    resumed = pump(tmp_path, p, h, samples, exits)
    resumed.cursor = pu.cursor
    p.emit(h, "ne\n")
    resumed.poll()
    assert (tmp_path / "output.log").read_text() == "half a line\n"


def test_restart_mid_control_line_exit_arrives_intact(tmp_path):
    p, samples, exits = FakeProvider(), [], []
    h = p.launch(launch_request(tmp_path)); p.start(h)
    pu = pump(tmp_path, p, h, samples, exits)
    full = ctl({"t": "exit", "code": 0, "signal": None, "reason": None})
    p.emit(h, full[:len(full) // 2])
    pu.poll()
    assert exits == [] and pu.exit_info is None
    resumed = pump(tmp_path, p, h, samples, exits)
    resumed.cursor = pu.cursor
    p.emit(h, full[len(full) // 2:])
    resumed.poll()
    assert exits == [{"t": "exit", "code": 0, "signal": None, "reason": None}]
    assert resumed.exit_info == {"t": "exit", "code": 0, "signal": None, "reason": None}
    assert (tmp_path / "output.log").read_text() == ""


def test_multibyte_character_split_across_polls_arrives_intact(tmp_path):
    p, samples, exits = FakeProvider(), [], []
    h = p.launch(launch_request(tmp_path)); p.start(h)
    pu = pump(tmp_path, p, h, samples, exits)
    line = "café\n".encode()  # b"caf\xc3\xa9\n"; splits the two-byte e-acute in half
    p.boxes[h].out += line[:4]
    pu.poll()
    assert not (tmp_path / "output.log").exists() or (tmp_path / "output.log").read_text() == ""
    p.boxes[h].out += line[4:]
    pu.poll()
    assert (tmp_path / "output.log").read_text() == "café\n"


def test_invalid_utf8_bytes_still_degrade_without_raising(tmp_path):
    p, samples, exits = FakeProvider(), [], []
    h = p.launch(launch_request(tmp_path)); p.start(h)
    pu = pump(tmp_path, p, h, samples, exits)
    p.boxes[h].out += b"bad \xff\xfe bytes\n"
    pu.poll()
    assert (tmp_path / "output.log").read_text() == "bad �� bytes\n"
