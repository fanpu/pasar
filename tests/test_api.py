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


def test_follow_logs_ends_promptly_on_shutdown(daemon, executor, make_spec):
    # Even for a job that's still running (not in TERMINAL), a set `shutdown` event must end
    # the stream immediately, the same way pasard's SIGTERM handling needs it to for a clean
    # exit: an open logs-follow connection must not keep the process alive.
    daemon.submit(make_spec())
    daemon.tick()
    path = daemon.job_dir(1) / "output.log"
    shutdown = asyncio.Event()
    shutdown.set()
    chunks = _drain(api._follow_logs(daemon, 1, path, 0, shutdown=shutdown), 1)
    assert chunks == ["event: end\ndata: {}\n\n"]


def test_follow_logs_stops_once_shutdown_is_set_mid_stream(daemon, executor, make_spec):
    daemon.submit(make_spec())
    daemon.tick()
    path = daemon.job_dir(1) / "output.log"
    shutdown = asyncio.Event()

    async def run():
        gen = api._follow_logs(daemon, 1, path, 0, shutdown=shutdown)
        try:
            await gen.__anext__()  # the "attempt 1" separator daemon.tick() already wrote
            # Nothing new has been written since and the job isn't finished, so the generator
            # is parked in its poll wait; setting `shutdown` there must still wake it promptly.
            task = asyncio.ensure_future(gen.__anext__())
            await asyncio.sleep(0.05)
            shutdown.set()
            chunk = await asyncio.wait_for(task, timeout=1)
            assert chunk == "event: end\ndata: {}\n\n"
            with pytest.raises(StopAsyncIteration):
                await gen.__anext__()
        finally:
            await gen.aclose()

    asyncio.run(run())


def test_stream_updates_stops_on_shutdown(daemon):
    shutdown = asyncio.Event()
    shutdown.set()

    async def run():
        gen = api._stream_updates(daemon, lambda: {"status": {}, "jobs": []}, None,
                                   shutdown=shutdown)
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    asyncio.run(run())


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


def test_patch_preempt_alone_keeps_bid(client, daemon, tmp_path):
    submit(client, tmp_path, bid=1200)
    r = client.patch("/api/jobs/1", json={"preempt": True})
    assert r.status_code == 200 and r.json()["preempt"] is True and r.json()["bid"] == 1200
    r = client.patch("/api/jobs/1", json={"bid": 1300})
    assert r.json()["preempt"] is True and r.json()["bid"] == 1300


def test_sparks_returns_one_series_per_job(client, tmp_path, daemon):
    submit(client, tmp_path)
    submit(client, tmp_path)
    submit(client, tmp_path)
    daemon.store.add_event(1, 1, 5.0, "progress", 1, {"step": 1, "loss": 0.9})
    daemon.store.add_event(1, 1, 6.0, "progress", 2, {"step": 2, "loss": 0.4})
    daemon.store.add_event(2, 1, 5.0, "progress", 1, {"step": 1})  # nothing plottable

    body = client.get("/api/sparks?ids=1,2,3").json()
    assert set(body) == {"1"}
    assert body["1"] == {"key": "loss", "points": [[5.0, 0.9], [6.0, 0.4]], "latest": 0.4}
    assert client.get("/api/sparks?ids=").json() == {}


def test_sparks_rejects_junk_and_oversized_id_lists(client):
    assert client.get("/api/sparks?ids=1,nope").status_code == 422
    too_many = ",".join(str(i) for i in range(api.MAX_SPARK_IDS + 1))
    assert client.get(f"/api/sparks?ids={too_many}").status_code == 422


def submit_cloud(client, cloud_cwd, **kw):
    body = {"command": "python -c 'pass'", "time": "1h", "cwd": cloud_cwd,
           "target": "fake", "gpu": "H100", **kw}
    return client.post("/api/jobs", json=body)


def test_submit_cloud_job_returns_awaiting_with_costs(client, cloud_daemon, cloud_cwd):
    r = submit_cloud(client, cloud_cwd)
    assert r.status_code == 201
    body = r.json()
    assert body["state"] == "awaiting"
    assert body["cloud"]["target"] == "fake" and body["cloud"]["gpu"] == "H100"
    assert body["cloud"]["estimated_cost"] > 0
    assert body["cloud"]["max_cost"] >= body["cloud"]["estimated_cost"]
    assert body["cloud"]["console_url"] is None  # nothing has launched yet


def test_local_job_has_no_cloud_object(client, tmp_path):
    r = submit(client, tmp_path)
    assert r.json()["cloud"] is None


def test_approve_then_reject_endpoints(client, cloud_daemon, cloud_cwd):
    job_id = submit_cloud(client, cloud_cwd).json()["id"]
    approved = client.post(f"/api/jobs/{job_id}/approve")
    assert approved.status_code == 200 and approved.json()["state"] == "queued"
    assert cloud_daemon.store.approvals(job_id)[0]["attempt"] == 1

    other_id = submit_cloud(client, cloud_cwd).json()["id"]
    rejected = client.post(f"/api/jobs/{other_id}/reject")
    assert rejected.status_code == 200
    assert rejected.json()["state"] == "cancelled" and rejected.json()["reason"] == "rejected"

    # approving something that isn't awaiting any more is a 409, not a silent no-op
    r = client.post(f"/api/jobs/{other_id}/approve")
    assert r.status_code == 409


def test_submit_rejects_an_unconfigured_target(client, daemon, cloud_cwd):
    # daemon (not cloud_daemon) has no cloud target at all: submit fails outright, before
    # anything exists to approve.
    r = submit_cloud(client, cloud_cwd)
    assert r.status_code == 422 and "unknown cloud target" in r.json()["detail"]


def test_approve_of_a_target_missing_its_provider_reads_differently_from_unconfigured(
        client, cloud_daemon, cloud_cwd):
    # A target can be configured (cfg.clouds has it) but wired to no provider on this pasard
    # (e.g. the operator didn't set the API key here); approving it must say so, not claim the
    # target itself is unknown.
    job_id = submit_cloud(client, cloud_cwd).json()["id"]
    del cloud_daemon.executors["fake"]
    r = client.post(f"/api/jobs/{job_id}/approve")
    assert r.status_code == 409
    assert "no provider" in r.json()["detail"]


def test_restart_of_a_cloud_job_is_refused_with_the_submit_command(client, cloud_daemon, cloud_cwd):
    job_id = submit_cloud(client, cloud_cwd).json()["id"]
    client.post(f"/api/jobs/{job_id}/reject")  # a terminal cloud job, restart is otherwise plausible
    r = client.post(f"/api/jobs/{job_id}/restart", json={})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "pasar submit --on fake --gpu H100" in detail


def test_cloud_endpoint_reports_budget_and_spend(client, cloud_daemon, cloud_cwd):
    body = client.get("/api/cloud").json()
    target = next(t for t in body["targets"] if t["name"] == "fake")
    assert target["daily_budget"] == 50.0 and target["monthly_budget"] == 300.0
    assert target["configured"] is True
    assert target["rates"]["gpu_hour_cost_h100"] == 3.95
    assert target["spent_today"] == 0 and target["committed"] == 0
    assert body["awaiting"] == []

    submit_cloud(client, cloud_cwd)
    body2 = client.get("/api/cloud").json()
    assert len(body2["awaiting"]) == 1
    assert body2["awaiting"][0]["cloud"]["target"] == "fake"


def test_submit_without_gpu_is_a_400(client, cloud_daemon, cloud_cwd):
    r = client.post("/api/jobs", json={"command": "python -c 'pass'", "time": "1h",
                                       "cwd": cloud_cwd, "target": "fake"})
    assert r.status_code == 422 and "gpu" in r.json()["detail"]


def test_submit_rejects_mem_and_retries_for_a_cloud_job(client, cloud_daemon, cloud_cwd):
    assert submit_cloud(client, cloud_cwd, mem="24G").status_code == 422
    assert submit_cloud(client, cloud_cwd, retries=2).status_code == 422


def test_submit_rejects_data_with_a_clear_message(client, cloud_daemon, cloud_cwd):
    r = submit_cloud(client, cloud_cwd, data=["dataset/"])
    assert r.status_code == 422
    assert "provider work" in r.json()["detail"]


def test_price_rise_leaves_the_old_ceiling_visible_while_awaiting(client, cloud_daemon,
                                                                   cloud_provider, cloud_cwd,
                                                                   monkeypatch, clock):
    job_id = submit_cloud(client, cloud_cwd).json()["id"]
    client.post(f"/api/jobs/{job_id}/approve")
    ceiling = cloud_daemon.store.approvals(job_id)[0]["max_cost"]
    monkeypatch.setattr(cloud_provider, "rates", lambda: {
        "gpu_hour_cost_h100": 7.90, "cpu_hour_cost_sandbox": 0.14,
        "mem_gib_hour_cost_sandbox": 0.024,
    })
    clock.advance(60)
    cloud_daemon.tick()
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["state"] == "awaiting" and job["reason"] == "price_rose"
    assert f"{ceiling:.2f}" in job["summary"]
    # re-approving now prices at the new, higher rate
    assert job["cloud"]["max_cost"] > ceiling


def test_submit_refused_when_estimate_exceeds_max_cost(client, cloud_daemon, cloud_cwd):
    # The estimate at the default 1h --time and the fake target's rates is $5.28; a --max-cost
    # below that is a mistake worth catching before anything is even priced for approval.
    r = submit_cloud(client, cloud_cwd, max_cost=2.00)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "$5.28" in detail and "$2.00" in detail
    assert cloud_daemon.store.list_jobs() == []  # nothing was created


def test_lower_max_cost_becomes_the_enforced_ceiling(client, cloud_daemon, cloud_cwd):
    # The target's own automatic ceiling here is ~$7.92; a --max-cost of $6, above the $5.28
    # estimate but below that, must be what actually gets recorded and shown, not the bigger
    # automatic figure.
    job_id = submit_cloud(client, cloud_cwd, max_cost=6.00).json()["id"]
    approved = client.post(f"/api/jobs/{job_id}/approve").json()
    assert approved["cloud"]["max_cost"] == pytest.approx(6.00)
    assert approved["cloud"]["user_capped"] is True
    row = cloud_daemon.store.approvals(job_id)[0]
    assert row["max_cost"] == pytest.approx(6.00)


def test_a_capped_job_shows_the_shorter_run_its_cap_buys(client, cloud_daemon, cloud_cwd):
    # Capping dollars costs time — the daemon pauses the attempt when the cap is spent — so the
    # view has to say how much time, not just repeat the dollar figure back.
    capped = submit_cloud(client, cloud_cwd, max_cost=6.00).json()["cloud"]
    assert capped["full_seconds"] == 5400        # a 1h estimate x the target's 1.5 timeout factor
    assert capped["approved_seconds"] == 4092    # what $6 buys at $5.278/h
    assert capped["user_capped"] is True
    plain = submit_cloud(client, cloud_cwd).json()["cloud"]
    assert plain["approved_seconds"] == plain["full_seconds"] == 5400


def test_price_rise_above_the_users_cap_bounces_to_awaiting(client, cloud_daemon, cloud_provider,
                                                             cloud_cwd, monkeypatch, clock):
    # --max-cost of $9 does not bind at approval (the automatic ceiling, ~$7.92, is lower), so
    # the approved ceiling here is that automatic figure, same as without a --max-cost at all.
    job_id = submit_cloud(client, cloud_cwd, max_cost=9.00).json()["id"]
    client.post(f"/api/jobs/{job_id}/approve")
    ceiling = cloud_daemon.store.approvals(job_id)[0]["max_cost"]
    assert ceiling == pytest.approx(7.92, abs=0.01)
    # Rates rise enough that the live, uncapped ceiling would be ~$10.99 — above the $9 cap,
    # which is therefore what the relaunch is priced at. That capped $9 is still above what was
    # actually approved (~$7.92), so the job must bounce back rather than launch at it unseen.
    monkeypatch.setattr(cloud_provider, "rates", lambda: {
        "gpu_hour_cost_h100": 6.00, "cpu_hour_cost_sandbox": 0.14,
        "mem_gib_hour_cost_sandbox": 0.024,
    })
    clock.advance(60)
    cloud_daemon.tick()
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["state"] == "awaiting" and job["reason"] == "price_rose"
    assert job["cloud"]["max_cost"] == pytest.approx(9.00)  # live re-price, held to --max-cost
    assert job["cloud"]["user_capped"] is True
