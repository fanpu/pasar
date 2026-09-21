import json

import httpx
import pytest
from fastapi.testclient import TestClient

from pasar.api import create_app
from pasar.cli import ApiError, base_url, call, main, wait_code


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
