from fastapi.testclient import TestClient

from pasar.api import create_app
from pasar.daemon import USAGE_HISTORY
from pasar.models import State
from pasar.units import GiB


def client_for(daemon):
    return TestClient(create_app(daemon, allowed_hosts=["testserver"]))


def submit(client, tmp_path, **kw):
    body = {"command": "python train.py", "time": "1h", "cwd": str(tmp_path), **kw}
    return client.post("/api/jobs", json=body).json()


def test_spans_empty_for_queued_job(daemon, tmp_path):
    c = client_for(daemon)
    job = submit(c, tmp_path, mem="4G")
    assert job["spans"] == []


def test_spans_and_usage_history_for_running_job(daemon, executor, probe, tmp_path):
    c = client_for(daemon)
    job = submit(c, tmp_path, mem="4G")
    daemon.tick()
    cg = executor.units["pasar-job-1-1"].control_group
    probe.cg_mem[cg] = 3 * GiB
    probe.cg_pids[cg] = []
    daemon.clock.advance(2)
    daemon.tick()
    daemon.clock.advance(2)
    daemon.tick()
    view = c.get(f"/api/jobs/{job['id']}").json()
    start, end, kind = view["spans"][0]
    assert end is None and kind is None and start > 0
    hist = c.get(f"/api/jobs/{job['id']}/usage").json()
    assert len(hist) >= 2 and hist[-1][1] == 3 * GiB
    assert hist[0][0] < hist[-1][0]


def test_usage_unknown_job_404(daemon):
    assert client_for(daemon).get("/api/jobs/99/usage").status_code == 404


def test_usage_history_is_bounded_and_resets_per_attempt(daemon, executor, probe, make_spec):
    daemon.submit(make_spec(mem_request=4 * GiB))
    daemon.tick()
    cg = executor.units["pasar-job-1-1"].control_group
    probe.cg_mem[cg] = 1 * GiB
    for _ in range(USAGE_HISTORY + 5):
        daemon.clock.advance(2)
        daemon.tick()
    assert len(daemon.usage_history[1]) == USAGE_HISTORY

    executor.exit("pasar-job-1-1", code=1)
    daemon.clock.advance(2)
    daemon.tick()  # fails, requeues, relaunches (retries default 0 -> stays failed unless retried)
    assert daemon.job(1).state == State.FAILED

    # Restart so a second attempt launches and history restarts.
    daemon.restart(1)
    daemon.clock.advance(2)
    daemon.tick()
    assert len(daemon.usage_history[1]) <= 2
