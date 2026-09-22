import json
import signal
import subprocess
import sys
import textwrap

import pasar_job
import pytest


@pytest.fixture
def events_file(monkeypatch, tmp_path):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("PASAR_EVENTS", str(path))
    return path


def read_events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_noop_outside_pasar(monkeypatch, tmp_path):
    monkeypatch.delenv("PASAR_EVENTS", raising=False)
    monkeypatch.delenv("PASAR_JOB_ID", raising=False)
    pasar_job.checkpoint(3)
    assert pasar_job.job_id() is None and pasar_job.attempt() == 1 and not pasar_job.resuming()


def test_events_are_json_lines(monkeypatch, tmp_path):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("PASAR_EVENTS", str(path))
    monkeypatch.setenv("PASAR_RESUMING", "1")
    pasar_job.checkpoint(10)
    pasar_job.resumed(10)
    pasar_job.progress(12, 100)
    pasar_job.note("hello")
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert lines == [
        {"event": "checkpoint", "step": 10},
        {"event": "resumed", "step": 10},
        {"event": "progress", "step": 12, "total_steps": 100},
        {"event": "note", "text": "hello"},
    ]
    assert pasar_job.resuming()


def test_on_preempt_runs_hook_then_exits(tmp_path):
    marker = tmp_path / "saved"
    script = textwrap.dedent(f"""
        import time, pasar_job
        pasar_job.on_preempt(lambda: open({str(marker)!r}, "w").write("ok"))
        print("ready", flush=True)
        time.sleep(30)
    """)
    p = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "ready"
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=10) == 143
    assert marker.read_text() == "ok"


def test_memory_limit_bytes(monkeypatch):
    monkeypatch.setenv("PASAR_MEM_LIMIT_BYTES", "1024")
    assert pasar_job.memory_limit_bytes() == 1024
    monkeypatch.delenv("PASAR_MEM_LIMIT_BYTES")
    assert pasar_job.apply_memory_limit() is None


def test_progress_metrics(events_file):
    pasar_job.progress(10, 100, loss=1.84, lr=3e-5)
    rec = read_events(events_file)[-1]
    assert rec == {"event": "progress", "step": 10, "total_steps": 100, "loss": 1.84, "lr": 3e-5}


@pytest.mark.parametrize("bad", [{"event": 1.0}, {"loss": float("nan")}, {"loss": "high"},
                                 {"loss": float("inf")}, {"lr": True}])
def test_progress_rejects_bad_metrics(events_file, bad):
    with pytest.raises(ValueError):
        pasar_job.progress(1, 10, **bad)


@pytest.mark.parametrize("total", [None, 0, -5, 2.5, True, "100"])
def test_progress_requires_positive_total_steps(events_file, total):
    with pytest.raises(ValueError):
        pasar_job.progress(1, total)


def test_progress_total_steps_is_required(events_file):
    with pytest.raises(TypeError):
        pasar_job.progress(1)  # type: ignore[call-arg]
