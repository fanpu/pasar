"""The scheduler daemon: owns the job store and turns decisions into launches and stops."""

import json
import logging
import os
import shutil
import time
from collections import deque
from dataclasses import replace
from pathlib import Path

from pasar import gitinfo
from pasar.config import Config
from pasar.db import Store
from pasar.diagnose import diagnose
from pasar.events import read_new, restart_cost, wasted_work
from pasar.executor.base import Executor, LaunchError, LaunchRequest, UnitState
from pasar.memory import pool_size, reservation
from pasar.models import TERMINAL, Attempt, EndKind, Job, JobSpec, State, default_name
from pasar.scheduler import Decision, Queued, Running, decide
from pasar.units import fmt_duration, fmt_gib
from pasar.watchdog import MachineSample, Watchdog

log = logging.getLogger(__name__)

UNSET = object()
LOG_TAIL_LINES = 50
ACTIVE = (State.RUNNING, State.STOPPING)
_BASE_ENV_KEYS = ("PATH", "HOME", "USER", "LANG", "SHELL")
USAGE_HISTORY = 1800  # samples per job (an hour at the default 2s tick), current attempt only


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
                 clock=time.time, metrics=None):
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
        self.oom_killed: set[int] = set()
        self.decision = Decision()
        self.version = 0

    # ---- helpers
    def job_dir(self, job_id: int) -> Path:
        return self.data_dir / "jobs" / str(job_id)

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

    # ---- commands
    def submit(self, spec: JobSpec) -> Job:
        if not Path(spec.cwd).is_dir():
            raise ValueError(f"working directory does not exist: {spec.cwd}")
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

    def cancel(self, job_id: int) -> Job:
        job = self.job(job_id)
        if job.state in TERMINAL:
            raise Conflict(f"job {job_id} is already {job.state}")
        if job.state == State.QUEUED:
            self.store.update_job(job_id, state=State.CANCELLED, reason="cancelled",
                                  summary="cancelled before it started")
        else:
            self.store.update_job(job_id, state=State.STOPPING, stop_requested="cancel")
            if job.state == State.RUNNING:
                self.executor.stop(self.store.current_attempt(job_id).unit)
        self.changed()
        return self.job(job_id)

    def set_bid(self, job_id: int, bid: int) -> Job:
        if bid < 0:
            raise ValueError("bid must not be negative")
        job = self.job(job_id)
        if job.state in TERMINAL:
            raise Conflict(f"job {job_id} is already {job.state}")
        self.store.update_job(job_id, bid=bid)
        self.changed()
        return self.job(job_id)

    def restart(self, job_id: int, *, mem_request=UNSET, est_runtime: int | None = None,
                bid: int | None = None, retries: int | None = None) -> Job:
        job = self.job(job_id)
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
        self._poll()
        self._measure(now)
        self._enforce(now)
        self._schedule(now)
        self.changed()

    def _sample_machine(self) -> None:
        total, available = self.probe.meminfo()
        self.sample = MachineSample(total, available, self.probe.psi_some_avg10())
        self.pool = pool_size(total, self.cfg)
        self.gpu = self.probe.gpu_process_memory()

    def _poll(self) -> None:
        self.units = {}
        for job in self.store.list_jobs(ACTIVE):
            try:
                att = self.store.current_attempt(job.id)
                self._read_events(job, att)
                st = self.executor.status(att.unit)
                if st is None:
                    self._finish(job.id, None, lost=not job.stop_requested)
                elif st.exited:
                    self._finish(job.id, st)
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
        elif st.exit_code == 0 and st.signal is None:
            kind, reason, summary = EndKind.COMPLETED, None, ""
        else:
            d = diagnose(exit_code=st.exit_code, signal=st.signal, result=st.result, log_tail=tail,
                         xid_errors=self.probe.xid_errors(att.start_time, now))
            kind, reason, summary = EndKind.FAILED, d.reason, d.summary
        wasted = None
        if kind in (EndKind.PREEMPTED, EndKind.FAILED):
            ckpt = self.store.last_event(job_id, "checkpoint", attempt=att.n)
            wasted = wasted_work(now, att.start_time, ckpt["ts"] if ckpt else None,
                                 self.store.has_events(job_id))
        self.store.update_attempt(
            job_id, att.n, end_time=now, end_kind=kind,
            exit_code=st.exit_code if st else None, signal=st.signal if st else None,
            reason=reason, summary=summary, log_tail=tail, wasted_work=wasted,
        )
        if st is not None:
            self.executor.cleanup(att.unit)
        self.oom_killed.discard(job_id)
        self.usage.pop(job_id, None)
        if kind == EndKind.COMPLETED:
            state, retries_used = State.COMPLETED, job.retries_used
        elif kind == EndKind.CANCELLED:
            state, retries_used = State.CANCELLED, job.retries_used
        elif kind == EndKind.PREEMPTED:
            state, retries_used = State.QUEUED, job.retries_used
        else:
            state, retries_used = self._retry_state(job)
        self.store.update_job(job_id, state=state, reason=reason, summary=summary,
                              stop_requested=None, retries_used=retries_used)
        if self.metrics is not None:
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

    def snapshot(self) -> tuple[list[Queued], list[Running]]:
        """The scheduler's view of the world: queued needs and running charges."""
        queued = [Queued(j.id, j.bid, j.queue_time, self.limit(j), j.spec.preemptible)
                  for j in self.store.list_jobs([State.QUEUED])]
        running = []
        for j in self.store.list_jobs(ACTIVE):
            att = self.store.current_attempt(j.id)
            charge = max(self.limit(j), self.usage.get(j.id, 0))
            # A job the watchdog already killed this tick is as good as gone: treat it like a
            # stopping job (not a preemption candidate, its charge counts as "incoming" free
            # space) rather than let the scheduler pick an already-dead job as a victim.
            stopping = j.state == State.STOPPING or j.id in self.oom_killed
            running.append(Running(j.id, j.bid, att.start_time, charge, j.spec.preemptible,
                                   stopping=stopping))
        return queued, running

    def free(self, running: list[Running]) -> int:
        return self.pool - self.external - sum(r.charge for r in running)

    def _schedule(self, now: float) -> None:
        queued, running = self.snapshot()
        self.decision = decide(queued, running, self.free(running))
        for job_id in self.decision.preempt:
            self.store.update_job(job_id, state=State.STOPPING, stop_requested="preempt")
            self.executor.stop(self.store.current_attempt(job_id).unit)
        for job_id in self.decision.launch:
            self._launch(self.job(job_id), now)

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

    # ---- startup and upkeep
    def reconcile(self) -> None:
        """At startup, note units no job owns. Vanished units are settled by the next tick."""
        live = {self.store.current_attempt(j.id).unit for j in self.store.list_jobs(ACTIVE)}
        for unit in self.executor.list_units():
            if unit not in live:
                self.store.add_machine_event(self.clock(), "stray_unit",
                                             f"{unit} is running but no job owns it")

    def housekeep(self) -> None:
        """Delete files of old finished jobs by age, then by total size. Database rows stay."""
        now = self.clock()
        finished = []
        for job in self.store.list_jobs(TERMINAL):
            att = self.store.current_attempt(job.id)
            finished.append(((att.end_time if att and att.end_time else job.queue_time), job.id))
        finished.sort()
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
