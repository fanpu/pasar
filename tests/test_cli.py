import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from pasar.api import create_app
from pasar.cli import ApiError, base_url, call, main, wait_code
from pasar.cloud.executor import parse_unit
from pasar.cloud.pace import MIN_REPORTS
from pasar.units import GiB


@pytest.fixture
def client(daemon):
    return TestClient(create_app(daemon, allowed_hosts=["testserver"]))


def run(client, capsys, *argv):
    code = main(list(argv), client=client)
    return code, capsys.readouterr()


def test_submit_json_and_ls(client, capsys, daemon, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HF_TOKEN", "secret")
    code, out = run(client, capsys, "submit", "--time", "2h", "--mem", "24G", "--bid", "1200",
                    "--tag", "sft", "--by", "agent-3", "--json", "--", ".venv/bin/python",
                    "train.py", "--lr", "3e-5")
    assert code == 0
    job = json.loads(out.out)
    assert job["command"] == ".venv/bin/python train.py --lr 3e-5"
    assert job["cwd"] == str(tmp_path) and job["submitter"] == "agent-3" and job["tags"] == ["sft"]
    # the submitter's environment is captured and sent along, but never echoed back
    assert "secret" not in out.out
    env = json.loads((daemon.job_dir(job["id"]) / "env.json").read_text())
    assert env["HF_TOKEN"] == "secret"
    code, out = run(client, capsys, "ls")
    assert code == 0 and "train" in out.out and "queued" in out.out


def test_submit_no_env_captures_nothing(client, capsys, daemon, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HF_TOKEN", "secret")
    code, out = run(client, capsys, "submit", "--time", "1h", "--no-env", "--json", "--",
                    "python", "a.py")
    assert code == 0
    job = json.loads(out.out)
    env = json.loads((daemon.job_dir(job["id"]) / "env.json").read_text())
    assert env == {}


def test_submit_requires_time(client, capsys):
    code, _ = run(client, capsys, "submit", "--", "python", "a.py")
    assert code == 64


def test_cancel_bid_show(client, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run(client, capsys, "submit", "--time", "1h", "--", "python a.py")
    code, out = run(client, capsys, "bid", "1", "1500", "--json")
    assert json.loads(out.out)["bid"] == 1500
    code, out = run(client, capsys, "show", "1")
    assert "1500" in out.out
    code, out = run(client, capsys, "cancel", "1")
    assert code == 0 and "cancelled" in out.out
    code, out = run(client, capsys, "cancel", "1")
    assert code == 70 and "already" in out.err
    code, out = run(client, capsys, "show", "42")
    assert code == 70 and "no job 42" in out.err


def test_show_prints_attempt_count_not_raw_list(client, capsys, daemon, tmp_path, monkeypatch):
    # GET /api/jobs/{id} embeds `attempts` as a list of attempt views (unlike the summary
    # views used elsewhere, where it's a count); `show` must print the count, not the list.
    monkeypatch.chdir(tmp_path)
    run(client, capsys, "submit", "--time", "1h", "--", "python", "a.py")
    daemon.tick()
    code, out = run(client, capsys, "show", "1")
    assert code == 0
    assert "attempts  1" in out.out
    assert "{" not in out.out and "[" not in out.out


def test_wait_codes():
    assert wait_code({"state": "completed", "reason": None}) == 0
    assert wait_code({"state": "failed", "reason": "exit"}) == 1
    assert wait_code({"state": "failed", "reason": "gpu_oom"}) == 2
    assert wait_code({"state": "cancelled", "reason": "cancelled"}) == 3


def test_wait_returns_job_outcome(client, capsys, daemon, executor, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run(client, capsys, "submit", "--time", "1h", "--", "python a.py")
    daemon.tick()
    executor.exit("pasar-job-1-1", code=1)
    daemon.tick()
    code, _ = run(client, capsys, "wait", "1")
    assert code == 1
    code, _ = run(client, capsys, "wait", "1", "--timeout", "0")
    assert code == 1


def test_logs_and_status(client, capsys, daemon, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run(client, capsys, "submit", "--time", "1h", "--", "python a.py")
    daemon.tick()
    code, out = run(client, capsys, "logs", "1")
    assert code == 0 and "attempt 1" in out.out
    code, out = run(client, capsys, "status", "--json")
    assert json.loads(out.out)["pool"] > 0


def test_logs_follow_missing_job(client, capsys):
    # the API error (404) must surface through the streaming path the same way `call()`
    # surfaces it elsewhere: exit 70 with the detail message, not a silent exit 0.
    code, out = run(client, capsys, "logs", "42", "-f")
    assert code == 70 and "no job 42" in out.err


def test_logs_follow_unreachable_daemon(capsys):
    # a real (non-ASGI) client pointed at a port nothing listens on; the connection error
    # from client.stream() must map to EX_UNAVAILABLE (69), not an uncaught exception.
    unreachable = httpx.Client(base_url="http://127.0.0.1:1", timeout=0.5)
    code, out = run(unreachable, capsys, "logs", "1", "-f")
    assert code == 69 and "cannot reach pasard" in out.err


def test_logs_follow_uses_a_read_none_timeout(capsys, monkeypatch):
    # The follow stream can be quiet for a long time between server keep-alives; it must not
    # use the client's normal (bounded) read timeout, or `pasar logs -f` dies after ~30s.
    sse = 'data: {"text": "hi"}\n\nevent: end\ndata: {}\n\n'
    captured = {}
    orig_stream = httpx.Client.stream

    def spy(self, method, url, **kw):
        captured.update(kw)
        return orig_stream(self, method, url, **kw)

    monkeypatch.setattr(httpx.Client, "stream", spy)

    def handler(request):
        return httpx.Response(200, text=sse)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    code, out = run(client, capsys, "logs", "1", "-f")
    assert code == 0 and out.out == "hi"
    assert captured["timeout"] == httpx.Timeout(30, read=None)


def test_logs_follow_streams_text(capsys):
    sse = ('data: {"text": "hello "}\n\n'
           'data: {"text": "world"}\n\n'
           'event: end\ndata: {}\n\n')

    def handler(request):
        return httpx.Response(200, text=sse)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    code, out = run(mock_client, capsys, "logs", "1", "-f")
    assert code == 0
    assert out.out == "hello world"


def test_base_url_defaults_to_the_always_bound_default_address(monkeypatch):
    monkeypatch.delenv("PASAR_URL", raising=False)
    assert base_url() == "http://127.0.0.1:8750"


def test_base_url_honors_pasar_url_env(monkeypatch):
    monkeypatch.setenv("PASAR_URL", "http://example.ts.net:9000")
    assert base_url() == "http://example.ts.net:9000"


def test_call_joins_fastapi_validation_error_list():
    # FastAPI's own request-validation errors return {"detail": [ {...}, ... ]}, unlike our
    # handlers' {"detail": "message"}; call() must handle both without crashing.
    def handler(request):
        return httpx.Response(422, json={"detail": [
            {"loc": ["body", "time"], "msg": "field required", "type": "missing"},
            {"loc": ["body", "cwd"], "msg": "field required", "type": "missing"},
        ]})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    with pytest.raises(ApiError) as exc_info:
        call(client, "GET", "/x")
    assert "field required" in str(exc_info.value)


def test_call_passes_through_plain_string_detail():
    def handler(request):
        return httpx.Response(404, json={"detail": "no job 42"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    with pytest.raises(ApiError) as exc_info:
        call(client, "GET", "/x")
    assert str(exc_info.value) == "no job 42"


def test_ls_and_show_mark_progress_based_times(client, capsys, daemon, clock, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run(client, capsys, "submit", "--time", "10m", "--", "python", "a.py")
    daemon.tick()
    clock.advance(720)
    (daemon.job_dir(1) / "events.jsonl").write_text('{"event":"progress","step":1000,"total_steps":5000}\n')
    daemon.tick()
    _, out = run(client, capsys, "ls")
    assert "12m / ~1h00m*" in out.out
    assert "* projected from the job's progress reports" in out.out
    _, out = run(client, capsys, "show", "1")
    assert "12m of ~1h00m from progress (estimated 10m)" in out.out


def test_preempt_flag_on_submit_bid_and_restart(client, capsys, daemon, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run(client, capsys, "submit", "--time", "1h", "--preempt", "--", "python", "a.py")
    assert daemon.job(1).spec.preempt and daemon.job(1).spec.preemptible
    _, out = run(client, capsys, "show", "1")
    assert "1000 (may preempt lower bids)" in out.out
    _, out = run(client, capsys, "bid", "1", "1500")  # not carried over: must be asked for again
    assert not daemon.job(1).spec.preempt and out.out.strip() == "#1 bid is now 1500"
    _, out = run(client, capsys, "bid", "1", "2000", "--preempt")
    assert daemon.job(1).spec.preempt and "may preempt lower bids" in out.out
    run(client, capsys, "cancel", "1")
    run(client, capsys, "restart", "1")
    assert not daemon.job(1).spec.preempt
    run(client, capsys, "cancel", "1")
    run(client, capsys, "restart", "1", "--preempt")
    assert daemon.job(1).spec.preempt


def test_non_preemptible_flag(client, capsys, daemon, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run(client, capsys, "submit", "--time", "1h", "--non-preemptible", "--", "python", "a.py")
    assert not daemon.job(1).spec.preemptible and not daemon.job(1).spec.preempt


def test_cli_has_no_approve_command(client, capsys):
    code, out = run(client, capsys, "--help")
    assert "approve" not in out.out
    code, out = run(client, capsys, "submit", "--help")
    assert code == 0 and "approve" not in out.out


def test_submit_on_cloud_prints_cost_and_where_to_approve(client, capsys, cloud_daemon,
                                                           cloud_cwd, monkeypatch):
    monkeypatch.chdir(cloud_cwd)
    code, out = run(client, capsys, "submit", "--time", "1h", "--on", "fake", "--gpu", "H100",
                    "--", "python", "-c", "pass")
    assert code == 0
    assert "awaiting" in out.out
    assert "$" in out.out
    assert "/api/jobs/1/approve" in out.out  # the endpoint, since there is no UI to point at
    assert cloud_daemon.job(1).state.value == "awaiting"


def test_submit_on_cloud_rejects_mem_and_requires_gpu(client, capsys, cloud_daemon, cloud_cwd,
                                                       monkeypatch):
    monkeypatch.chdir(cloud_cwd)
    code, out = run(client, capsys, "submit", "--time", "1h", "--on", "fake", "--mem", "24G",
                    "--gpu", "H100", "--", "python", "-c", "pass")
    assert code == 70 and "--mem" in out.err
    code, out = run(client, capsys, "submit", "--time", "1h", "--on", "fake", "--",
                    "python", "-c", "pass")
    assert code == 70 and "gpu" in out.err


def test_submit_on_cloud_rejects_data(client, capsys, cloud_daemon, cloud_cwd, monkeypatch):
    monkeypatch.chdir(cloud_cwd)
    code, out = run(client, capsys, "submit", "--time", "1h", "--on", "fake", "--gpu", "H100",
                    "--data", "dataset/", "--", "python", "-c", "pass")
    assert code == 70 and "provider work" in out.err


def test_ls_shows_cloud_cost(client, capsys, cloud_daemon, cloud_cwd, monkeypatch):
    monkeypatch.chdir(cloud_cwd)
    run(client, capsys, "submit", "--time", "1h", "--on", "fake", "--gpu", "H100", "--",
        "python", "-c", "pass")
    code, out = run(client, capsys, "ls")
    assert code == 0 and "awaiting" in out.out and "H100" in out.out and "$" in out.out


def test_restart_of_a_cloud_job_gives_the_equivalent_submit_command(client, capsys, cloud_daemon,
                                                                     cloud_cwd, monkeypatch):
    monkeypatch.chdir(cloud_cwd)
    run(client, capsys, "submit", "--time", "1h", "--on", "fake", "--gpu", "H100", "--",
        "python", "-c", "pass")
    run(client, capsys, "cancel", "1")
    code, out = run(client, capsys, "restart", "1")
    assert code == 70
    assert "pasar submit --on fake --gpu H100" in out.err


def test_wait_keeps_waiting_through_awaiting(client, capsys, cloud_daemon, cloud_cwd,
                                             monkeypatch):
    monkeypatch.chdir(cloud_cwd)
    run(client, capsys, "submit", "--time", "1h", "--on", "fake", "--gpu", "H100", "--",
        "python", "-c", "pass")
    code, out = run(client, capsys, "wait", "1", "--timeout", "0")
    assert code == 4  # still waiting on approval when the timeout hits, not treated as done
    assert "awaiting" in out.out


def test_submit_on_cloud_refused_when_estimate_exceeds_max_cost(client, capsys, cloud_daemon,
                                                                 cloud_cwd, monkeypatch):
    monkeypatch.chdir(cloud_cwd)
    code, out = run(client, capsys, "submit", "--time", "1h", "--on", "fake", "--gpu", "H100",
                    "--max-cost", "2", "--", "python", "-c", "pass")
    assert code == 70
    assert "$5.28" in out.err and "$2.00" in out.err


def test_submit_on_cloud_notes_the_users_cap_when_it_binds(client, capsys, cloud_daemon,
                                                            cloud_cwd, monkeypatch):
    monkeypatch.chdir(cloud_cwd)
    code, out = run(client, capsys, "submit", "--time", "1h", "--on", "fake", "--gpu", "H100",
                    "--max-cost", "6", "--", "python", "-c", "pass")
    assert code == 0
    assert "capped at $6.00 (your --max-cost)" in out.out


def test_show_says_how_much_run_time_a_cap_costs(client, capsys, cloud_daemon, cloud_cwd,
                                                 monkeypatch):
    monkeypatch.chdir(cloud_cwd)
    code, out = run(client, capsys, "submit", "--time", "1h", "--on", "fake", "--gpu", "H100",
                    "--max-cost", "6", "--", "python", "-c", "pass")
    assert code == 0 and "paused after 1h08m instead of 1h30m" in out.out
    code, out = run(client, capsys, "show", "1")
    assert code == 0
    assert "1h08m of run time (your --max-cost cuts it from 1h30m)" in out.out
    # and an uncapped job just says what it bought, with nothing cut from it
    run(client, capsys, "submit", "--time", "1h", "--on", "fake", "--gpu", "H100", "--",
        "python", "-c", "pass")
    code, out = run(client, capsys, "show", "2")
    assert code == 0 and "1h30m of run time" in out.out and "cuts it" not in out.out


def test_cloud_command_text_and_json(client, capsys, cloud_daemon, cloud_cwd, monkeypatch):
    monkeypatch.chdir(cloud_cwd)
    code, out = run(client, capsys, "cloud")
    assert code == 0 and "fake" in out.out and "nothing awaiting approval" in out.out
    assert "nothing running past its approved pace" in out.out
    run(client, capsys, "submit", "--time", "1h", "--on", "fake", "--gpu", "H100", "--",
        "python", "-c", "pass")
    code, out = run(client, capsys, "cloud", "--json")
    assert code == 0
    body = json.loads(out.out)
    assert body["targets"][0]["name"] == "fake"
    assert len(body["awaiting"]) == 1
    assert body["needs_time"] == []


def test_show_and_cloud_surface_a_job_that_needs_more_time(client, capsys, cloud_daemon,
                                                            cloud_cwd, monkeypatch):
    monkeypatch.chdir(cloud_cwd)
    code, out = run(client, capsys, "submit", "--time", "10m", "--on", "fake", "--gpu", "H100",
                    "--", "python", "-c", "pass")
    assert code == 0
    job_id = cloud_daemon.job(1).id
    cloud_daemon.approve(job_id)
    cloud_daemon.tick()  # the attempt starts now
    # Three reports on one line: 1000 of 5000 steps in 400s, far behind the 10m estimate's pace.
    # One report is never a pace (pasar.cloud.pace.MIN_REPORTS), and only the last of them sets
    # the projection.
    t0 = cloud_daemon.store.current_attempt(job_id).start_time
    for i in range(1, MIN_REPORTS + 1):
        cloud_daemon.clock.t = t0 + 400 * i / MIN_REPORTS
        with (cloud_daemon.job_dir(job_id) / "events.jsonl").open("a") as f:
            f.write(json.dumps({"event": "progress", "step": round(1000 * i / MIN_REPORTS),
                                "total_steps": 5000}) + "\n")
        cloud_daemon.tick()
    cloud_daemon.clock.advance(320)  # past the 300s observation window
    cloud_daemon.tick()

    code, out = run(client, capsys, "show", str(job_id))
    assert code == 0 and "running (needs more time)" in out.out
    assert "projected to run" in out.out and "extend it" in out.out

    code, out = run(client, capsys, "cloud")
    assert code == 0 and "running past the pace" in out.out


def _run_cloud_job_to_completion(client, capsys, cloud_daemon, cloud_provider, cloud_cwd,
                                 monkeypatch):
    """Submit, approve, launch and finish one cloud job through the CLI/API path; returns its
    id. Mirrors `start`/`finish` in test_daemon_cloud.py, but through the HTTP surface `pull`'s
    own CLI and API tests exercise."""
    monkeypatch.chdir(cloud_cwd)
    run(client, capsys, "submit", "--time", "1h", "--on", "fake", "--gpu", "H100", "--",
        "python", "-c", "pass")
    job_id = cloud_daemon.job(1).id
    cloud_daemon.approve(job_id)
    cloud_daemon.tick()
    handle = parse_unit(cloud_daemon.store.current_attempt(job_id).unit)[1]
    cloud_provider.finish(handle, 0)
    cloud_daemon.tick()
    assert cloud_daemon.job(job_id).state.value == "completed"
    return job_id


def test_pull_downloads_verifies_and_deletes_the_remote_copy(client, capsys, cloud_daemon,
                                                              cloud_provider, cloud_cwd, tmp_path,
                                                              monkeypatch):
    job_id = _run_cloud_job_to_completion(client, capsys, cloud_daemon, cloud_provider,
                                          cloud_cwd, monkeypatch)
    cloud_provider.persist(job_id, "checkpoint.pt", b"weights")
    dest = tmp_path / "out"

    code, out = run(client, capsys, "pull", str(job_id), "--to", str(dest))

    assert code == 0
    assert "pulled 1 file" in out.out and "deleted the remote copy" in out.out
    assert (dest / "checkpoint.pt").read_bytes() == b"weights"
    assert cloud_provider.deleted_persist == [job_id]


def test_pull_keep_leaves_the_remote_copy(client, capsys, cloud_daemon, cloud_provider,
                                          cloud_cwd, tmp_path, monkeypatch):
    job_id = _run_cloud_job_to_completion(client, capsys, cloud_daemon, cloud_provider,
                                          cloud_cwd, monkeypatch)
    cloud_provider.persist(job_id, "checkpoint.pt", b"weights")
    dest = tmp_path / "out"

    code, out = run(client, capsys, "pull", str(job_id), "--to", str(dest), "--keep")

    assert code == 0 and "kept the remote copy" in out.out
    assert cloud_provider.deleted_persist == []
    assert job_id in cloud_provider.persisted


def test_pull_reports_nothing_to_pull_and_exits_zero(client, capsys, cloud_daemon,
                                                     cloud_provider, cloud_cwd, tmp_path,
                                                     monkeypatch):
    job_id = _run_cloud_job_to_completion(client, capsys, cloud_daemon, cloud_provider,
                                          cloud_cwd, monkeypatch)
    dest = tmp_path / "out"

    code, out = run(client, capsys, "pull", str(job_id), "--to", str(dest))

    assert code == 0 and "nothing on the volume" in out.out
    assert not dest.exists()


def test_pull_refuses_a_job_that_is_still_running(client, capsys, cloud_daemon, cloud_provider,
                                                  cloud_cwd, monkeypatch):
    monkeypatch.chdir(cloud_cwd)
    run(client, capsys, "submit", "--time", "1h", "--on", "fake", "--gpu", "H100", "--",
        "python", "-c", "pass")
    job_id = cloud_daemon.job(1).id
    cloud_daemon.approve(job_id)
    cloud_daemon.tick()
    assert cloud_daemon.job(job_id).state.value == "running"

    code, out = run(client, capsys, "pull", str(job_id))

    assert code == 70 and "still live" in out.err


def test_pull_refuses_a_local_job(client, capsys, daemon, executor, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run(client, capsys, "submit", "--time", "1h", "--", "python a.py")
    daemon.tick()
    unit = daemon.store.current_attempt(1).unit
    executor.exit(unit, 0)
    daemon.tick()
    assert daemon.job(1).state.value == "completed"

    code, out = run(client, capsys, "pull", "1")

    assert code == 70 and "local GPU" in out.err


def test_pull_json_output(client, capsys, cloud_daemon, cloud_provider, cloud_cwd, tmp_path,
                          monkeypatch):
    job_id = _run_cloud_job_to_completion(client, capsys, cloud_daemon, cloud_provider,
                                          cloud_cwd, monkeypatch)
    cloud_provider.persist(job_id, "checkpoint.pt", b"weights")
    dest = tmp_path / "out"

    code, out = run(client, capsys, "pull", str(job_id), "--to", str(dest), "--json")

    assert code == 0
    body = json.loads(out.out)
    assert body == {"job_id": job_id, "files": 1, "bytes": len(b"weights"), "dest": str(dest),
                    "deleted": True}


def test_pull_api_rejects_a_relative_to(client, capsys, cloud_daemon, cloud_provider, cloud_cwd,
                                        monkeypatch):
    # The CLI always resolves --to to an absolute path before sending it (see the "pull"
    # branch of cli.run); this is what stops anyone talking to the API directly from getting a
    # download that lands relative to pasard's own cwd instead of theirs.
    job_id = _run_cloud_job_to_completion(client, capsys, cloud_daemon, cloud_provider,
                                          cloud_cwd, monkeypatch)
    r = client.post(f"/api/jobs/{job_id}/pull", json={"to": "relative/dir"})
    assert r.status_code == 422
    assert "absolute" in r.text


def test_show_and_cloud_state_the_jobs_lifetime_cap(client, capsys, cloud_daemon, cloud_cwd,
                                                    monkeypatch):
    monkeypatch.chdir(cloud_cwd)
    run(client, capsys, "submit", "--time", "1h", "--on", "fake", "--gpu", "H100", "--",
        "python", "-c", "pass")
    code, out = run(client, capsys, "show", "1")
    assert code == 0 and "$0.00 of its $10.00 job cap" in out.out
    code, out = run(client, capsys, "cloud")
    assert code == 0 and "JOB CAP" in out.out and "$10.00" in out.out
    code, out = run(client, capsys, "cloud", "--json")
    assert json.loads(out.out)["targets"][0]["max_job_cost"] == 10.0


def test_submit_says_when_the_job_cap_is_what_shortens_the_run(client, capsys, cloud_daemon,
                                                               cloud_cwd, monkeypatch):
    # 1h30m estimates $7.92, but its 2h15m window would cost $11.88: the $10 cap cuts it short,
    # and saying "your --max-cost" there would blame a flag nobody passed.
    monkeypatch.chdir(cloud_cwd)
    code, out = run(client, capsys, "submit", "--time", "1h30m", "--on", "fake", "--gpu", "H100",
                    "--", "python", "-c", "pass")
    assert code == 0
    assert "capped at $10.00 (the job cap)" in out.out and "--max-cost" not in out.out
    assert "instead of 2h15m" in out.out
    code, out = run(client, capsys, "show", "1")
    assert "(the $10.00 job cap cuts it from 2h15m)" in out.out


def test_submit_past_the_job_cap_is_refused_with_the_reason(client, capsys, cloud_daemon,
                                                            cloud_cwd, monkeypatch):
    monkeypatch.chdir(cloud_cwd)
    code, out = run(client, capsys, "submit", "--time", "3h", "--on", "fake", "--gpu", "H100",
                    "--", "python", "-c", "pass")
    assert code == 70 and "$15.83" in out.err and "max_job_cost" in out.err


def test_show_says_where_a_cloud_jobs_results_landed(client, capsys, cloud_daemon,
                                                     cloud_provider, cloud_cwd, tmp_path,
                                                     monkeypatch):
    job_id = _run_cloud_job_to_completion(client, capsys, cloud_daemon, cloud_provider,
                                          cloud_cwd, monkeypatch)
    cloud_provider.persist(job_id, "checkpoint.pt", b"weights")
    dest = tmp_path / "out"
    run(client, capsys, "pull", str(job_id), "--to", str(dest))

    code, out = run(client, capsys, "show", str(job_id))
    assert code == 0
    assert f"1 file(s), 0.0 GiB to {dest}" in out.out and "remote copy deleted" in out.out
    assert "sweep" not in out.out  # nothing left at the provider to sweep


def test_show_says_what_is_left_at_the_provider_and_when_it_goes(client, capsys, cloud_daemon,
                                                                 cloud_provider, cloud_cwd,
                                                                 tmp_path, monkeypatch):
    job_id = _run_cloud_job_to_completion(client, capsys, cloud_daemon, cloud_provider,
                                          cloud_cwd, monkeypatch)
    cloud_daemon.store.add_pull(job_id, cloud_daemon.clock(), str(tmp_path / "out"), None, None,
                                False, "not enough room", auto=True, remote_files=1,
                                remote_bytes=GiB // 2)
    at = cloud_daemon.sweeps_at(job_id)

    code, out = run(client, capsys, "show", str(job_id))
    assert code == 0 and "pulled" not in out.out
    assert "0.5 GiB still at fake" in out.out
    assert time.strftime("%Y-%m-%d %H:%M", time.localtime(at)) in out.out
    assert f"pasar pull {job_id}" in out.out and "not enough room" in out.out

    cloud_daemon.store.mark_cloud_swept(job_id, at, 1, GiB // 2)
    code, out = run(client, capsys, "show", str(job_id))
    assert "nobody pulled it: 0.5 GiB deleted from fake" in out.out
    assert "still at" not in out.out and "not enough room" not in out.out


def test_show_says_a_kept_remote_copy_is_still_swept(client, capsys, cloud_daemon,
                                                     cloud_provider, cloud_cwd, tmp_path,
                                                     monkeypatch):
    job_id = _run_cloud_job_to_completion(client, capsys, cloud_daemon, cloud_provider,
                                          cloud_cwd, monkeypatch)
    cloud_provider.persist(job_id, "checkpoint.pt", b"weights")
    run(client, capsys, "pull", str(job_id), "--to", str(tmp_path / "out"), "--keep")
    at = cloud_daemon.sweeps_at(job_id)

    code, out = run(client, capsys, "show", str(job_id))
    assert code == 0 and "remote copy kept" in out.out and "still at fake" in out.out
    assert time.strftime("%Y-%m-%d %H:%M", time.localtime(at)) in out.out


def test_cloud_shows_what_is_known_to_be_stored_per_target(client, capsys, cloud_daemon,
                                                           cloud_provider, cloud_cwd, tmp_path,
                                                           monkeypatch):
    job_id = _run_cloud_job_to_completion(client, capsys, cloud_daemon, cloud_provider,
                                          cloud_cwd, monkeypatch)
    # What a skipped pull measured, recorded without shipping half a GiB through a test.
    cloud_daemon.store.add_pull(job_id, cloud_daemon.clock(), str(tmp_path / "out"), None, None,
                                False, "no room", auto=True, remote_files=1,
                                remote_bytes=GiB // 2)

    code, out = run(client, capsys, "cloud")
    assert code == 0 and "STORED" in out.out and "0.5 GiB" in out.out
    assert "at least" in out.out  # a floor, not a live size: running jobs are not counted
    code, out = run(client, capsys, "cloud", "--json")
    [target] = json.loads(out.out)["targets"]
    assert target["known_stored_bytes"] == GiB // 2 and target["known_stored_jobs"] == 1
