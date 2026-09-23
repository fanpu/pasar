import json
import signal
import subprocess
import sys
import threading
import time

from pasar_job import run as wrapper

TOKEN = "tok123"


def run_wrapper(tmp_path, command, limit=60, grace=5):
    return subprocess.run(
        [sys.executable, "-m", "pasar_job.run", "--token", TOKEN,
         "--events", str(tmp_path / "events"), "--limit", str(limit),
         "--grace", str(grace), "--", command],
        capture_output=True, text=True, timeout=60, cwd=tmp_path, check=False)


def control(out):
    # str.splitlines() treats \x1e (record separator) itself as a line break and drops it,
    # which would silently hide every control line; split on the literal "\n" instead.
    return [json.loads(l.split(" ", 1)[1]) for l in out.split("\n")
            if l.startswith(f"\x1epasar:{TOKEN} ")]


def plain(out):
    return [l for l in out.split("\n") if not l.startswith("\x1epasar:")]


def test_passes_job_output_through_and_reports_exit(tmp_path):
    p = run_wrapper(tmp_path, "echo hello; exit 3")
    assert "hello" in plain(p.stdout)
    exits = [c for c in control(p.stdout) if c["t"] == "exit"]
    assert exits == [{"t": "exit", "code": 3, "signal": None}]
    assert p.returncode == 3


def test_forwards_events_written_to_the_events_path(tmp_path):
    p = run_wrapper(tmp_path, 'echo \'{"event":"progress","step":5}\' >> "$PASAR_EVENTS"; sleep 1')
    events = [c for c in control(p.stdout) if c["t"] == "event"]
    assert {"event": "progress", "step": 5} in [e["e"] for e in events]


def test_stops_at_its_limit_and_says_so(tmp_path):
    p = run_wrapper(tmp_path, 'trap "echo SAVED; exit 143" TERM; sleep 30', limit=2, grace=5)
    assert "SAVED" in plain(p.stdout)
    assert any(c["t"] == "exit" and c["reason"] == "time_limit" for c in control(p.stdout))


def test_kills_after_the_grace_period(tmp_path):
    p = run_wrapper(tmp_path, 'trap "" TERM; sleep 30', limit=1, grace=2)
    exits = [c for c in control(p.stdout) if c["t"] == "exit"]
    assert exits and exits[0]["signal"] == "SIGKILL"


def test_forwards_sigterm_to_the_job_and_reports_stopped(tmp_path):
    """A stop from the provider arrives to the wrapper (PID 1) as SIGTERM, mirroring Modal:
    it must forward to the job, wait for --grace, then report reason "stopped"."""
    p = subprocess.Popen(
        [sys.executable, "-m", "pasar_job.run", "--token", TOKEN,
         "--events", str(tmp_path / "events"), "--limit", "60", "--grace", "5",
         "--", 'trap "echo SAVED; exit 143" TERM; sleep 30'],
        cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(1)
    p.send_signal(signal.SIGTERM)
    out = p.communicate(timeout=60)[0]
    assert "SAVED" in plain(out)
    exits = [c for c in control(out) if c["t"] == "exit"]
    assert exits and exits[0]["reason"] == "stopped"


def test_sigkills_after_grace_when_the_job_ignores_its_own_sigterm(tmp_path):
    p = subprocess.Popen(
        [sys.executable, "-m", "pasar_job.run", "--token", TOKEN,
         "--events", str(tmp_path / "events"), "--limit", "60", "--grace", "2",
         "--", 'trap "" TERM; sleep 30'],
        cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(1)
    p.send_signal(signal.SIGTERM)
    out = p.communicate(timeout=60)[0]
    exits = [c for c in control(out) if c["t"] == "exit"]
    assert exits and exits[0]["signal"] == "SIGKILL" and exits[0]["reason"] == "stopped"


def test_sampler_degrades_silently_without_nvidia_smi(monkeypatch, tmp_path):
    """The wrapper must not crash when a provider's container has no nvidia-smi on PATH."""
    monkeypatch.setattr(wrapper, "SAMPLE_INTERVAL", 0.05)
    monkeypatch.setenv("PATH", str(tmp_path))
    samples = []
    monkeypatch.setattr(wrapper, "emit", lambda token, obj: samples.append(obj))
    stop = threading.Event()
    t = threading.Thread(target=wrapper._sample_gpus, args=("tok", stop), daemon=True)
    t.start()
    t.join(timeout=2)
    assert not t.is_alive()
    assert samples == []
