"""Real-process check that `pasard` exits promptly on SIGTERM/SIGINT even with an open
`/api/stream` connection, instead of hanging until something SIGKILLs it (the bug this guards
against: an infinite SSE generator plus uvicorn's own indefinite wait for open connections)."""

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.systemd


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pasard_path() -> Path:
    """The `pasard` console script installed next to the current interpreter (works for both
    a `uv run` venv and a plain `.venv`)."""
    candidate = Path(sys.executable).parent / "pasard"
    if candidate.exists():
        return candidate
    raise AssertionError(f"pasard entry point not found next to {sys.executable}")


def _wait_for_status(base_url: str, timeout: float = 15) -> None:
    deadline = time.time() + timeout
    last_exc = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base_url}/api/status", timeout=1)
            if r.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_exc = exc
        time.sleep(0.2)
    raise AssertionError(f"pasard never came up on {base_url}: {last_exc}")


def _start_pasard(tmp_path: Path, port: int) -> subprocess.Popen:
    env = {
        **os.environ,
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "PASAR_ADDRESS": f"127.0.0.1:{port}",
    }
    return subprocess.Popen([str(_pasard_path())], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_exits_promptly_with_an_open_stream_connection(tmp_path, sig):
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = _start_pasard(tmp_path, port)
    try:
        _wait_for_status(base_url)

        stop_reading = threading.Event()
        stream_opened = threading.Event()

        def read_stream():
            try:
                with httpx.stream("GET", f"{base_url}/api/stream", timeout=None) as r:
                    stream_opened.set()
                    for _ in r.iter_lines():
                        if stop_reading.is_set():
                            break
            except (httpx.HTTPError, RuntimeError):
                # The connection gets reset once pasard exits; that's expected, not a failure.
                stream_opened.set()

        reader = threading.Thread(target=read_stream, daemon=True)
        reader.start()
        assert stream_opened.wait(timeout=5), "never managed to open /api/stream"
        time.sleep(0.2)  # let the request actually reach the server, not just the socket

        proc.send_signal(sig)
        try:
            code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            raise AssertionError(
                f"pasard did not exit within 10s of {signal.Signals(sig).name}; "
                f"output:\n{proc.stdout.read()}"
            ) from None
        assert code == 0, f"pasard exited {code}; output:\n{proc.stdout.read()}"

        stop_reading.set()
        reader.join(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
