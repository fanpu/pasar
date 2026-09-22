"""`pasar`: command-line client for the pasar daemon."""

import argparse
import getpass
import json
import os
import shlex
import sys
import time

import httpx

from pasar.config import DEFAULT_ADDRESS
from pasar.guide import load_guide
from pasar.units import fmt_duration, fmt_gib

EX_USAGE, EX_UNAVAILABLE, EX_API = 64, 69, 70
WAIT_TIMEOUT = 4
OOM_REASONS = {"oom", "gpu_oom", "kernel_oom"}


class ApiError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"pasar: error: {message}", file=sys.stderr)
        raise SystemExit(EX_USAGE)


def wait_code(job: dict) -> int:
    if job["state"] == "completed":
        return 0
    if job["state"] == "cancelled":
        return 3
    if job.get("reason") in OOM_REASONS:
        return 2
    return 1


def base_url() -> str:
    if os.environ.get("PASAR_URL"):
        return os.environ["PASAR_URL"]
    return "http://" + DEFAULT_ADDRESS


def _error_message(detail) -> str:
    """Format an API error `detail`, which is either a plain string (our own handlers) or a
    list of FastAPI request-validation error dicts (each with a `msg` field)."""
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        return "; ".join(
            item.get("msg", json.dumps(item)) if isinstance(item, dict) else str(item)
            for item in detail
        )
    return json.dumps(detail)


def _response_detail(r: httpx.Response):
    """Pull an error `detail` out of a response body, without assuming it's a dict (a
    non-JSON or non-dict body degrades to the raw text/body instead of raising)."""
    try:
        body = r.json()
    except ValueError:
        return r.text
    return body.get("detail") if isinstance(body, dict) else body


def call(client: httpx.Client, method: str, path: str, **kw):
    try:
        r = client.request(method, path, **kw)
    except httpx.TransportError as e:
        raise ConnectionError(f"cannot reach pasard at {client.base_url}: {e}") from e
    if r.status_code >= 400:
        raise ApiError(_error_message(_response_detail(r)))
    return r.json()


def _mem(job: dict) -> str:
    if job["mode"] == "whole":
        return "whole GPU"
    if job.get("usage") is not None:
        return f"{fmt_gib(job['usage'])} / {fmt_gib(job['limit'])}"
    return fmt_gib(job["mem_request"])


def _expected(job: dict) -> str:
    """Expected total run time, e.g. `~48m`; `~48m*` when projected from progress reports."""
    total = job.get("expected_runtime", job["est_runtime"])
    return f"~{fmt_duration(total)}" + ("*" if job.get("eta_source") == "progress" else "")


def _when(job: dict) -> str:
    if job["state"] in ("running", "stopping"):
        return f"{fmt_duration(job['run_time'])} / {_expected(job)}"
    if job["state"] == "queued":
        if job["projected"]:
            return "starts ~" + time.strftime("%H:%M", time.localtime(job["projected"][0][0]))
        return "blocked"
    ended = job.get("end_time")
    return "ended " + time.strftime("%H:%M", time.localtime(ended)) if ended else ""


def _state(job: dict) -> str:
    if job["state"] in ("failed", "cancelled") and job.get("reason"):
        return f"{job['state']} ({job['reason']})"
    if job["state"] == "queued" and job.get("preemptions"):
        return f"queued (preempted x{job['preemptions']})"
    return job["state"]


def print_table(jobs: list[dict]) -> None:
    rows = [("ID", "NAME", "STATE", "BID", "MEMORY", "TIME", "BY")]
    for j in jobs:
        rows.append((str(j["id"]), j["name"], _state(j), str(j["bid"]), _mem(j), _when(j),
                     j["submitter"]))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    if any(r[5].endswith("*") for r in rows[1:]):
        print("* projected from the job's progress reports")


def _time_line(job: dict) -> str:
    ran = fmt_duration(job["run_time"])
    if job.get("eta_source") == "progress" and job["state"] in ("running", "stopping"):
        return (f"{ran} of ~{fmt_duration(job['expected_runtime'])} from progress"
                f" (estimated {fmt_duration(job['est_runtime'])})")
    return f"{ran} of ~{fmt_duration(job['est_runtime'])}"


def print_job(job: dict) -> None:
    attempts = job["attempts"]
    fields = [
        ("job", f"#{job['id']} {job['name']}"), ("state", _state(job)),
        ("summary", job["summary"]), ("bid", f"{job['bid']} (may preempt lower bids)" if job.get("preempt") else job["bid"]),
        ("memory", _mem(job)),
        ("time", _time_line(job)),
        ("attempts", len(attempts) if isinstance(attempts, list) else attempts),
        ("command", job["command"]), ("cwd", job["cwd"]),
        ("note", job["note"]), ("by", job["submitter"]), ("git", job["git_commit"] or ""),
    ]
    for k, v in fields:
        if v not in ("", None):
            print(f"{k:>9}  {v}")


def build_parser() -> Parser:
    p = Parser(prog="pasar", description="Submit and manage jobs on the pasar GPU scheduler.")
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=Parser)

    def add(name, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--json", action="store_true", help="machine-readable output")
        return sp

    s = add("submit", "queue a job")
    s.add_argument("--time", required=True, help="estimated runtime, e.g. 2h30m")
    s.add_argument("--mem", help="share the GPU, reserving this much memory (e.g. 24G); omit for the whole GPU (recommended)")
    s.add_argument("--bid", type=int, help="priority (default 1000); higher bids go first")
    s.add_argument("--preempt", action="store_true", help="may stop running jobs with a lower bid to start now (never carried over)")
    s.add_argument("--non-preemptible", action="store_true", dest="no_preempt",
                   help="other jobs may never stop this one")
    s.add_argument("--no-preempt", action="store_true", dest="no_preempt", help=argparse.SUPPRESS)
    s.add_argument("--grace", help="time to save a checkpoint when stopped (default 120s)")
    s.add_argument("--retries", type=int, default=0)
    s.add_argument("--name", default="")
    s.add_argument("--note", default="", help="why this job matters")
    s.add_argument("--tag", action="append", default=[])
    s.add_argument("--by", default=None, help="who is submitting (default: $USER)")
    s.add_argument("--cwd", default=None)
    s.add_argument("--no-env", action="store_true", help="don't pass your environment")
    s.add_argument("command", nargs=argparse.REMAINDER, help="-- command to run")

    ls = add("ls", "list jobs")
    ls.add_argument("--all", action="store_true", help="include older finished jobs")
    ls.add_argument("--state")

    for name, help_ in (("show", "show one job"), ("cancel", "cancel a job")):
        add(name, help_).add_argument("id", type=int)

    lg = add("logs", "print a job's output")
    lg.add_argument("id", type=int)
    lg.add_argument("-f", "--follow", action="store_true")

    b = add("bid", "change a job's bid")
    b.add_argument("id", type=int)
    b.add_argument("bid", type=int)
    b.add_argument("--preempt", action="store_true", help="may stop running jobs with a lower bid to start now (never carried over)")

    r = add("restart", "requeue a finished job")
    r.add_argument("id", type=int)
    r.add_argument("--mem")
    r.add_argument("--whole-gpu", action="store_true")
    r.add_argument("--time")
    r.add_argument("--bid", type=int)
    r.add_argument("--retries", type=int)
    r.add_argument("--preempt", action="store_true", help="may stop running jobs with a lower bid to start now (never carried over)")

    w = add("wait", "wait for a job to finish; exit code reflects the outcome")
    w.add_argument("id", type=int)
    w.add_argument("--timeout", type=float, default=None, help="seconds")
    w.add_argument("--interval", type=float, default=2.0, help=argparse.SUPPRESS)

    add("status", "machine and memory pool status")
    sub.add_parser("guide", help="print the agent guide (works without a daemon running)")
    return p


def run(args, client: httpx.Client) -> int:
    if args.cmd == "guide":
        sys.stdout.write(load_guide())
        return 0
    out = (lambda obj: print(json.dumps(obj, indent=2))) if args.json else None
    if args.cmd == "submit":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            raise SystemExit(EX_USAGE)
        body = {
            "command": command[0] if len(command) == 1 else shlex.join(command),
            "time": args.time, "cwd": os.path.abspath(args.cwd or os.getcwd()),
            "mem": args.mem, "bid": args.bid, "preempt": args.preempt, "preemptible": not args.no_preempt,
            "grace": args.grace, "retries": args.retries, "name": args.name, "note": args.note,
            "tags": args.tag, "submitter": args.by or getpass.getuser(),
            "env": None if args.no_env else dict(os.environ),
        }
        job = call(client, "POST", "/api/jobs", json=body)
        out(job) if out else print(f"submitted #{job['id']} {job['name']} ({_state(job)})")
    elif args.cmd == "ls":
        params = {"all": args.all}
        if args.state:
            params["state"] = args.state
        jobs = call(client, "GET", "/api/jobs", params=params)
        out(jobs) if out else print_table(jobs)
    elif args.cmd == "show":
        job = call(client, "GET", f"/api/jobs/{args.id}")
        out(job) if out else print_job(job)
    elif args.cmd == "cancel":
        job = call(client, "POST", f"/api/jobs/{args.id}/cancel")
        out(job) if out else print(f"#{job['id']} {job['state']}")
    elif args.cmd == "bid":
        body = {"bid": args.bid, "preempt": args.preempt}
        job = call(client, "PATCH", f"/api/jobs/{args.id}", json=body)
        extra = ", may preempt lower bids" if job["preempt"] else ""
        out(job) if out else print(f"#{job['id']} bid is now {job['bid']}{extra}")
    elif args.cmd == "restart":
        body = {"mem": args.mem, "whole_gpu": args.whole_gpu, "time": args.time,
                "bid": args.bid, "retries": args.retries, "preempt": args.preempt}
        job = call(client, "POST", f"/api/jobs/{args.id}/restart", json=body)
        out(job) if out else print(f"#{job['id']} requeued")
    elif args.cmd == "logs":
        if args.follow:
            try:
                # The server sends a comment every ~15 s of quiet (see KEEPALIVE_INTERVAL), so
                # this stream can safely wait past the client's normal read timeout; only the
                # initial connect still needs to time out promptly.
                with client.stream("GET", f"/api/jobs/{args.id}/logs", params={"follow": True},
                                   timeout=httpx.Timeout(30, read=None)) as r:
                    if r.status_code >= 400:
                        r.read()
                        raise ApiError(_error_message(_response_detail(r)))
                    for line in r.iter_lines():
                        # SSE comment lines (e.g. ": keep-alive") and the blank lines between
                        # events are not data; only "data: " lines carry log text.
                        if line.startswith("data: "):
                            sys.stdout.write(json.loads(line[6:]).get("text", ""))
                            sys.stdout.flush()
            except httpx.TransportError as e:
                raise ConnectionError(f"cannot reach pasard at {client.base_url}: {e}") from e
        else:
            r = call(client, "GET", f"/api/jobs/{args.id}/logs")
            out(r) if out else sys.stdout.write(r["text"])
    elif args.cmd == "wait":
        deadline = None if args.timeout is None else time.monotonic() + args.timeout
        while True:
            job = call(client, "GET", f"/api/jobs/{args.id}")
            if job["state"] in ("completed", "failed", "cancelled"):
                out(job) if out else print(f"#{job['id']} {_state(job)} {job['summary']}".rstrip())
                return wait_code(job)
            if deadline is not None and time.monotonic() >= deadline:
                out(job) if out else print(f"#{job['id']} still {job['state']}")
                return WAIT_TIMEOUT
            time.sleep(args.interval)
    elif args.cmd == "status":
        s = call(client, "GET", "/api/status")
        if out:
            out(s)
        else:
            print(f"pool      {fmt_gib(s['reserved'])} reserved of {fmt_gib(s['pool'])}, "
                  f"{fmt_gib(max(0, s['free']))} free")
            print(f"external  {fmt_gib(s['external'])} used outside pasar")
            print(f"memory    {fmt_gib(s['mem_available'])} available, "
                  f"pressure {s['psi_some_avg10']:.1f}%")
    return 0


def main(argv: list[str] | None = None, client: httpx.Client | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    try:
        client = client or httpx.Client(base_url=base_url(), timeout=30)
        return run(args, client)
    except SystemExit as e:
        return int(e.code or 0)
    except ConnectionError as e:
        print(f"pasar: {e}", file=sys.stderr)
        return EX_UNAVAILABLE
    except ApiError as e:
        print(f"pasar: {e}", file=sys.stderr)
        return EX_API
