import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from pasar import api
from pasar.api import create_app
from pasar.units import GiB


@pytest.fixture
def client(daemon):
    return TestClient(create_app(daemon, allowed_hosts=["testserver"]))


def submit(client, tmp_path, **kw):
    body = {"command": "python train.py", "time": "1h", "cwd": str(tmp_path), **kw}
    return client.post("/api/jobs", json=body)


def test_submit_and_list(client, tmp_path):
    r = submit(client, tmp_path, mem="24G", bid=1200, tags=["a"], submitter="agent-1")
    assert r.status_code == 201
    job = r.json()
    assert job["id"] == 1 and job["mem_request"] == 24 * GiB and job["bid"] == 1200
    assert job["est_runtime"] == 3600 and job["grace"] == 120 and job["submitter"] == "agent-1"
    jobs = client.get("/api/jobs").json()
    assert [j["id"] for j in jobs] == [1]


def test_validation_and_errors(client, tmp_path):
    assert submit(client, tmp_path, time="soon").status_code == 422
    assert client.get("/api/jobs/9").status_code == 404
    submit(client, tmp_path)
    assert client.post("/api/jobs/1/cancel").json()["state"] == "cancelled"
    r = client.post("/api/jobs/1/cancel")
    assert r.status_code == 409 and "already" in r.json()["detail"]


def test_bid_and_restart(client, daemon, tmp_path):
    submit(client, tmp_path)
    assert client.patch("/api/jobs/1", json={"bid": 1500}).json()["bid"] == 1500
    client.post("/api/jobs/1/cancel")
    r = client.post("/api/jobs/1/restart", json={"mem": "8G", "time": "30m"})
    assert r.json()["state"] == "queued" and r.json()["mem_request"] == 8 * GiB
    r = client.post("/api/jobs/1/cancel")
    r = client.post("/api/jobs/1/restart", json={"whole_gpu": True})
    assert r.json()["mode"] == "whole"


def test_job_detail_logs_and_events(client, daemon, executor, tmp_path):
    submit(client, tmp_path)
    daemon.tick()
    log = daemon.job_dir(1) / "output.log"
    with log.open("a") as f:
        f.write("hello\n")
    detail = client.get("/api/jobs/1").json()
    assert detail["attempts"][0]["unit"] == "pasar-job-1-1"
    r = client.get("/api/jobs/1/logs").json()
    assert r["text"].endswith("hello\n") and r["offset"] == log.stat().st_size
    assert client.get(f"/api/jobs/1/logs?offset={r['offset']}").json()["text"] == ""
    (daemon.job_dir(1) / "events.jsonl").write_text('{"event":"checkpoint","step":3}\n')
    daemon.tick()
    assert client.get("/api/jobs/1/events").json()[0]["kind"] == "checkpoint"


def test_nan_in_event_payload_does_not_break_get_jobs_for_everyone(client, daemon, tmp_path):
    # NaN/Infinity in event JSON used to reach job_view and make Starlette's strict JSON
    # encoder raise (caught by the app's blanket ValueError handler as a 422) for the whole
    # /api/jobs response, not just the offending job.
    submit(client, tmp_path)
    daemon.tick()
    (daemon.job_dir(1) / "events.jsonl").write_text(
        '{"event":"progress","step":1,"loss":NaN}\n'
        '{"event":"progress","step":2,"total_steps":10}\n'
    )
    daemon.tick()
    r = client.get("/api/jobs")
    assert r.status_code == 200
    assert r.json()[0]["progress"]["step"] == 2


def _drain(gen, n):
    """Pull up to `n` items from an async generator, then close it. Bounded so a generator
    that never finishes on its own (like the SSE ones below) can't hang a test."""
    async def run():
        items = []
        try:
            for _ in range(n):
                items.append(await gen.__anext__())
        finally:
            await gen.aclose()
        return items
    return asyncio.run(run())


def test_follow_logs_sends_keepalive_when_quiet(daemon, executor, make_spec, monkeypatch):
    monkeypatch.setattr(api, "KEEPALIVE_INTERVAL", 0.05)
    daemon.submit(make_spec())
    daemon.tick()  # writes the "attempt 1" separator into output.log
    path = daemon.job_dir(1) / "output.log"
    chunks = _drain(api._follow_logs(daemon, 1, path, 0), 4)
    assert any(c.startswith(":") for c in chunks)


def test_stream_sends_keepalive_when_quiet(daemon, monkeypatch):
    monkeypatch.setattr(api, "KEEPALIVE_INTERVAL", 0.05)
    chunks = _drain(api._stream_updates(daemon, lambda: {"status": {}, "jobs": []}, None), 4)
    assert any(c.startswith(":") for c in chunks)


def test_follow_logs_ends_when_job_finishes(client, daemon, executor, tmp_path):
    submit(client, tmp_path)
    daemon.tick()
    executor.exit("pasar-job-1-1", code=0)
    daemon.tick()
    with client.stream("GET", "/api/jobs/1/logs?follow=true") as r:
        body = "".join(r.iter_text())
    assert "attempt 1" in body and "event: end" in body


def test_status_gpu_and_stream(client, daemon, tmp_path):
    submit(client, tmp_path)
    daemon.tick()
    assert client.get("/api/status").json()["pool"] == 105 * GiB
    assert client.get("/api/gpu").json() == {"power_w": [], "temp_c": [], "util_pct": []}
    with client.stream("GET", "/api/stream?limit=1") as r:
        line = next(ln for ln in r.iter_lines() if ln.startswith("data: "))
    msg = json.loads(line[len("data: "):])
    assert msg["status"]["pool"] == 105 * GiB and msg["jobs"][0]["id"] == 1


def test_disallowed_host_header_is_rejected(daemon):
    # DNS rebinding: a request whose Host header doesn't match an allowed name must be
    # rejected before it reaches any handler.
    app = create_app(daemon, allowed_hosts=["testserver"])
    client = TestClient(app)
    r = client.get("/api/status", headers={"Host": "evil.example.com"})
    assert r.status_code == 400


def test_default_hosts_are_allowed_without_config(daemon):
    app = create_app(daemon, allowed_hosts=["testserver"])
    client = TestClient(app)
    assert client.get("/api/status", headers={"Host": "127.0.0.1"}).status_code == 200
    assert client.get("/api/status", headers={"Host": "localhost"}).status_code == 200


def test_configured_allowed_hosts_extra_name_is_allowed(daemon):
    daemon.cfg.allowed_hosts = ["mybox.example.ts.net"]
    app = create_app(daemon, allowed_hosts=["testserver"])
    client = TestClient(app)
    r = client.get("/api/status", headers={"Host": "mybox.example.ts.net"})
    assert r.status_code == 200


def test_submit_rejects_out_of_range_bid(client, tmp_path):
    assert submit(client, tmp_path, bid=2**70).status_code == 422


def test_patch_rejects_out_of_range_bid(client, daemon, tmp_path):
    submit(client, tmp_path)
    assert client.patch("/api/jobs/1", json={"bid": -1}).status_code == 422
    assert client.patch("/api/jobs/1", json={"bid": 2**70}).status_code == 422


def test_restart_rejects_out_of_range_bid(client, daemon, tmp_path):
    submit(client, tmp_path)
    client.post("/api/jobs/1/cancel")
    r = client.post("/api/jobs/1/restart", json={"bid": 2**70})
    assert r.status_code == 422


def test_negative_logs_offset_is_rejected(client, daemon, tmp_path):
    submit(client, tmp_path)
    daemon.tick()
    r = client.get("/api/jobs/1/logs?offset=-1")
    assert r.status_code == 422


def test_env_is_stored_privately(client, daemon, tmp_path):
    r = submit(client, tmp_path, env={"HF_TOKEN": "secret"})
    assert r.status_code == 201
    job = r.json()
    assert "env" not in job
    assert "HF_TOKEN" not in json.dumps(job)
    d = daemon.job_dir(job["id"])
    assert json.loads((d / "env.json").read_text()) == {"HF_TOKEN": "secret"}
    assert "HF_TOKEN" not in (d / "spec.json").read_text()
