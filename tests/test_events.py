from pasar.events import parse_line, read_new, restart_cost, wasted_work


def test_parse_line():
    e = parse_line('{"event": "progress", "step": 5, "total_steps": 10}')
    assert e.kind == "progress" and e.step == 5 and e.payload == {"step": 5, "total_steps": 10}
    assert parse_line('{"event": "note", "text": "hi"}').step is None
    assert parse_line('{"event": "checkpoint", "step": "x"}').step is None
    assert parse_line("not json") is None
    assert parse_line('{"event": "explode"}') is None
    assert parse_line("[1, 2]") is None


def test_parse_line_drops_step_outside_signed_64_bit_range():
    # A step this large would blow up sqlite's INTEGER column (OverflowError) every tick;
    # treat it like any other bad step and drop it instead of the whole event.
    too_big = f'{{"event": "checkpoint", "step": {2**63}}}'
    e = parse_line(too_big)
    assert e.kind == "checkpoint" and e.step is None
    too_small = f'{{"event": "checkpoint", "step": {-(2**63) - 1}}}'
    assert parse_line(too_small).step is None
    in_range = f'{{"event": "checkpoint", "step": {2**63 - 1}}}'
    assert parse_line(in_range).step == 2**63 - 1


def test_parse_line_rejects_non_finite_json_numbers():
    # json.loads accepts NaN/Infinity/-Infinity and oversized float literals by default; a job
    # emitting one of these must not make it through as a usable event.
    assert parse_line('{"event": "progress", "step": 1, "loss": NaN}') is None
    assert parse_line('{"event": "progress", "step": 1, "loss": Infinity}') is None
    assert parse_line('{"event": "progress", "step": 1, "loss": -Infinity}') is None
    assert parse_line('{"event": "progress", "step": 1, "loss": 1e999}') is None
    assert parse_line('{"event": "progress", "step": 1, "loss": 0.5}').payload["loss"] == 0.5


def test_read_new_logs_malformed_lines_as_warnings(tmp_path, caplog):
    p = tmp_path / "events.jsonl"
    p.write_text('{"event":"checkpoint","step":1}\ngarbage\n{"event":"explode"}\n')
    with caplog.at_level("WARNING"):
        evs, off = read_new(p, 0, job_id=7)
    assert [e.kind for e in evs] == ["checkpoint"]
    assert off == p.stat().st_size
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2
    assert all("7" in r.getMessage() for r in warnings)
    # re-reading from the new offset must not re-log the same lines
    caplog.clear()
    with caplog.at_level("WARNING"):
        read_new(p, off, job_id=7)
    assert not caplog.records


def test_read_new_keeps_partial_lines_for_later(tmp_path):
    p = tmp_path / "events.jsonl"
    assert read_new(p, 0) == ([], 0)
    p.write_text('{"event":"checkpoint","step":1}\ngarbage\n{"event":"resu')
    evs, off = read_new(p, 0)
    assert [e.kind for e in evs] == ["checkpoint"]
    with p.open("a") as f:
        f.write('med","step":1}\n')
    evs, off2 = read_new(p, off)
    assert [e.kind for e in evs] == ["resumed"] and off2 == p.stat().st_size


def test_wasted_work():
    assert wasted_work(150, 100, 130, True) == 20
    assert wasted_work(150, 100, None, True) == 50       # no checkpoint this attempt
    assert wasted_work(150, 100, 90, True) == 50         # checkpoint from an older attempt
    assert wasted_work(150, 100, None, False) is None    # job never reports events


def test_restart_cost():
    assert restart_cost(100, 125) == 25
    assert restart_cost(100, None) is None
