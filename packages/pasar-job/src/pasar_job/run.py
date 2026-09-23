"""Run a job in a cloud container and report to pasar over stdout.

stdout is the only channel every provider has, so events, GPU samples and the exit status
travel as control lines the daemon splits back out: \x1epasar:<token> <json>.

The wrapper is PID 1's job in the container (Modal and friends deliver a stop as SIGTERM to
it): it forwards SIGTERM to the job's whole process group, gives it --grace seconds to
checkpoint, then SIGKILLs. The same thing happens if the job simply runs past --limit.
"""

import argparse
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time

SAMPLE_INTERVAL = 5.0
NVIDIA_QUERY = "index,utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu"
NVIDIA_SMI_TIMEOUT = 10.0

# How the relay winds down once asked to stop: it keeps draining whatever is already readable,
# but a hard cutoff shortly after the request stops it even if a stray process keeps writing.
_RELAY_POLL = 0.1
_RELAY_STOP_GRACE = 0.5
_RELAY_JOIN_TIMEOUT = 2.0

_write_lock = threading.Lock()


def emit(token: str, obj: dict) -> None:
    _write_line(("\x1epasar:" + token + " " + json.dumps(obj, separators=(",", ":"))).encode())


def _write_line(data: bytes) -> None:
    """The one place that touches our stdout, so a relayed job line and a control line can
    never interleave into something the daemon can't parse."""
    with _write_lock:
        sys.stdout.buffer.write(data + b"\n")
        sys.stdout.buffer.flush()


def _relay(stream, stop: threading.Event) -> None:
    """Pass the job's stdout/stderr through to ours, one whole line at a time, byte for byte
    (no decoding, so binary or non-UTF-8 output survives intact).

    Polls instead of blocking in readline(): a job can leave a background process holding the
    pipe's write end open (`nohup foo &`, a logging sidecar, `(sleep 60 &)`) after the process
    the wrapper waits on has already exited, and by then the wrapper's own --limit/--grace
    enforcement has stopped running, so a blocking read here would wedge the wrapper forever.
    stop() tells this loop to wind down: it keeps draining whatever is already readable for a
    short extra grace, then gives up so the caller's bounded join() is honoured for real.
    """
    fd = stream.fileno()
    os.set_blocking(fd, False)
    buf = b""
    cutoff = None
    while True:
        ready, _, _ = select.select([fd], [], [], _RELAY_POLL)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                chunk = b""
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                _write_line(line)
        if stop.is_set():
            if cutoff is None:
                cutoff = time.time() + _RELAY_STOP_GRACE
            if time.time() >= cutoff:
                break
    if buf:
        _write_line(buf)


class _EventTailer:
    """Relays lines a job appends to $PASAR_EVENTS.

    A background thread polls; a final synchronous drain() right after the job exits picks up
    whatever it wrote in the instant before exiting, which the poll loop could otherwise still
    be asleep for.
    """

    def __init__(self, path: str, token: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        open(path, "a").close()
        self._f = open(path)  # noqa: SIM115 - kept open for the tailer's whole lifetime
        self._token = token
        self._lock = threading.Lock()

    def drain(self) -> None:
        with self._lock:
            while True:
                line = self._f.readline()
                if not line:
                    return
                try:
                    emit(self._token, {"t": "event", "e": json.loads(line)})
                except ValueError:
                    pass

    def poll(self, stop: threading.Event) -> None:
        while not stop.wait(0.2):
            self.drain()


def _sample_gpus(token: str, stop: threading.Event) -> None:
    """Poll nvidia-smi for GPU stats; give up for good only if it isn't installed at all."""
    while not stop.wait(SAMPLE_INTERVAL):
        try:
            out = subprocess.run(
                ["nvidia-smi", f"--query-gpu={NVIDIA_QUERY}", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=NVIDIA_SMI_TIMEOUT, check=False,
            ).stdout
        except FileNotFoundError:
            return
        except (OSError, subprocess.TimeoutExpired):
            continue
        rows = []
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != 6:
                continue
            try:
                rows.append([float(p) for p in parts])
            except ValueError:
                continue
        if rows:
            emit(token, {"t": "sample", "gpus": rows})


def _signal_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True)
    ap.add_argument("--events", required=True)
    ap.add_argument("--limit", type=float, required=True, help="seconds of run time approved")
    ap.add_argument("--grace", type=float, default=120.0)
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command

    os.environ["PASAR_EVENTS"] = args.events
    stop = threading.Event()
    tailer = _EventTailer(args.events, args.token)
    threading.Thread(target=tailer.poll, args=(stop,), daemon=True).start()
    threading.Thread(target=_sample_gpus, args=(args.token, stop), daemon=True).start()

    proc: subprocess.Popen | None = None
    reason: str | None = None
    kill_at: float | None = None

    def on_sigterm(signum, frame):
        nonlocal reason, kill_at
        if reason is None:
            reason = "stopped"
            kill_at = time.time() + args.grace
            if proc is not None:
                _signal_group(proc.pid, signal.SIGTERM)

    # Installed before the job exists: a stop arriving in the gap between here and Popen is
    # still recorded, and gets forwarded the moment there is a process group to forward it to.
    signal.signal(signal.SIGTERM, on_sigterm)

    emit(args.token, {"t": "phase", "phase": "running", "ts": time.time()})
    proc = subprocess.Popen(["bash", "-c", " ".join(command)], start_new_session=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if reason is not None:
        _signal_group(proc.pid, signal.SIGTERM)

    relay_stop = threading.Event()
    relay = threading.Thread(target=_relay, args=(proc.stdout, relay_stop), daemon=True)
    relay.start()

    deadline = time.time() + args.limit
    while True:
        try:
            code = proc.wait(timeout=1)
            break
        except subprocess.TimeoutExpired:
            pass
        now = time.time()
        if reason is None and now >= deadline:
            reason = "time_limit"
            kill_at = now + args.grace
            _signal_group(proc.pid, signal.SIGTERM)
        if kill_at is not None and now >= kill_at:
            _signal_group(proc.pid, signal.SIGKILL)
            code = proc.wait()
            break

    # The job's own process may have exited while a process it backgrounded is still holding
    # the pipe's write end open; kill whatever is left of its process group so the relay sees
    # EOF straight away, then bound the wait so a process that somehow escaped the group (a
    # double fork, setsid) can never wedge the wrapper past its own deadline.
    _signal_group(proc.pid, signal.SIGKILL)
    relay_stop.set()
    relay.join(max(0.0, min(_RELAY_JOIN_TIMEOUT, deadline - time.time())))

    tailer.drain()
    stop.set()
    sig = signal.Signals(-code).name if code < 0 else None
    exit_obj = {"t": "exit", "code": None if code < 0 else code, "signal": sig}
    if reason is not None:
        exit_obj["reason"] = reason
    emit(args.token, exit_obj)
    return 128 - code if code < 0 else code


if __name__ == "__main__":
    raise SystemExit(main())
