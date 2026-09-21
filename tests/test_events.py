from pasar.events import parse_line, read_new, restart_cost, wasted_work


def test_parse_line():
    e = parse_line('{"event": "progress", "step": 5, "total_steps": 10}')
    assert e.kind == "progress" and e.step == 5 and e.payload == {"step": 5, "total_steps": 10}
    assert parse_line('{"event": "note", "text": "hi"}').step is None
    assert parse_line('{"event": "checkpoint", "step": "x"}').step is None
    assert parse_line("not json") is None
    assert parse_line('{"event": "explode"}') is None
    assert parse_line("[1, 2]") is None


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
