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
import signal
import subprocess
import sys
import threading
import time

SAMPLE_INTERVAL = 5.0
NVIDIA_QUERY = "index,utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu"


def emit(token: str, obj: dict) -> None:
    sys.stdout.write("\x1epasar:" + token + " " + json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _tail_events(path: str, token: str, stop: threading.Event) -> None:
    """Forward lines a job appends to $PASAR_EVENTS as they arrive."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    open(path, "a").close()
    with open(path) as f:
        while not stop.is_set():
            line = f.readline()
            if not line:
                time.sleep(0.2)
                continue
            try:
                emit(token, {"t": "event", "e": json.loads(line)})
            except ValueError:
                pass


def _sample_gpus(token: str, stop: threading.Event) -> None:
    """Poll nvidia-smi for GPU stats; give up for good the first time it isn't there."""
    while not stop.wait(SAMPLE_INTERVAL):
        try:
            out = subprocess.run(
                ["nvidia-smi", f"--query-gpu={NVIDIA_QUERY}", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10, check=False,
            ).stdout
        except OSError:
            return
        rows = []
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 6:
                rows.append([float(p) for p in parts])
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
    threading.Thread(target=_tail_events, args=(args.events, args.token, stop), daemon=True).start()
    threading.Thread(target=_sample_gpus, args=(args.token, stop), daemon=True).start()

    emit(args.token, {"t": "phase", "phase": "running", "ts": time.time()})
    proc = subprocess.Popen(["bash", "-c", " ".join(command)], start_new_session=True)

    reason: str | None = None
    kill_at: float | None = None

    def on_sigterm(signum, frame):
        nonlocal reason, kill_at
        if reason is None:
            reason = "stopped"
            kill_at = time.time() + args.grace
            _signal_group(proc.pid, signal.SIGTERM)

    signal.signal(signal.SIGTERM, on_sigterm)
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

    stop.set()
    sig = signal.Signals(-code).name if code < 0 else None
    exit_obj = {"t": "exit", "code": None if code < 0 else code, "signal": sig}
    if reason is not None:
        exit_obj["reason"] = reason
    emit(args.token, exit_obj)
    return 128 - code if code < 0 else code


if __name__ == "__main__":
    raise SystemExit(main())
