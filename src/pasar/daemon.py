"""The scheduler daemon: owns the job store and turns decisions into launches and stops."""

import base64
import json
import logging
import os
import shutil
import tempfile
import time
from collections import deque
from dataclasses import replace
from pathlib import Path

from pasar import gitinfo
from pasar.cloud.base import parse_gpu
from pasar.cloud.bundle import Bundle, BundleError, EnvSpec, build_bundle
from pasar.cloud.cost import Ledger, estimate, hourly_rate
from pasar.cloud.executor import CloudExecutor
from pasar.cloud.lane import CloudDecision, CloudQueued, decide_cloud
from pasar.config import CloudTarget, Config
from pasar.db import Store
from pasar.diagnose import diagnose
from pasar.eta import remaining_time
from pasar.events import read_new, restart_cost, wasted_work
from pasar.executor.base import CloudLaunchInfo, Executor, LaunchError, LaunchRequest, UnitState
from pasar.memory import pool_size, reservation
from pasar.models import TERMINAL, Attempt, EndKind, Job, JobSpec, State, default_name
from pasar.projection import MIN_REMAINING
from pasar.scheduler import Decision, Queued, Running, decide
from pasar.units import fmt_duration, fmt_gib
from pasar.watchdog import MachineSample, Watchdog

log = logging.getLogger(__name__)

UNSET = object()
LOG_TAIL_LINES = 50
ACTIVE = (State.RUNNING, State.STOPPING)
LOCAL = "local"
_BASE_ENV_KEYS = ("PATH", "HOME", "USER", "LANG", "SHELL")
PAUSE_LIMIT = 5  # attempts a cloud job may end `paused` before it has to be submitted afresh
PRICE_TOLERANCE = 1e-6  # dollars of float noise, which is not a price rise
USAGE_HISTORY = 1800  # samples per job (an hour at the default 2s tick), current attempt only
RECENT = 86400  # matches api.RECENT: usage_history for jobs finished longer ago than this is dropped


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def _tail(path: Path, lines: int) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size - 64 * 1024))
        text = f.read().decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


def _separator(n: int, now: float, prev: Attempt | None) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    why = f" · after {prev.end_kind}" if prev and prev.end_kind else ""
    return f"──── attempt {n} · {stamp}{why} ────\n"


class Daemon:
    def __init__(self, cfg: Config, store: Store, executor: Executor, probe, data_dir: Path,
                 clock=time.time, metrics=None, providers: dict | None = None):
        self.cfg = cfg
        self.store = store
        self.executor = executor
        self.probe = probe
        self.data_dir = Path(data_dir)
        self.clock = clock
        self.metrics = metrics
        self.watchdog = Watchdog(cfg)
        self.sample = MachineSample(0, 0, 0.0)
        self.pool = 0
        self.gpu: dict[int, int] = {}
        self.external = 0
        self.usage: dict[int, int] = {}
        self.usage_history: dict[int, deque[tuple[float, int]]] = {}
        self.units: dict[int, UnitState] = {}
        self.cloud_units: dict[int, UnitState] = {}
        self.oom_killed: set[int] = set()
        self.decision = Decision()
        self.cloud_decisions: dict[str, CloudDecision] = {}
        self.ledger = Ledger(store, clock)
        self.executors: dict[str, Executor] = {LOCAL: executor}
        for name, target in cfg.clouds.items():
            provider = (providers or {}).get(name)
            if provider is None:
                log.warning("cloud target %s has no provider: jobs cannot be submitted to it", name)
                continue
            self.executors[name] = CloudExecutor(provider, target, store, clock, self.job_dir)
        self.version = 0

    # ---- helpers
    def job_dir(self, job_id: int) -> Path:
        return self.data_dir / "jobs" / str(job_id)

    def _executor(self, job: Job) -> Executor:
        ex = self.executors.get(job.spec.target)
        if ex is None:
            raise Conflict(f"job {job.id} runs on {job.spec.target}, which is not configured")
        return ex

    def _cloud_executors(self) -> list[tuple[str, CloudExecutor]]:
        return [(n, e) for n, e in self.executors.items() if isinstance(e, CloudExecutor)]

    @staticmethod
    def _is_cloud(job: Job) -> bool:
        return job.spec.target != LOCAL

    def job(self, job_id: int) -> Job:
        job = self.store.get_job(job_id)
        if job is None:
            raise NotFound(f"no job {job_id}")
        return job

    def limit(self, job: Job) -> int:
        """What a job holds while running. Whole-GPU jobs take whatever isn't used outside
        pasar (not the raw pool) so a foreign GPU process doesn't block them forever."""
        if job.spec.mem_request is None:
            return max(0, self.pool - self.external)
        return reservation(job.spec.mem_request, self.pool, self.cfg)

    def _retry_state(self, job: Job) -> tuple[State, int]:
        """Retry if attempts remain, otherwise the job's terminal state."""
        if job.retries_used < job.spec.retries:
            return State.QUEUED, job.retries_used + 1
        return State.FAILED, job.retries_used

    def changed(self) -> None:
        self.version += 1

    def _validate_spec(self, spec: JobSpec) -> None:
        """Field-level checks shared by submit and restart. Needs self.pool sampled first for
        the memory-request check."""
        if not spec.command.strip():
            raise ValueError("command is empty")
        if spec.est_runtime <= 0:
            raise ValueError("estimated runtime must be positive")
        if spec.bid < 0 or spec.grace < 0 or spec.retries < 0:
            raise ValueError("bid, grace and retries must not be negative")
        if spec.mem_request is not None:
            if spec.mem_request <= 0:
                raise ValueError("memory request must be positive")
            if reservation(spec.mem_request, self.pool, self.cfg) > self.pool:
                raise ValueError(f"requests more than the whole pool ({fmt_gib(self.pool)}); "
                                 "omit the memory request to take the whole GPU")

    # ---- cloud helpers
    def _rates(self, target: CloudTarget) -> dict:
        """Live prices, with anything the operator pinned in the config on top."""
        ex = self.executors.get(target.name)
        rates: dict = {}
        if ex is not None:
            try:
                rates = dict(ex.provider.rates())
            except Exception:
                log.exception("%s could not report its rates", target.name)
        rates.update(target.rates)
        return rates

    def _cost(self, target: CloudTarget, gpu: str, seconds: float) -> float:
        kind, count = parse_gpu(gpu)
        return estimate(hourly_rate(self._rates(target), kind, count), seconds)

    @staticmethod
    def _approved_seconds(spec: JobSpec, target: CloudTarget) -> int:
        """The run time one approval buys: the estimate with room to be wrong, but never more
        than the target allows in one attempt."""
        return max(1, min(int(spec.est_runtime * target.timeout_factor), target.max_runtime))

    def _write_bundle(self, d: Path, bundle: Bundle) -> None:
        (d / "bundle.json").write_text(json.dumps({
            "root": bundle.root, "rel_cwd": bundle.rel_cwd, "size": bundle.size,
            "key": bundle.env.key,
            "files": {n: base64.b64encode(b).decode() for n, b in bundle.env.files.items()},
        }))

    def _read_bundle(self, d: Path) -> Bundle:
        """The snapshot taken at submit, which every attempt of this job reuses: a cloud job runs
        the code as it was submitted, however long it waited for approval."""
        meta = json.loads((d / "bundle.json").read_text())
        files = {n: base64.b64decode(v) for n, v in meta["files"].items()}
        return Bundle(d / "bundle.tar", meta["root"], meta["rel_cwd"],
                      EnvSpec(files, meta["key"]), meta["size"])

    # ---- commands
    def submit(self, spec: JobSpec) -> Job:
        if not Path(spec.cwd).is_dir():
            raise ValueError(f"working directory does not exist: {spec.cwd}")
        if spec.target != LOCAL:
            return self._submit_cloud(spec)
        if not self.pool:
            self._sample_machine()
        self._validate_spec(spec)
        now = self.clock()
        commit, diff = gitinfo.capture(spec.cwd)
        stored = replace(spec, name=spec.name or default_name(spec.command), env=None)
        job_id = self.store.insert_job(stored, spec.bid, now, commit)
        d = self.job_dir(job_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "spec.json").write_text(stored.to_json())
        _write_private(d / "env.json", json.dumps(spec.env or {}))
        if diff:
            (d / "git.diff").write_text(diff)
        self.changed()
        return self.job(job_id)

    def _submit_cloud(self, spec: JobSpec) -> Job:
        """A cloud job is checked, priced and packaged before it exists, so a mistake costs
        nothing, and lands awaiting a person's approval rather than in the queue."""
        target = self.cfg.clouds.get(spec.target)
        if target is None:
            known = ", ".join(sorted(self.cfg.clouds)) or "none configured"
            raise ValueError(f"unknown cloud target {spec.target!r} (known targets: {known})")
        if spec.target not in self.executors:
            # Configured but unusable: saying "unknown" here would send someone to fix a config
            # file that is already right.
            raise ValueError(f"cloud target {spec.target!r} has no provider on this pasard, so "
                             "nothing can be submitted to it")
        if not spec.gpu:
            raise ValueError("a cloud job needs a gpu, e.g. --gpu H100 or --gpu H100:4")
        parse_gpu(spec.gpu)
        if spec.mem_request is not None:
            raise ValueError("--mem sizes a slice of the local GPU; a cloud job rents whole "
                             "GPUs, so use --gpu instead")
        if spec.retries:
            raise ValueError("--retries is not allowed for cloud jobs: every paid run is a "
                             "new submit, priced and approved again")
        if spec.preempt:
            raise ValueError("--preempt is for the local GPU; cloud jobs never stop one another")
        self._validate_spec(spec)
        try:
            self._cost(target, spec.gpu, spec.est_runtime)
        except KeyError as e:
            raise ValueError(f"{spec.target} has no price for --gpu {spec.gpu}: {e}") from None
        now = self.clock()
        commit, diff = gitinfo.capture(spec.cwd)
        tmp = Path(tempfile.mkdtemp(dir=self.data_dir, prefix="bundle-"))
        try:
            try:
                bundle = build_bundle(spec.cwd, tmp / "bundle.tar", target.bundle_max)
            except BundleError as e:
                # A job that cannot be packaged is the submitter's mistake and nothing exists
                # yet; as a ValueError it reaches them as a 4xx instead of a server error.
                raise ValueError(str(e)) from None
            stored = replace(spec, name=spec.name or default_name(spec.command), env=None)
            job_id = self.store.insert_job(stored, spec.bid, now, commit)
            # Before anything else can fail: insert_job writes `queued`, and a cloud job sitting
            # in `queued` is one the lane may pay for without anybody having approved it.
            self.store.update_job(job_id, state=State.AWAITING)
            d = self.job_dir(job_id)
            d.mkdir(parents=True, exist_ok=True)
            (tmp / "bundle.tar").replace(d / "bundle.tar")
            self._write_bundle(d, bundle)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        (d / "spec.json").write_text(stored.to_json())
        _write_private(d / "env.json", json.dumps(spec.env or {}))
        if diff:
            (d / "git.diff").write_text(diff)
        self.changed()
        return self.job(job_id)

    def approve(self, job_id: int) -> Job:
        """Let one attempt run, at today's price. Approval is per attempt: a paused or reclaimed
        job comes back here rather than straight to the queue. The row records the price only —
        the estimate and the ceiling the launch is held to — because pasard has no
        authentication and there is nobody to name as the approver."""
        job = self.job(job_id)
        if job.state != State.AWAITING:
            raise Conflict(f"job {job_id} is {job.state}, not awaiting approval")
        target = self.cfg.clouds.get(job.spec.target)
        if target is None:
            raise Conflict(f"job {job_id} runs on {job.spec.target}, which is not configured")
        now = self.clock()
        est = top = None
        try:
            est = self._cost(target, job.spec.gpu, job.spec.est_runtime)
            top = self._cost(target, job.spec.gpu, self._approved_seconds(job.spec, target))
        except (KeyError, ValueError):
            log.exception("could not price job %s at approval", job_id)
        self.store.add_approval(job_id, len(self.store.attempts(job_id)) + 1, now, est, top)
        self.store.update_job(job_id, state=State.QUEUED, queue_time=now, reason=None, summary="")
        self.changed()
        return self.job(job_id)

    def reject(self, job_id: int) -> Job:
        job = self.job(job_id)
        if job.state != State.AWAITING:
            raise Conflict(f"job {job_id} is {job.state}, not awaiting approval")
        self.store.update_job(job_id, state=State.CANCELLED, reason="rejected",
                              summary="rejected instead of approved")
        self.changed()
        return self.job(job_id)

    def cancel(self, job_id: int) -> Job:
        job = self.job(job_id)
        if job.state in TERMINAL:
            raise Conflict(f"job {job_id} is already {job.state}")
        if job.state in (State.QUEUED, State.AWAITING):
            self.store.update_job(job_id, state=State.CANCELLED, reason="cancelled",
                                  summary="cancelled before it started")
        else:
            self.store.update_job(job_id, state=State.STOPPING, stop_requested="cancel")
            if job.state == State.RUNNING:
                self._executor(job).stop(self.store.current_attempt(job_id).unit)
        self.changed()
        return self.job(job_id)

    def set_bid(self, job_id: int, bid: int | None = None, preempt: bool | None = None) -> Job:
        """Change a job's bid and/or whether it may preempt lower-bid jobs."""
        if bid is not None and bid < 0:
            raise ValueError("bid must not be negative")
        job = self.job(job_id)
        if job.state in TERMINAL:
            raise Conflict(f"job {job_id} is already {job.state}")
        values: dict = {}
        if bid is not None:
            values["bid"] = bid
        if preempt is not None:
            values["spec"] = replace(job.spec, preempt=preempt)
        if values:
            self.store.update_job(job_id, **values)
        self.changed()
        return self.job(job_id)

    def restart(self, job_id: int, *, mem_request=UNSET, est_runtime: int | None = None,
                bid: int | None = None, retries: int | None = None,
                preempt: bool | None = None) -> Job:
        job = self.job(job_id)
        if self._is_cloud(job):
            # A restart would pay for another run with whatever is in the working tree now, and
            # without anyone looking at the price again.
            raise Conflict(
                f"job {job_id} ran on {job.spec.target} and cloud jobs are never restarted; "
                f"submit it again: pasar submit --on {job.spec.target} --gpu {job.spec.gpu} "
                f"--time {job.spec.est_runtime} -- {job.spec.command}")
        if job.state not in TERMINAL:
            raise Conflict(f"job {job_id} is {job.state}; only finished jobs can be restarted")
        if not self.job_dir(job_id).is_dir():
            raise Conflict(f"job {job_id}'s files are gone (housekeeping removed them); "
                           "resubmit it instead")
        new_bid = job.bid if bid is None else bid
        if new_bid < 0:
            raise ValueError("bid must not be negative")
        spec = job.spec
        if mem_request is not UNSET:
            spec = replace(spec, mem_request=mem_request)
        if est_runtime is not None:
            spec = replace(spec, est_runtime=est_runtime)
        if retries is not None:
            spec = replace(spec, retries=retries)
        if preempt is not None:
            spec = replace(spec, preempt=preempt)
        if not self.pool:
            self._sample_machine()
        self._validate_spec(spec)
        self.store.update_job(
            job_id, spec=spec, state=State.QUEUED, bid=new_bid,
            queue_time=self.clock(), retries_used=0, reason=None, summary="", stop_requested=None,
        )
        self.changed()
        return self.job(job_id)

    # ---- the loop
    def tick(self) -> None:
        now = self.clock()
        self._sample_machine()
        self._drop_unreachable(now)
        self._expire_awaiting(now)
        for _, ex in self._cloud_executors():
            # Before the statuses are read: an attempt that ended between two ticks left its own
            # account of why on stdout, and that beats whatever the provider says.
            ex.poll_output()
        self._poll()
        self._measure(now)
        self._enforce(now)
        self._pause_overdue(now)
        self._schedule(now)
        self.changed()

    def _drop_unreachable(self, now: float) -> None:
        """Settle jobs on a target this pasard cannot reach — one removed from the config, or one
        whose provider is gone. Without this a running job stays running forever, its attempt
        never ends (so the budget stays committed to a target nothing can spend on), the poll
        raises every tick where only the log sees it, and a waiting job can never be approved
        nor expired. A running job is failed rather than quietly closed: its sandbox may well
        still be alive, nothing here can end it, and somebody has to go and look."""
        for job in self.store.list_jobs(ACTIVE):
            if job.spec.target in self.executors:
                continue
            att = self.store.current_attempt(job.id)
            summary = (f"{job.spec.target} is no longer configured on this pasard, so this "
                       "attempt cannot be followed")
            if att is not None and att.end_time is None:
                self.store.update_attempt(job.id, att.n, end_time=now, end_kind=EndKind.FAILED,
                                          reason="target_gone", summary=summary)
            self.store.add_machine_event(
                now, "target_gone",
                f"job {job.id} runs on {job.spec.target}, which is not configured here: "
                f"{att.unit if att else 'its attempt'} may still be running and billing — "
                "end it at the provider, or put the target back and restart pasard")
            self.store.update_job(job.id, state=State.FAILED, reason="target_gone",
                                  summary=summary, stop_requested=None)
            self.cloud_units.pop(job.id, None)
            self.usage.pop(job.id, None)
        for job in self.store.list_jobs([State.AWAITING, State.QUEUED]):
            if job.spec.target in self.executors:
                continue
            self.store.add_machine_event(
                now, "target_gone", f"job {job.id} is waiting for {job.spec.target}, which is "
                "not configured here; it was cancelled rather than left waiting forever")
            self.store.update_job(
                job.id, state=State.CANCELLED, reason="target_gone",
                summary=f"{job.spec.target} is no longer configured on this pasard; nothing was "
                        "spent — submit it again to a target that is")

    def _expire_awaiting(self, now: float) -> None:
        """An approval nobody gave is a job nobody wants; queue_time is when it started waiting."""
        for job in self.store.list_jobs([State.AWAITING]):
            target = self.cfg.clouds.get(job.spec.target)
            if target is None or now - job.queue_time < target.approval_ttl:
                continue
            self.store.update_job(
                job.id, state=State.CANCELLED, reason="approval_expired",
                summary=f"nobody approved it within {fmt_duration(target.approval_ttl)}")

    def _pause_overdue(self, now: float) -> None:
        """Stop a cloud attempt that has used the run time its approval bought. The wrapper
        enforces a limit of its own, but that one is the sandbox's backstop: pausing from here
        keeps the decision (and the deadline it is measured against) with the daemon."""
        for job_id in list(self.cloud_units):
            job = self.job(job_id)
            target = self.cfg.clouds.get(job.spec.target)
            if job.state != State.RUNNING or job.stop_requested or target is None:
                continue
            att = self.store.current_attempt(job_id)
            if now - att.start_time < self._approved_seconds(job.spec, target):
                continue
            self.store.update_job(job_id, state=State.STOPPING, stop_requested="pause")
            self._executor(job).stop(att.unit)

    def _sample_machine(self) -> None:
        total, available = self.probe.meminfo()
        self.sample = MachineSample(total, available, self.probe.psi_some_avg10())
        self.pool = pool_size(total, self.cfg)
        self.gpu = self.probe.gpu_process_memory()

    def _poll(self) -> None:
        # Two books, because a cloud attempt has no cgroup and no claim on the local pool:
        # `units` is what _measure, the watchdog and the pool accounting work from.
        self.units = {}
        self.cloud_units = {}
        for job in self.store.list_jobs(ACTIVE):
            try:
                att = self.store.current_attempt(job.id)
                self._read_events(job, att)
                st = self._executor(job).status(att.unit)
                if st is None:
                    self._finish(job.id, None, lost=not job.stop_requested)
                elif st.exited:
                    self._finish(job.id, st)
                elif self._is_cloud(job):
                    self.cloud_units[job.id] = st
                else:
                    self.units[job.id] = st
            except Exception:
                log.exception("poll failed for job %s", job.id)

    def _read_events(self, job: Job, att: Attempt) -> None:
        events, offset = read_new(self.job_dir(job.id) / "events.jsonl", job.events_offset, job.id)
        if offset == job.events_offset:
            return
        now = self.clock()
        for e in events:
            self.store.add_event(job.id, att.n, now, e.kind, e.step, e.payload)
            if e.kind == "resumed" and att.n > 1 and att.restart_cost is None:
                att.restart_cost = restart_cost(att.start_time, now)
                self.store.update_attempt(job.id, att.n, restart_cost=att.restart_cost)
        self.store.update_job(job.id, events_offset=offset)

    def _finish(self, job_id: int, st: UnitState | None, lost: bool = False) -> None:
        now = self.clock()
        job = self.job(job_id)
        att = self.store.current_attempt(job_id)
        tail = _tail(self.job_dir(job_id) / "output.log", LOG_TAIL_LINES)
        if job.stop_requested == "cancel":
            # A user cancel wins even over a same-window watchdog OOM kill: the user asked for
            # the job to stop, so it should end CANCELLED, not get a free requeue as a "failed"
            # attempt. Ordering below this point: oom, then preempt.
            kind, reason, summary = EndKind.CANCELLED, "cancelled", "cancelled while running"
        elif job_id in self.oom_killed:
            # Takes priority even if a preempt was also requested this tick: the job is
            # already dead from the watchdog's kill, and reporting anything else would let it
            # requeue without consuming a retry, looping on the same OOM forever.
            kind, reason = EndKind.FAILED, "oom"
            summary = (f"exceeded its {fmt_gib(self.limit(job))} limit "
                       f"(peak {fmt_gib(att.peak_mem)}) during memory pressure")
        elif job.stop_requested == "preempt":
            kind, reason, summary = EndKind.PREEMPTED, "preempted", "preempted by a higher bid"
        elif lost:
            kind, reason = EndKind.FAILED, "lost"
            summary = "the job's process disappeared (did pasard or the machine restart?)"
        elif st is not None and st.result == "success" and st.exit_code == 0 and not st.signal:
            # An attempt whose wrapper reported a clean exit has finished, even if a pause was
            # asked for in the same window: recording that as paused would send a job that
            # already succeeded back for approval and pay to run it a second time. A cancel is
            # still above this: the user asked for the job to stop.
            kind, reason, summary = EndKind.COMPLETED, None, ""
        elif job.stop_requested == "pause" or st.result == "time_limit":
            kind, reason = EndKind.PAUSED, "time_limit"
            summary = "paused at its approved run time; approve it again to carry on"
        elif st.result == "reclaimed":
            kind, reason = EndKind.PAUSED, "cloud_preempted"
            summary = "the provider took the machine back; approve it again to carry on"
        elif st.exit_code == 0 and st.signal is None:
            kind, reason, summary = EndKind.COMPLETED, None, ""
        else:
            # A cloud attempt never touched the local GPU, so this machine's Xid errors say
            # nothing about why it failed.
            xids = 0 if self._is_cloud(job) else self.probe.xid_errors(att.start_time, now)
            d = diagnose(exit_code=st.exit_code, signal=st.signal, result=st.result, log_tail=tail,
                         xid_errors=xids)
            kind, reason, summary = EndKind.FAILED, d.reason, d.summary
        wasted = None
        if kind in (EndKind.PREEMPTED, EndKind.PAUSED, EndKind.FAILED):
            ckpt = self.store.last_event(job_id, "checkpoint", attempt=att.n)
            wasted = wasted_work(now, att.start_time, ckpt["ts"] if ckpt else None,
                                 self.store.has_events(job_id))
        self.store.update_attempt(
            job_id, att.n, end_time=now, end_kind=kind,
            exit_code=st.exit_code if st else None, signal=st.signal if st else None,
            reason=reason, summary=summary, log_tail=tail, wasted_work=wasted,
        )
        if st is not None:
            # Never after a status() of None: for a cloud attempt that is already terminal, and
            # the executor has ended and forgotten the sandbox itself.
            self._executor(job).cleanup(att.unit)
        self.oom_killed.discard(job_id)
        self.usage.pop(job_id, None)
        self.cloud_units.pop(job_id, None)
        if kind == EndKind.COMPLETED:
            state, retries_used = State.COMPLETED, job.retries_used
        elif kind == EndKind.CANCELLED:
            state, retries_used = State.CANCELLED, job.retries_used
        elif kind == EndKind.PREEMPTED:
            state, retries_used = State.QUEUED, job.retries_used
        elif kind == EndKind.PAUSED:
            state, retries_used = State.AWAITING, job.retries_used
            paused = sum(1 for a in self.store.attempts(job_id) if a.end_kind == EndKind.PAUSED)
            if paused >= PAUSE_LIMIT:
                # A pause costs no retry, so nothing else bounds this: a job that never
                # checkpoints would pause, resume from the start and pause again forever, on
                # hardware billed by the second.
                state, reason = State.FAILED, "pause_limit"
                summary = (f"paused {paused} times without finishing; submit it again with a "
                           "longer --time, or make sure it checkpoints and resumes")
        else:
            state, retries_used = self._retry_state(job)
        extra = {}
        if state == State.AWAITING:
            # It starts waiting for a person now, and approval_ttl is measured from here.
            extra["queue_time"] = now
        self.store.update_job(job_id, state=state, reason=reason, summary=summary,
                              stop_requested=None, retries_used=retries_used, **extra)
        if self.metrics is not None and not self._is_cloud(job):
            # The metrics recorder summarises this machine's GPU over the attempt's window,
            # which has nothing to do with a job that ran somewhere else.
            self.metrics.record(job_id, att.n, att.start_time, now)

    def _measure(self, now: float) -> None:
        self.usage = {}
        claimed: set[int] = set()
        for job_id, st in self.units.items():
            cg = st.control_group
            pids = self.probe.cgroup_pids(cg) if cg else []
            claimed.update(pids)
            used = (self.probe.cgroup_memory(cg) if cg else 0) + sum(self.gpu.get(p, 0) for p in pids)
            self.usage[job_id] = used
            att = self.store.current_attempt(job_id)
            if used > att.peak_mem:
                self.store.update_attempt(job_id, att.n, peak_mem=used)
            self.usage_history.setdefault(job_id, deque(maxlen=USAGE_HISTORY)).append((now, used))
        self.external = sum(v for pid, v in self.gpu.items() if pid not in claimed)

    def _enforce(self, now: float) -> None:
        limits = {j.id: self.limit(j) for j in self.store.list_jobs([State.RUNNING])
                  if j.id in self.usage}
        before = self.watchdog.pressure_since
        victim = self.watchdog.check(now, self.sample, self.usage, limits)
        after = self.watchdog.pressure_since
        if before is None and after is not None:
            self.store.add_machine_event(now, "pressure", "memory pressure started")
        elif before is not None and after is None:
            self.store.add_machine_event(
                now, "pressure_end", f"memory pressure ended after {fmt_duration(now - before)}")
        if victim is not None:
            self.executor.kill(self.store.current_attempt(victim).unit)
            self.oom_killed.add(victim)
            self.store.add_machine_event(
                now, "oom_kill", f"killed #{victim}: using {fmt_gib(self.usage[victim])}, "
                                 f"limit {fmt_gib(limits[victim])}")

    def _remaining(self, job: Job, now: float) -> float:
        left, _ = remaining_time(self.store, job, self.store.attempts(job.id), now)
        return max(MIN_REMAINING, left)

    def snapshot(self) -> tuple[list[Queued], list[Running]]:
        """The scheduler's view of the world: queued needs and expected run times, running
        charges and projected ends. Local jobs only: a cloud job holds none of this pool, and
        counting it here would charge the local GPU for a machine somewhere else."""
        now = self.clock()
        queued = [Queued(j.id, j.bid, j.queue_time, self.limit(j), j.spec.preemptible,
                         j.spec.preempt, self._remaining(j, now))
                  for j in self.store.list_jobs([State.QUEUED]) if not self._is_cloud(j)]
        running = []
        for j in self.store.list_jobs(ACTIVE):
            if self._is_cloud(j):
                continue
            att = self.store.current_attempt(j.id)
            charge = max(self.limit(j), self.usage.get(j.id, 0))
            # A job the watchdog already killed this tick is as good as gone: treat it like a
            # stopping job (not a preemption candidate, its charge counts as "incoming" free
            # space) rather than let the scheduler pick an already-dead job as a victim.
            stopping = j.state == State.STOPPING or j.id in self.oom_killed
            end = now if stopping else now + self._remaining(j, now)
            running.append(Running(j.id, j.bid, att.start_time, charge, j.spec.preemptible,
                                   stopping=stopping, end=end, preempt=j.spec.preempt))
        return queued, running

    def free(self, running: list[Running]) -> int:
        return self.pool - self.external - sum(r.charge for r in running)

    def _schedule(self, now: float) -> None:
        queued, running = self.snapshot()
        self.decision = decide(queued, running, self.free(running), now)
        for job_id in self.decision.preempt:
            self.store.update_job(job_id, state=State.STOPPING, stop_requested="preempt")
            self.executor.stop(self.store.current_attempt(job_id).unit)
        for job_id in self.decision.launch:
            self._launch(self.job(job_id), now)
        self._schedule_cloud(now)

    def _schedule_cloud(self, now: float) -> None:
        """One lane per target: bid order, the concurrency cap, then the budget."""
        self.cloud_decisions = {}
        for name, ex in self._cloud_executors():
            target = self.cfg.clouds.get(name)
            if target is None:
                continue
            queued = []
            for job in self.store.list_jobs([State.QUEUED]):
                if job.spec.target != name:
                    continue
                try:
                    price = self._cost(target, job.spec.gpu, self._approved_seconds(job.spec, target))
                except (KeyError, ValueError):
                    log.warning("job %s has no price on %s; leaving it queued", job.id, name)
                    continue
                queued.append(CloudQueued(job.id, job.bid, job.queue_time, price))
            running = sum(1 for j in self.store.list_jobs(ACTIVE) if j.spec.target == name)
            # settled_* and committed, never spent_*: the first pair excludes open attempts,
            # which committed() is already pricing, so each attempt is counted exactly once.
            decision = decide_cloud(queued, running, target,
                                    spent_day=self.ledger.settled_day(name),
                                    spent_month=self.ledger.settled_month(name),
                                    committed=self.ledger.committed(name))
            self.cloud_decisions[name] = decision
            for job_id in decision.launch:
                self._launch_cloud(self.job(job_id), target, ex, now)

    def _fail_launch(self, job: Job, n: int, unit: str, now: float, message: str) -> None:
        self.store.insert_attempt(Attempt(job.id, n, unit, now, end_time=now,
                                          end_kind=EndKind.FAILED, reason="launch_error",
                                          summary=message))
        state, retries_used = self._retry_state(job)
        self.store.update_job(job.id, state=state, reason="launch_error", summary=message,
                              retries_used=retries_used)

    def _launch(self, job: Job, now: float) -> None:
        prior = self.store.attempts(job.id)
        n = len(prior) + 1
        unit = f"pasar-job-{job.id}-{n}"
        d = self.job_dir(job.id)
        # The cgroup cap must be stable regardless of transient external GPU usage: unlike
        # limit() (used for scheduling and the watchdog), a whole-GPU job's MemoryMax is the
        # full pool, not pool - external, or it would shrink/grow as foreign processes come
        # and go and could even go to zero.
        mem_max = reservation(job.spec.mem_request, self.pool, self.cfg)
        try:
            base = {k: os.environ[k] for k in _BASE_ENV_KEYS if k in os.environ}
            # Drop any PASAR_* keys the submitter's own environment happened to carry (e.g. a
            # stale PASAR_MEM_LIMIT_BYTES) before overlaying the fresh ones we set below.
            stored = {k: v for k, v in json.loads((d / "env.json").read_text()).items()
                     if not k.startswith("PASAR_")}
            env = {**base, **stored}
            env.update(
                PASAR_JOB_ID=str(job.id), PASAR_ATTEMPT=str(n),
                PASAR_RESUMING="1" if n > 1 else "0", PASAR_EVENTS=str(d / "events.jsonl"),
                PASAR_JOB_DIR=str(d), PASAR_GRACE_SECONDS=str(job.spec.grace),
            )
            if job.spec.mem_request is not None:
                env["PASAR_MEM_LIMIT_BYTES"] = str(mem_max)
            _write_private(d / "launch.json",
                           json.dumps({"command": job.spec.command, "cwd": job.spec.cwd, "env": env}))
            with (d / "output.log").open("a") as f:
                f.write(_separator(n, now, prior[-1] if prior else None))
        except (OSError, ValueError) as e:
            self._fail_launch(job, n, unit, now, str(e))
            return
        if not Path(job.spec.cwd).is_dir():
            self._fail_launch(job, n, unit, now,
                              f"working directory no longer exists: {job.spec.cwd}")
            return
        try:
            self.executor.launch(LaunchRequest(unit, str(d), str(d / "output.log"), mem_max,
                                               job.spec.grace))
        except LaunchError as e:
            self._fail_launch(job, n, unit, now, str(e))
            return
        self.store.insert_attempt(Attempt(job.id, n, unit, now))
        self.usage_history[job.id] = deque(maxlen=USAGE_HISTORY)
        self.store.update_job(job.id, state=State.RUNNING, reason=None, summary="")

    def _cloud_env(self, d: Path, job: Job, target: CloudTarget, n: int) -> dict[str, str]:
        """What the container gets. The submitter's environment is not shipped: only the
        variables the target allows through and the ones the job asked for by name."""
        allowed = set(target.env_passthrough) | set(job.spec.env_keys)
        stored = json.loads((d / "env.json").read_text())
        env = {k: v for k, v in stored.items() if k in allowed and not k.startswith("PASAR_")}
        for k in target.env_passthrough:
            if k not in env and k in os.environ:
                env[k] = os.environ[k]
        env.update(PASAR_JOB_ID=str(job.id), PASAR_ATTEMPT=str(n),
                   PASAR_RESUMING="1" if n > 1 else "0",
                   PASAR_GRACE_SECONDS=str(job.spec.grace))
        return env

    def _over_the_approval(self, job: Job, n: int, price: float, now: float) -> bool:
        """Whether this attempt now costs more than the approval for it allowed. The launch
        prices from live rates, and a rate that moved between the two is a price nobody agreed
        to: send the job back for approval rather than spend it."""
        ceiling = next((r["max_cost"] for r in self.store.approvals(job.id)
                        if r["attempt"] == n), None)
        if ceiling is None or price <= ceiling + PRICE_TOLERANCE:
            return False
        self.store.add_machine_event(
            now, "price_rise", f"job {job.id} was approved at up to ${ceiling:.2f} for this run "
            f"but now costs ${price:.2f}; it is waiting for approval again")
        self.store.update_job(
            job.id, state=State.AWAITING, queue_time=now, reason="price_rose",
            summary=f"the price rose to ${price:.2f}, above the ${ceiling:.2f} approved for this "
                    "run; approve it again to run at the new price")
        return True

    def _launch_cloud(self, job: Job, target: CloudTarget, ex: CloudExecutor, now: float) -> None:
        prior = self.store.attempts(job.id)
        n = len(prior) + 1
        d = self.job_dir(job.id)
        pending = f"cloud:{target.name}:pending"
        try:
            bundle = self._read_bundle(d)
            env = self._cloud_env(d, job, target, n)
            price = self._cost(target, job.spec.gpu, self._approved_seconds(job.spec, target))
        except (OSError, ValueError, KeyError) as e:
            self._fail_launch(job, n, pending, now, str(e))
            return
        if self._over_the_approval(job, n, price, now):
            return
        try:
            _write_private(d / "launch.json", json.dumps(
                {"command": job.spec.command, "cwd": job.spec.cwd, "env": env}))
            with (d / "output.log").open("a") as f:
                f.write(_separator(n, now, prior[-1] if prior else None))
        except (OSError, ValueError) as e:
            self._fail_launch(job, n, pending, now, str(e))
            return
        try:
            # The limit handed to the wrapper is the target's ceiling, not the approved run
            # time: the daemon stops the attempt at the time that was approved (_pause_overdue),
            # and the wrapper's own pause is only the backstop for a daemon that isn't there.
            ex.launch(LaunchRequest(pending, str(d), str(d / "output.log"), None, job.spec.grace,
                                    cloud=CloudLaunchInfo(job_id=job.id, attempt=n, bundle=bundle,
                                                          gpu=job.spec.gpu, env=env,
                                                          limit=target.max_runtime)))
        except LaunchError as e:
            self._fail_launch(job, n, pending, now, str(e))
            return
        unit = ex.unit_of(job.id, n)
        try:
            if unit is None:
                raise LaunchError("the attempt started but the executor lost track of it")
            self.ledger.record(target.name, job.id, n, price)
            self.store.insert_attempt(Attempt(job.id, n, unit, now))
        except Exception as e:
            # Something is running and nothing would own it: end it rather than leave a sandbox
            # billing for a job that has no record of it.
            log.exception("could not record job %s's cloud attempt %s", job.id, n)
            if unit is not None:
                ex.cleanup(unit)
            self.store.update_job(job.id, state=State.FAILED, reason="launch_error",
                                  summary=f"the attempt could not be recorded: {e}")
            return
        self.store.update_job(job.id, state=State.RUNNING, reason=None, summary="")

    # ---- startup and upkeep
    def reconcile(self) -> None:
        """At startup, pick every running cloud attempt back up and note units no job owns.
        Vanished units are settled by the next tick."""
        now = self.clock()
        live: dict[str, set[str]] = {}
        for job in self.store.list_jobs(ACTIVE):
            att = self.store.current_attempt(job.id)
            if att is None:
                continue
            live.setdefault(job.spec.target, set()).add(att.unit)
            ex = self.executors.get(job.spec.target)
            # Only the current attempt's unit: without this the executor knows nothing about it
            # and the next poll would report every cloud attempt as vanished.
            if isinstance(ex, CloudExecutor) and not ex.adopt(job.id, att.unit):
                # Nothing can follow this attempt any more: the next poll reads it as vanished
                # and settles the job as lost. The sandbox would go on billing until its own
                # timeout, and this is the last moment its unit is in hand, so end it here.
                log.warning("job %s's cloud attempt %s could not be picked back up",
                            job.id, att.unit)
                ended = ex.terminate_stray(att.unit)
                self.store.add_machine_event(
                    now, "orphan_unit", f"job {job.id}'s attempt {att.unit} could not be picked "
                    "back up; " + ("ended it" if ended else "it could not be ended"))
        for name, ex in self.executors.items():
            owned = live.get(name, set())
            for unit in ex.list_units():
                if unit in owned:
                    continue
                if isinstance(ex, CloudExecutor):
                    ended = ex.terminate_stray(unit)
                    self.store.add_machine_event(
                        now, "stray_unit", f"{unit} is running but no job owns it; "
                        + ("ended it" if ended else "it could not be ended"))
                else:
                    self.store.add_machine_event(now, "stray_unit",
                                                 f"{unit} is running but no job owns it")

    def housekeep(self) -> None:
        """Delete files of old finished jobs by age, then by total size. Database rows stay."""
        now = self.clock()
        finished = []
        for job in self.store.list_jobs(TERMINAL):
            att = self.store.current_attempt(job.id)
            finished.append(((att.end_time if att and att.end_time else job.queue_time), job.id))
        finished.sort()
        for ended, job_id in finished:
            if now - ended >= RECENT:
                self.usage_history.pop(job_id, None)
        cutoff = now - self.cfg.log_retention_days * 86400
        keep = []
        for ended, job_id in finished:
            if ended < cutoff:
                shutil.rmtree(self.job_dir(job_id), ignore_errors=True)
            else:
                keep.append(job_id)
        sizes = {j: sum(f.stat().st_size for f in self.job_dir(j).rglob("*") if f.is_file())
                 for j in keep if self.job_dir(j).exists()}
        total = sum(sizes.values())
        for job_id in keep:
            if total <= self.cfg.log_retention_size:
                break
            total -= sizes.get(job_id, 0)
            shutil.rmtree(self.job_dir(job_id), ignore_errors=True)
