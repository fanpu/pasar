"""`pasar`: command-line client for the pasar daemon."""

import argparse
import getpass
import json
import os
import shlex
import sys
import time
from dataclasses import asdict

import httpx

from pasar.cloud.check import CloudCheckResult, check_targets
from pasar.config import DEFAULT_ADDRESS, load_config
from pasar.guide import TOPICS, load_guide
from pasar.units import fmt_duration, fmt_gib

EX_USAGE, EX_UNAVAILABLE, EX_API = 64, 69, 70
EX_CLOUD_CHECK = 1  # pasar cloud check: a target is unusable, or two share a workspace
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


def _money(n: float | None) -> str:
    return f"${n:.2f}" if n is not None else "?"


def _mem(job: dict) -> str:
    cloud = job.get("cloud")
    if cloud is not None:
        label = cloud["gpu"] or cloud["target"]
        cost = cloud.get("estimated_cost")
        bits = [f"~{_money(cost)}"] if cost is not None else []
        if cloud.get("owner"):
            bits.append(cloud["owner"])
        return f"{label} ({' · '.join(bits)})" if bits else label
    if job["mode"] == "whole":
        return "whole GPU"
    if job.get("usage") is not None:
        return f"{fmt_gib(job['usage'])} / {fmt_gib(job['limit'])}"
    return fmt_gib(job["mem_request"])


def _cap_note(cloud: dict) -> str:
    """Which dollar cap set the ceiling, when one did: the submitter's `--max-cost`, or the job's
    lifetime cap. A shortened run nobody passed `--max-cost` for is the job cap's doing, and
    saying "your --max-cost" there would blame a flag nobody used."""
    if cloud.get("user_capped"):
        return " (your --max-cost)"
    approved, full = cloud.get("approved_seconds"), cloud.get("full_seconds")
    if approved and full and approved < full:
        return " (the job cap)"
    return ""


def _approved_run(cloud: dict) -> str:
    """How much run time one approval buys, and — when a dollar cap (`--max-cost`, or the job's
    lifetime cap) is what limits it — how much time that cap is costing. A dollar cap is enforced
    by pausing the job early, which is not what somebody who capped dollars expects to have
    bought, so it is said plainly rather than left to be discovered when the job pauses."""
    approved, full = cloud.get("approved_seconds"), cloud.get("full_seconds")
    if not approved:
        return ""
    if full and approved < full:
        who = ("your --max-cost" if cloud.get("user_capped")
               else f"the {_money(cloud.get('job_cap'))} job cap")
        return f"{fmt_duration(approved)} of run time ({who} cuts it from {fmt_duration(full)})"
    return f"{fmt_duration(approved)} of run time"


def _expected(job: dict) -> str:
    """Expected total run time, e.g. `~48m`; `~48m*` when projected from progress reports."""
    total = job["expected_runtime"]
    return f"~{fmt_duration(total)}" + ("*" if job["eta_source"] == "progress" else "")


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
    if job["state"] == "queued" and job["preemptions"]:
        return f"queued (preempted x{job['preemptions']})"
    cloud = job.get("cloud")
    if cloud and cloud.get("needs_more_time"):
        return f"{job['state']} (needs more time)"
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
    if job["eta_source"] == "progress" and job["state"] in ("running", "stopping"):
        return (f"{ran} of ~{fmt_duration(job['expected_runtime'])} from progress"
                f" (estimated {fmt_duration(job['est_runtime'])})")
    return f"{ran} of ~{fmt_duration(job['est_runtime'])}"


def _clock(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _persist_fields(job_id: int, target: str, persist: dict) -> list[tuple[str, str]]:
    """What a finished cloud job left behind: where a pull put it, what is still at the provider
    and when the retention sweep deletes it there, and what the sweep already did. Said with a
    date, because results left at a provider do not stay there for good."""
    fields = []
    if persist["files"]:
        kept = "remote copy deleted" if persist["remote_deleted"] else "remote copy kept"
        pulled = (f"{persist['files']} file(s), {fmt_gib(persist['bytes'])} to "
                  f"{persist['pulled_to']} at {_clock(persist['pulled_at'])} ({kept})")
        fields.append(("pulled", pulled))
    held = persist["remote_bytes"]
    if persist["sweeps_at"] is not None and held != 0:
        what = "whatever it left is" if held is None else f"{fmt_gib(held)} still"
        line = (f"{what} at {target}, deleted there by the retention sweep after "
                f"{_clock(persist['sweeps_at'])}")
        if not persist["files"]:
            line += f"; pull it before then: pasar pull {job_id} --to <dir>"
        fields.append(("remote", line))
        fields.append(("last pull", persist["last_error"]))
    if persist["swept_at"] is not None and persist["swept_bytes"]:
        swept = (f"nobody pulled it: {fmt_gib(persist['swept_bytes'])} deleted from {target} "
                 f"at {_clock(persist['swept_at'])}")
        fields.append(("swept", swept))
    return fields


def print_job(job: dict) -> None:
    attempts = job["attempts"]
    cloud = job.get("cloud")
    fields = [
        ("job", f"#{job['id']} {job['name']}"), ("state", _state(job)),
        ("summary", job["summary"]), ("bid", f"{job['bid']} (may preempt lower bids)" if job["preempt"] else job["bid"]),
        ("memory", _mem(job)),
        ("time", _time_line(job)),
        ("attempts", len(attempts) if isinstance(attempts, list) else attempts),
        ("command", job["command"]), ("cwd", job["cwd"]),
        ("note", job["note"]), ("by", job["submitter"]), ("git", job["git_commit"] or ""),
    ]
    if cloud is not None:
        cost = (f"est {_money(cloud['estimated_cost'])}, "
                f"capped at {_money(cloud['max_cost'])}{_cap_note(cloud)}")
        # `phase` is the live attempt's own, and is absent between attempts; the job's state is
        # already on its own line, so there is nothing to fall back to and nothing to repeat.
        where = f" · {cloud['phase']}" if cloud["phase"] else ""
        owner = f" ({cloud['owner']}'s account)" if cloud.get("owner") else ""
        fields.append(("cloud", f"{cloud['target']}{owner} · {cloud['gpu']}{where}"))
        fields.append(("cost", cost))
        fields.append(("approved", _approved_run(cloud)))
        if cloud.get("job_cap") is not None:
            spent = f"{_money(cloud['job_spent'])} of its {_money(cloud['job_cap'])} job cap"
            fields.append(("spent", spent))
        if cloud.get("needs_more_time"):
            pace = (f"projected to run {fmt_duration(cloud['needs_more_time'])} past its "
                    "approved time; extend it in the web UI to keep it running "
                    "(POST .../approve?extend=1)")
            fields.append(("pace", pace))
        if cloud["console_url"]:
            fields.append(("console", cloud["console_url"]))
        if cloud.get("persist"):
            fields += _persist_fields(job["id"], cloud["target"], cloud["persist"])
    for k, v in fields:
        if v not in ("", None):
            print(f"{k:>9}  {v}")


def _gpu_mem(memory_gb: float | None) -> str:
    return f"{memory_gb:g}GB" if memory_gb is not None else "?"


def _extra_rates(rates: dict) -> str:
    """The RATES column: what a GPU's $/hour is added to (the sandbox's CPU and memory) and
    storage, not the whole price list. GPUs have a table of their own, and a provider's list
    also prices model endpoints nothing here runs — Modal's runs to dozens of keys. `--json`
    keeps every key. Sub-cent rates keep their digits: 32 GiB at $0.024 is not 32 at $0.02."""
    kept = {k: v for k, v in rates.items()
            if not k.startswith("gpu_hour_cost_") and "endpoint" not in k}
    return ", ".join(f"{k}=${v:.4g}" for k, v in sorted(kept.items())) or "none"


def print_cloud(body: dict) -> None:
    targets = body["targets"]
    if not targets:
        print("no cloud targets configured")
    else:
        rows = [("TARGET", "OWNER", "GROUP", "PROVIDER", "RUNNING", "TODAY", "MONTH", "JOB CAP",
                 "STORED", "RATES")]
        for t in targets:
            today = f"{_money(t['spent_today'])} / {_money(t['daily_budget'])}"
            month = f"{_money(t['spent_month'])} / {_money(t['monthly_budget'])}"
            # pasar's own budgets, each under its own column (the lane gates on both, and
            # `budget_exhausted` is whichever runs out first), then the provider's own books
            # where pasard has read them.
            if t["spent_today"] >= t["daily_budget"]:
                today += " (budget exhausted)"
            if t["spent_month"] >= t["monthly_budget"]:
                month += " (budget exhausted)"
            if t.get("credit_exhausted"):
                month += " (credit exhausted)"
            running = f"{t['running']}/{t['max_running']}"
            rates = _extra_rates(t["rates"])
            name = t["name"] + ("" if t["configured"] else " (no provider)")
            stored = f"{fmt_gib(t['known_stored_bytes'])} ({t['known_stored_jobs']} job(s))"
            rows.append((name, t.get("owner") or "-", t.get("group") or "-", t["provider"],
                        running, today, month, _money(t.get("max_job_cost")), stored, rates))
        widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
        for r in rows:
            print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
        print("STORED is at least what finished jobs left at the provider, as a pull last "
              "measured it;\nrunning and paused jobs' checkpoints are not counted. Storage is "
              "billed even when nothing runs.")
        for t in targets:
            gpus = t.get("gpus") or []
            if not gpus:
                continue
            print(f"\n{t['name']} GPUs (what --gpu accepts):")
            grows = [("GPU", "$/HOUR", "MEMORY")]
            for g in gpus:
                grows.append((g["name"], _money(g["hourly_rate"]), _gpu_mem(g["memory_gb"])))
            gwidths = [max(len(r[i]) for r in grows) for i in range(len(grows[0]))]
            for r in grows:
                print("  " + "  ".join(c.ljust(w) for c, w in zip(r, gwidths)).rstrip())
    awaiting = body["awaiting"]
    print()
    if awaiting:
        print(f"{len(awaiting)} job(s) awaiting approval:")
        print_table(awaiting)
    else:
        print("nothing awaiting approval")
    needs_time = body.get("needs_time", [])
    print()
    if needs_time:
        print(f"{len(needs_time)} job(s) running past the pace their approval allows for:")
        print_table(needs_time)
    else:
        print("nothing running past its approved pace")


def print_cloud_check(result: CloudCheckResult) -> None:
    if not result.targets:
        print("no cloud targets with a profile configured")
        return
    # LEFT OF BUDGET is pasar's own monthly budget for the target less what Modal says is used,
    # not Modal's own allowance, which its billing summary does not report.
    rows = [("TARGET", "OWNER", "GROUP", "WORKSPACE", "STATUS", "CREDIT USED", "LEFT OF BUDGET",
             "CYCLE")]
    for t in result.targets:
        status = "ok" if t.ok else f"UNUSABLE: {t.error}"
        if t.exhausted:
            status += " (credit exhausted)"
        used = _money(t.credit_used) if t.credit_used is not None else "?"
        left = _money(t.budget_left) if t.budget_left is not None else "?"
        cycle = (f"{_clock(t.cycle_start)}..{_clock(t.cycle_end)}"
                 if t.cycle_start is not None and t.cycle_end is not None else "?")
        rows.append((t.name, t.owner, t.group or "-", t.workspace or "-", status, used, left,
                    cycle))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    for ws, names in sorted(result.collisions.items()):
        print(f"\nWARNING: {', '.join(sorted(names))} all reach the same workspace ({ws}); "
              "headroom() would count its allowance once per target, so the group could "
              "overspend. Configure only one target for it.")
    if result.ok:
        print("\nevery target checked out")
    else:
        print("\nsee above: some target needs attention")


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
    s.add_argument("--non-preemptible", action="store_true",
                   help="other jobs may never stop this one")
    s.add_argument("--grace", help="time to save a checkpoint when stopped (default 120s)")
    s.add_argument("--retries", type=int, default=0)
    s.add_argument("--name", default="")
    s.add_argument("--note", default="", help="why this job matters")
    s.add_argument("--tag", action="append", default=[])
    s.add_argument("--by", default=None, help="who is submitting (default: $USER)")
    s.add_argument("--cwd", default=None)
    s.add_argument("--no-env", action="store_true", help="don't pass your environment")
    s.add_argument("--on", dest="target", default="local",
                   help="run on this cloud target instead of the local GPU (e.g. modal), or on "
                        "one account of a group of them, picked at submit; lands awaiting "
                        "approval instead of running right away")
    s.add_argument("--gpu", help="cloud only: GPU type, e.g. H100 or H100:4")
    s.add_argument("--data", action="append", default=[],
                   help="cloud only: not wired up yet (rejected) — a cloud job's input data "
                        "has to arrive with the provider work, not through pasar submit")
    s.add_argument("--env", dest="env_keys", action="append", default=[], metavar="KEY",
                   help="cloud only: pass this environment variable through by name "
                        "(repeatable); local jobs already capture the whole environment")
    s.add_argument("--max-cost", type=float,
                   help="cloud only: dollars one attempt may spend. Refuses the submit if the "
                        "estimate already exceeds it, and otherwise pauses the job once it has "
                        "spent that much — which is sooner than --time x the target's timeout "
                        "factor, so a cap buys fewer hours as well as fewer dollars")
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

    pl = add("pull", "fetch a finished cloud job's results to local disk, then delete the remote copy")
    pl.add_argument("id", type=int)
    pl.add_argument("--to", help="destination directory (default: the daemon's pull_dir/<id>/)")
    pl.add_argument("--keep", action="store_true", help="leave the remote copy in place instead of deleting it once verified")

    add("status", "machine and memory pool status")
    c = add("cloud", "cloud targets: budgets, spend, rates and jobs awaiting approval")
    csub = c.add_subparsers(dest="cloud_cmd")
    chk = csub.add_parser("check", help="verify every configured target's profile, workspace "
                          "and credit; exits non-zero if any is unusable or two share a "
                          "workspace. Works without a daemon running")
    chk.add_argument("--json", action="store_true", help="machine-readable output")
    g = sub.add_parser("guide", help="print the agent guide (works without a daemon running)")
    g.add_argument("topic", nargs="?", choices=sorted(TOPICS),
                   help="print one topic's guide instead, e.g. `cloud` before any cloud work")
    return p


def run(args, client: httpx.Client) -> int:
    if args.cmd == "guide":
        sys.stdout.write(load_guide(args.topic))
        return 0
    if args.cmd == "cloud" and getattr(args, "cloud_cmd", None) == "check":
        # Never talks to pasard: it reads pasar's own config file directly, so it still works as
        # a health check when pasard is down.
        result = check_targets(sorted(load_config().clouds.values(), key=lambda t: t.group))
        if args.json:
            print(json.dumps({"ok": result.ok, "collisions": result.collisions,
                              "targets": [asdict(t) for t in result.targets]}, indent=2))
        else:
            print_cloud_check(result)
        return 0 if result.ok else EX_CLOUD_CHECK
    out = (lambda obj: print(json.dumps(obj, indent=2))) if args.json else None
    if args.cmd == "submit":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            raise SystemExit(EX_USAGE)
        body = {
            "command": command[0] if len(command) == 1 else shlex.join(command),
            "time": args.time, "cwd": os.path.abspath(args.cwd or os.getcwd()),
            "mem": args.mem, "bid": args.bid, "preempt": args.preempt, "preemptible": not args.non_preemptible,
            "grace": args.grace, "retries": args.retries, "name": args.name, "note": args.note,
            "tags": args.tag, "submitter": args.by or getpass.getuser(),
            "env": None if args.no_env else dict(os.environ),
            "target": args.target, "gpu": args.gpu, "env_keys": args.env_keys,
            "data": args.data, "max_cost": args.max_cost,
        }
        job = call(client, "POST", "/api/jobs", json=body)
        if out:
            out(job)
        elif job.get("cloud"):
            c = job["cloud"]
            cap_note = _cap_note(c)
            print(f"submitted #{job['id']} {job['name']} ({_state(job)}) on {c['target']}")
            print(f"  estimated {_money(c['estimated_cost'])}, "
                  f"capped at {_money(c['max_cost'])}{cap_note} for this run")
            if c.get("full_seconds") and (c.get("approved_seconds") or 0) < c["full_seconds"]:
                print(f"  it will be paused after {fmt_duration(c['approved_seconds'])}"
                      f" instead of {fmt_duration(c['full_seconds'])}, to stay under that cap")
            print(f"  a person has to approve it before it launches: "
                  f"POST /api/jobs/{job['id']}/approve (no web UI for it yet)")
        else:
            print(f"submitted #{job['id']} {job['name']} ({_state(job)})")
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
    elif args.cmd == "pull":
        body = {"to": os.path.abspath(args.to) if args.to else None, "keep": args.keep}
        # No read timeout: a multi-GiB checkpoint can take a long time to fetch, and the server
        # sends nothing back until the whole thing (download, verify, maybe delete) is done.
        result = call(client, "POST", f"/api/jobs/{args.id}/pull", json=body,
                      timeout=httpx.Timeout(30, read=None))
        if out:
            out(result)
        elif result["files"] == 0:
            print(f"#{args.id}: nothing on the volume to pull")
        else:
            where = "deleted the remote copy" if result["deleted"] else "kept the remote copy"
            print(f"#{args.id}: pulled {result['files']} file(s), {fmt_gib(result['bytes'])} "
                  f"to {result['dest']} ({where})")
            if result.get("note"):
                print(result["note"])
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
    elif args.cmd == "cloud":
        body = call(client, "GET", "/api/cloud")
        out(body) if out else print_cloud(body)
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
