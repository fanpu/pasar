from fastapi.testclient import TestClient

from pasar import mascot
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
    daemon.tick()  # fails; retries default 0, so it stays failed until an explicit restart
    assert daemon.job(1).state == State.FAILED

    # Restart so a second attempt launches and history restarts.
    daemon.restart(1)
    daemon.clock.advance(2)
    daemon.tick()
    assert len(daemon.usage_history[1]) <= 2


def make_webui(tmp_path):
    ui = tmp_path / "webui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<!doctype html><title>pasar</title>")
    (ui / "assets" / "app-abc123.js").write_text("console.log(1)")
    return ui


def test_spa_fallback_and_assets(daemon, tmp_path):
    c = TestClient(create_app(daemon, allowed_hosts=["testserver"], webui_dir=make_webui(tmp_path)))
    for path in ["/", "/jobs/42"]:
        r = c.get(path)
        assert r.status_code == 200 and "<title>pasar</title>" in r.text
        assert r.headers["cache-control"] == "no-cache"
    r = c.get("/assets/app-abc123.js")
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    assert c.get("/assets/missing.js").status_code == 404
    r = c.get("/api/nope")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")


def test_assets_traversal_is_404(daemon, tmp_path):
    ui = make_webui(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    link = ui / "assets" / "escape"
    link.symlink_to(outside)
    c = TestClient(create_app(daemon, allowed_hosts=["testserver"], webui_dir=ui))
    assert c.get("/assets/..%2Findex.html").status_code == 404
    assert c.get("/assets/%2e%2e/%2e%2e/etc/passwd").status_code == 404
    assert c.get("/assets/escape").status_code == 404


def test_unbuilt_ui_says_how_to_build(daemon, tmp_path):
    c = TestClient(create_app(daemon, allowed_hosts=["testserver"], webui_dir=tmp_path / "none"))
    r = c.get("/")
    assert r.status_code == 503
    assert "npm run build" in r.text
    assert "uv tool install --force ." in r.text


def test_mascot_routes(daemon, tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    (builtin / "idle.svg").write_text("<svg/>")
    monkeypatch.setattr(mascot, "BUILTIN_DIR", builtin)
    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "happy.png").write_bytes(b"\x89PNG")
    daemon.cfg.mascot_dir = str(custom)
    c = TestClient(create_app(daemon, allowed_hosts=["testserver"], webui_dir=tmp_path / "none"))
    m = c.get("/api/mascot").json()
    assert m["happy"] == ["/mascot/happy.png"] and m["idle"] == ["/mascot/builtin/idle.svg"]
    assert c.get("/mascot/happy.png").content == b"\x89PNG"
    assert c.get("/mascot/builtin/idle.svg").status_code == 200
    assert c.get("/mascot/secret.png").status_code == 404
    assert c.get("/mascot/builtin/nope.svg").status_code == 404
