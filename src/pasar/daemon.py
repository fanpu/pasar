"""The scheduler daemon: owns the job store and turns decisions into launches and stops."""

import base64
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from collections import deque
from dataclasses import replace
from pathlib import Path

from pasar import gitinfo
from pasar.cloud.base import parse_gpu
from pasar.cloud.bundle import Bundle, BundleError, EnvSpec, build_bundle, check_platform
from pasar.cloud.cost import Ledger, estimate, hourly_rate
from pasar.cloud.executor import CloudExecutor
from pasar.cloud.lane import CloudDecision, CloudQueued, decide_cloud
from pasar.cloud.pace import needs_more_time as _needs_more_time
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
PAUSE_LIMIT = 5  # times a cloud job may run out of its approved time before it must be resubmitted
PRICE_TOLERANCE = 1e-6  # dollars of float noise, which is not a price rise
CLOUD_RATE_TTL = 10  # seconds a target's price list is reused before the provider is asked again
EXTENSION_MARGIN = 1.1  # buffer added to a projected overrun so one extension does not get
                        # immediately re-flagged by ordinary noise in the next progress report
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


def _disk_usage(dest: Path) -> tuple[int, int]:
    """(file count, total bytes) actually sitting under `dest` right now. What `pull` verifies
    a download against is this, walked fresh, and not the provider's own return value — see
    `Daemon.pull`."""
    files = [p for p in dest.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


class Daemon:
    def __init__(self, cfg: Config, store: Store, executor: Executor, probe, data_dir: Path,
                 clock=time.time, metrics=None, providers: dict | None = None,
                 platform_check=check_platform):
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
        self._rates: dict[str, tuple[float, dict]] = {}
        # Injectable because it shells out to uv: tests hand in a stub rather than pay for a
        # real resolve on every cloud submit.
        self.platform_check = platform_check
        self._platform_checked: set[str] = set()
        self.executors: dict[str, Executor] = {LOCAL: executor}
        for name, target in cfg.clouds.items():
            provider = (providers or {}).get(name)
            if provider is None:
                log.warning("cloud target %s has no provider: jobs cannot be submitted to it", name)
                continue
            self.executors[name] = CloudExecutor(provider, target, store, clock, self.job_dir)
        self.version = 0
        # A job being pulled right now, guarded by this lock: a pull deletes the only copy of a
        # job's data once it has verified the local one, and a second pull racing the first would
        # see the same "still there" persist dir and could delete out from under a download that
        # has not finished yet, or double-delete an already-gone one. Held only for the duration
        # of one `pull()` call, never across a tick.
        self._pulling: set[int] = set()
        self._pull_lock = threading.Lock()

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
    def cloud_rates(self, target: CloudTarget) -> dict:
        """Live prices, with anything the operator pinned in the config on top. Public: the API
        and CLI read it too (`GET /api/cloud`), not just pricing done here.

        Memoised per target for `CLOUD_RATE_TTL` seconds, because almost everything about a cloud
        job is priced on demand rather than stored: a job view prices a capped job twice and every
        tick prices each running capped job again, so `pasar ls` over twenty of them was forty
        provider round-trips inside one two-second tick. A provider's price list moves on the
        order of days, so a few seconds of staleness costs nothing, and holding one answer for a
        few ticks also stops a momentary blip in a rate from moving a running attempt's
        enforcement deadline for exactly one tick and back.

        A failure is cached too, so a provider that is down is asked once every few seconds
        rather than once per job per tick."""
        now = self.clock()
        cached = self._rates.get(target.name)
        if cached is not None and now - cached[0] < CLOUD_RATE_TTL:
            rates = dict(cached[1])
        else:
            ex = self.executors.get(target.name)
            rates = {}
            if ex is not None:
                try:
                    rates = dict(ex.provider.rates())
                except Exception:
                    log.exception("%s could not report its rates", target.name)
            self._rates[target.name] = (now, dict(rates))
        rates.update(target.rates)
        return rates

    def _hourly(self, target: CloudTarget, gpu: str) -> float:
        """Dollars per hour for one attempt of `gpu` on `target`, at today's rates."""
        kind, count = parse_gpu(gpu)
        return hourly_rate(self.cloud_rates(target), kind, count)

    def _cost(self, target: CloudTarget, gpu: str, seconds: float) -> float:
        return estimate(self._hourly(target, gpu), seconds)

    def cloud_window(self, job: Job) -> tuple[int, int] | None:
        """(approved, full) seconds of run time for `job`'s next cloud attempt: what one approval
        actually buys once `--max-cost` is taken into account, and what it would buy without it.
        `None` for a job whose target isn't configured. Public because a dollar cap is enforced as
        a shorter run (`_approved_seconds`), and somebody who capped dollars deserves to see the
        time it costs them rather than discover it when the job pauses."""
        target = self.cfg.clouds.get(job.spec.target)
        if target is None:
            return None
        return self._approved_seconds(job.spec, target), self._window(job.spec, target)

    def cloud_estimate(self, job: Job) -> tuple[float, float] | None:
        """(estimated, max) dollars for `job`'s next cloud attempt at today's rates, or `None`
        if it cannot be priced right now (an unconfigured target, or a rate the provider or
        config is missing). Public so views can show a price before a job has ever been
        approved, and keep showing one after — `approve()` prices the same way at the moment it
        writes the approvals row, this just does it again, live, on demand."""
        target = self.cfg.clouds.get(job.spec.target)
        if target is None or not job.spec.gpu:
            return None
        try:
            est = self._cost(target, job.spec.gpu, job.spec.est_runtime)
            top = self._ceiling(target, job.spec)
        except (KeyError, ValueError):
            return None
        return est, top

    def _ceiling(self, target: CloudTarget, spec: JobSpec) -> float:
        """The most one attempt can bill: the price of the run time the target would allow it
        (`_window`), or the submitter's own `--max-cost`, whichever is lower. This is a real
        ceiling and not just a number to show, because `_approved_seconds` stops the attempt at
        the moment the cap's dollars run out — the ceiling and the deadline are the same fact,
        priced two ways."""
        top = self._cost(target, spec.gpu, self._window(spec, target))
        return top if spec.max_cost is None else min(top, spec.max_cost)

    @staticmethod
    def _window(spec: JobSpec, target: CloudTarget) -> int:
        """The run time the target itself allows one attempt: the estimate with room to be wrong,
        but never more than the target allows. `--max-cost` narrows it further, in
        `_approved_seconds`; this is the unnarrowed figure, and the one a price rise is priced
        against, so that a cap never hides a rise."""
        return max(1, min(int(spec.est_runtime * target.timeout_factor), target.max_runtime))

    def _approved_seconds(self, spec: JobSpec, target: CloudTarget) -> int:
        """The run time one approval would buy at today's price: the target's window, cut short at
        the point where the job would have spent its `--max-cost`. What views quote for a job that
        has not started yet (`cloud_window`); once an attempt is running, `_attempt_seconds` is
        the figure `_pause_overdue` enforces, because by then there is an approvals row saying what
        was actually agreed to.

        A dollar cap has to become a time bound to mean anything. Stopping the attempt is the only
        power the daemon has over a running sandbox, so `max_cost / hourly rate` is the cap: past
        that instant the job is spending money nobody approved. A job stopped here pauses and comes
        back for approval like any other pause, which is the right answer to "you hit your cap".

        Priced from live rates each time it is asked rather than frozen at approval — as is the
        cap half of `_attempt_seconds`, for the same reason: a rate that rose shortens what the cap
        buys, which is what "never spend more than $X" means. A job that cannot be priced right now
        keeps the full window rather than being stopped on a missing rate — nothing launches
        without a price (`_launch_cloud`), and a wrong stop costs the job its progress."""
        window = self._window(spec, target)
        if spec.max_cost is None:
            return window
        try:
            rate = self._hourly(target, spec.gpu)
        except (KeyError, ValueError):
            log.warning("job on %s cannot be priced; holding it to the full approved window",
                        target.name)
            return window
        return max(1, min(window, int(spec.max_cost / rate * 3600)))

    def _attempt_seconds(self, job: Job, att: Attempt, target: CloudTarget) -> float:
        """The run time this *running* attempt may still have, and so the deadline `_pause_overdue`
        stops it at: the lesser of two different promises.

        The **window** is frozen. It is this attempt's own approvals row (`max_cost / hourly_rate`,
        converted back to time), not `_approved_seconds` recomputed against today's rate, because
        it is the run time a person looked at and agreed to — it should not shrink or stretch as
        the market moves under them, and `extend` raises it by writing a new figure into that same
        row (`_extend`), which is exactly what this reads back.

        The **cap** is live. `--max-cost` is a promise about dollars, not about time: if the rate
        rises mid-run, the same cap buys fewer seconds, and enforcing the seconds it bought at
        yesterday's price would let the job bill straight past the figure its submitter set. So the
        cap is re-derived from today's rate on every tick and the attempt is held to whichever of
        the two is shorter. A rate that *falls* cannot buy back time beyond the frozen window,
        which is the asymmetry the two promises imply.

        Falls back to the full window for a job that cannot be priced right now (and to
        `_approved_seconds(job.spec, target)` for a row missing a price, which `approve` no longer
        writes but older rows may still hold): a wrong stop costs the job its progress."""
        row = next((r for r in self.store.approvals(job.id) if r["attempt"] == att.n), None)
        if row is None or row["max_cost"] is None or not row["hourly_rate"]:
            return self._approved_seconds(job.spec, target)
        window = row["max_cost"] / row["hourly_rate"] * 3600
        if job.spec.max_cost is None:
            return window
        try:
            rate = self._hourly(target, job.spec.gpu)
        except (KeyError, ValueError):
            log.warning("job %s cannot be priced; holding it to the run time its approval bought",
                        job.id)
            return window
        return max(1.0, min(window, job.spec.max_cost / rate * 3600))

    def needs_more_time(self, job: Job) -> float | None:
        """Extra seconds `job`'s own pace (see `pasar.cloud.pace.needs_more_time`) projects past
        its running attempt's approved ceiling, or `None` when there's nothing to flag — not a
        running cloud job, not enough of this attempt observed yet, or a projection that still
        fits. Public: `views.py` reads it for the job view and `pasar ls`/`show`, and `_extend`
        reads the same figure to size an extension, so the two can never disagree about whether
        (or by how much) a job is overrunning."""
        if not self._is_cloud(job) or job.state != State.RUNNING:
            return None
        target = self.cfg.clouds.get(job.spec.target)
        if target is None:
            return None
        att = self.store.current_attempt(job.id)
        if att is None:
            return None
        approved = self._attempt_seconds(job, att, target)
        return _needs_more_time(self.store, job, self.store.attempts(job.id), self.clock(),
                                approved)

    def _extend(self, job_id: int) -> Job:
        """Raise a running attempt's approved ceiling to cover the overrun its own pace projects,
        through the very same approvals row a first approval writes (`add_approval` replaces the
        row for `(job_id, attempt)`) — so `_over_the_approval`'s price-rise guard (keyed on
        `hourly_rate`) and `_pause_overdue`'s enforcement (keyed on `max_cost`, via
        `_attempt_seconds`) both keep reading one number per attempt, never two that could
        disagree.

        Repeatable: each call re-reads the attempt's *current* ceiling and today's pace, so a job
        extended once and still falling further behind can be extended again — nothing here caps
        how many times, only how far past `--max-cost` any single call may reach.

        Bounded by three ceilings, and refused rather than quietly trimmed when any of them binds,
        so nobody is told a job was extended when it was not:

        - the target's own `max_runtime`, which is the limit the sandbox itself is launched with
          (`_launch_cloud`): time past it cannot be run at any price, and a projection that asks
          for it is a job reporting nonsense, not a job that needs a bigger ceiling. Writing that
          figure into the approvals row would also hand `Ledger.committed()` an imaginary
          commitment and shut every other job on the target out of its budget.
        - the submitter's own `--max-cost` (below).

        Refused past the submitter's own `--max-cost`: that figure is the hard financial limit
        they set at submit time, and an extension is a person granting more *time* on the strength
        of the job's own pace, not silently spending past a dollar figure nobody has revisited. A
        job whose cap is already what is binding its ceiling has nothing left to extend into — the
        message says so plainly and points at resubmitting with a higher cap instead of leaving
        the person to discover it when the extension appears to do nothing.

        Refused past the target's own daily and monthly budgets (`_afford_extension`), which a
        first approval also has to clear on its way through the lane: a bigger commitment made
        here is money taken from every other job on the target, so it goes through the same gate.

        The ledger is told the same new ceiling (`ledger.record`, the same call `_launch_cloud`
        makes): a longer approved run is a bigger commitment, and the budget gate that reads
        `committed()` has to see it grow, or a job could be extended past what its target's daily
        or monthly budget would otherwise allow.

        The price is stated, not assumed. An extension commits at *today's* rate, which may not be
        the rate the first approval was made at — and unlike a launch, there is no price-rise guard
        to bounce it back, because the person is standing right here. So the job's summary and a
        `extended` machine event both name the rate being committed to, and say when it is above
        the one this attempt was approved at: agreeing to more hours is also agreeing to what an
        hour now costs."""
        job = self.job(job_id)
        if not self._is_cloud(job) or job.state != State.RUNNING:
            raise Conflict(f"job {job_id} is {job.state}, not a running cloud job, so there is "
                           "no running attempt to extend")
        target = self.cfg.clouds.get(job.spec.target)
        if target is None:
            raise Conflict(f"job {job_id} runs on {job.spec.target}, which is not configured")
        att = self.store.current_attempt(job_id)
        if att is None:
            raise Conflict(f"job {job_id} has no running attempt to extend")
        now = self.clock()
        was_rate = next((r["hourly_rate"] for r in self.store.approvals(job_id)
                         if r["attempt"] == att.n), None)
        approved = self._attempt_seconds(job, att, target)
        overrun = _needs_more_time(self.store, job, self.store.attempts(job_id), now, approved)
        if overrun is None:
            raise Conflict(f"job {job_id} is not projected to run past its approved time; "
                           "nothing to extend")
        try:
            rate = self._hourly(target, job.spec.gpu)
        except (KeyError, ValueError) as e:
            raise Conflict(f"job {job_id} cannot be priced right now, so it cannot be "
                           f"extended: {e}") from None
        new_seconds = approved + overrun * EXTENSION_MARGIN
        if new_seconds > target.max_runtime:
            raise Conflict(
                f"job {job_id}'s own pace needs {fmt_duration(new_seconds)} of run time, past the "
                f"{fmt_duration(target.max_runtime)} {target.name} allows any one attempt, so it "
                "cannot be extended that far — a projection that large usually means the job is "
                "barely moving or is misreporting its progress; submit it again with a longer "
                "--time estimate (and a checkpoint to resume from) rather than extending this "
                "attempt")
        new_cost = estimate(rate, new_seconds)
        if job.spec.max_cost is not None and new_cost > job.spec.max_cost + PRICE_TOLERANCE:
            raise Conflict(
                f"job {job_id} would need ${new_cost:.2f} to cover its current pace, above the "
                f"${job.spec.max_cost:.2f} --max-cost it was submitted with; that cap is what is "
                "binding, so it cannot be extended past it — submit it again with a higher "
                "--max-cost if it should be allowed to run longer")
        self._afford_extension(target, job_id, att.n, new_cost)
        self.store.add_approval(job_id, att.n, now, new_cost, new_cost, rate)
        self.ledger.record(target.name, job_id, att.n, new_cost)
        note = (f"extended to {fmt_duration(new_seconds)} of run time, up to ${new_cost:.2f} at "
                f"${rate:.2f}/hour")
        if was_rate is not None and rate > was_rate + PRICE_TOLERANCE:
            note += f", above the ${was_rate:.2f}/hour it was approved at"
        self.store.add_machine_event(now, "extended", f"job {job_id} {note}")
        self.store.update_job(job_id, summary=note)
        self.changed()
        return self.job(job_id)

    def _afford_extension(self, target: CloudTarget, job_id: int, n: int, cost: float) -> None:
        """Refuse an extension the target's budgets cannot pay for.

        A first approval passes the same two budgets on its way through the lane
        (`decide_cloud`); without this, the second and much larger commitment would be the one
        that walked past them, quietly taking the room every other job on the target is held to.
        Approval can't override the budget, and neither can an extension.

        `committed(replacing=…)` prices this attempt at the new ceiling *instead of* the one it
        already promised, because an extension replaces that commitment rather than adding to it
        — adding the two would refuse extensions the budget could comfortably afford."""
        after = self.ledger.committed(target.name, replacing=(job_id, n, cost))
        day = self.ledger.settled_day(target.name)
        month = self.ledger.settled_month(target.name)
        for total, spent, budget, period in ((day + after, day, target.daily_budget, "daily"),
                                             (month + after, month, target.monthly_budget,
                                              "monthly")):
            if total > budget + PRICE_TOLERANCE:
                raise Conflict(
                    f"extending job {job_id} to ${cost:.2f} would commit ${total:.2f} against "
                    f"{target.name}'s ${budget:.2f} {period} budget (${spent:.2f} already "
                    f"settled, ${after:.2f} committed by jobs still running), so it cannot be "
                    "extended that far — raise the budget, or let it pause at its approved time "
                    "and approve it again once there is room")

    def _check_platform(self, bundle: Bundle) -> None:
        """Refuse a submit whose lockfile cannot resolve for the cloud's architecture, before a
        GPU is rented for a job that could only ever have failed to build.

        Remembered per environment, because this shells out to uv and submit has to stay quick:
        the key is the hash of `pyproject.toml`, `uv.lock` and `.python-version`, so a sweep of
        twenty jobs over one lockfile pays for one resolve, and editing any of the three is a
        different key and gets checked again."""
        if bundle.env.key in self._platform_checked:
            return
        self.platform_check(bundle.root)
        self._platform_checked.add(bundle.env.key)

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
        if spec.data:
            raise ValueError("--data is not wired up yet: a cloud job's input data has to "
                             "arrive with the provider work (e.g. a volume mount in the target's "
                             "config), not through pasar submit; leave it out for now")
        self._validate_spec(spec)
        try:
            est = self._cost(target, spec.gpu, spec.est_runtime)
        except KeyError as e:
            raise ValueError(f"{spec.target} has no price for --gpu {spec.gpu}: {e}") from None
        if spec.max_cost is not None and est > spec.max_cost + PRICE_TOLERANCE:
            raise ValueError(
                f"estimated cost ${est:.2f} already exceeds --max-cost ${spec.max_cost:.2f}; "
                "raise --max-cost or lower --time so the estimate fits under it")
        now = self.clock()
        commit, diff = gitinfo.capture(spec.cwd)
        tmp = Path(tempfile.mkdtemp(dir=self.data_dir, prefix="bundle-"))
        try:
            try:
                bundle = build_bundle(spec.cwd, tmp / "bundle.tar", target.bundle_max)
                self._check_platform(bundle)
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

    def approve(self, job_id: int, extend: bool = False) -> Job:
        """Let one attempt run, at today's price. Approval is per attempt: a paused or reclaimed
        job comes back here rather than straight to the queue. The row records the price only —
        the estimate and the ceiling the launch is held to — because pasard has no
        authentication and there is nobody to name as the approver.

        `extend=True` is a different action on the same verb: it raises the *running* attempt's
        ceiling instead of approving a new one. See `_extend`."""
        if extend:
            return self._extend(job_id)
        job = self.job(job_id)
        if job.state != State.AWAITING:
            raise Conflict(f"job {job_id} is {job.state}, not awaiting approval")
        target = self.cfg.clouds.get(job.spec.target)
        if target is None:
            raise Conflict(f"job {job_id} runs on {job.spec.target}, which is not configured")
        if job.spec.target not in self.executors:
            # Approving would only move it to the queue, where nothing launches it and nothing
            # expires it: the job would wait there for good.
            raise Conflict(f"cloud target {job.spec.target!r} has no provider on this pasard, "
                           "so approving this job would leave it queued for good")
        now = self.clock()
        try:
            rate = self._hourly(target, job.spec.gpu)
            est = estimate(rate, job.spec.est_runtime)
            top = self._ceiling(target, job.spec)
        except (KeyError, ValueError) as e:
            # An approval is a person agreeing to a price, so an approval with no price in it is
            # not one: the row's NULL hourly_rate also switches off `_over_the_approval`'s
            # price-rise guard, and the job would launch at whatever the market happened to ask.
            # Refused rather than written, the same way a provider-less target is.
            raise Conflict(f"job {job_id} could not be priced right now, so approving it would "
                           f"agree to a price nobody has seen: {e}. Try again once "
                           f"{job.spec.target} reports its rates") from None
        self.store.add_approval(job_id, len(self.store.attempts(job_id)) + 1, now, est, top, rate)
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

    def _pull_root(self) -> Path:
        """Where a pull lands when nobody names a `--to`. `cfg.pull_dir` is what production
        uses (resolved by `load_config` to `<data_dir>/pulls`); the fallback to `self.data_dir`
        (the constructor argument, not `cfg.data_dir`) is what a `Daemon` built directly from an
        unresolved `Config()` gets instead — which is how every test builds one, and which must
        not write into the test process's own working directory."""
        return Path(self.cfg.pull_dir) if self.cfg.pull_dir else self.data_dir / "pulls"

    def pull(self, job_id: int, dest: Path | None = None, keep: bool = False) -> dict:
        """Fetch a finished cloud job's persist dir to local disk, verify it landed whole, and
        delete the remote copy — the only copy of whatever the job left behind, until this runs.

        Runs to completion on the caller's thread rather than touching the event loop itself:
        `api.py` is the one that has to keep a multi-GiB fetch off it (`asyncio.to_thread`), and
        a later automatic post-finish pull calls this exact method, so it stays self-contained
        here — no CLI or HTTP concerns.

        Refused (a `Conflict`, each saying why) for: a job whose checkpoint is still live (not
        `TERMINAL`, including `AWAITING` — the very case `TERMINAL` excludes, because that
        checkpoint is what the next attempt resumes from); a local job, which never had anything
        on a provider; a cloud target whose provider this pasard cannot reach (config or
        provider gone, like `approve`'s own refusal); a second pull of the same job while one is
        already in flight; and a `dest` that already exists with something in it (or exists as a
        plain file), which pulling into would both corrupt and make the verification below
        meaningless.

        `persist_usage` is read *before* downloading, and is the number the download is checked
        against — not its own return value, which a provider that quietly wrote less could lie
        about, but a private staging directory (never `dest` itself — see `_pull`) walked fresh
        on disk after the fact. A mismatch, or the download itself raising, deletes nothing: not
        the remote copy, not the staged local one — the error names where the staged copy is, so
        nothing is lost even when a pull cannot finish.
        """
        job = self.job(job_id)
        if job.state not in TERMINAL:
            raise Conflict(f"job {job_id} is {job.state}, not finished: its checkpoint is still "
                           "live and a future attempt may resume from it, so it cannot be pulled "
                           "yet — wait for it to finish, or cancel it first")
        if not self._is_cloud(job):
            raise Conflict(f"job {job_id} ran on the local GPU; there is nothing on a provider "
                           "to pull")
        ex = self.executors.get(job.spec.target)
        if not isinstance(ex, CloudExecutor):
            raise Conflict(f"job {job_id} ran on {job.spec.target}, which has no provider on "
                           "this pasard, so its persist dir cannot be reached")
        dest = Path(dest) if dest is not None else self._pull_root() / str(job_id)
        with self._pull_lock:
            if job_id in self._pulling:
                raise Conflict(f"job {job_id} is already being pulled")
            self._pulling.add(job_id)
        try:
            return self._pull(job_id, job, ex.provider, dest, keep)
        finally:
            with self._pull_lock:
                self._pulling.discard(job_id)

    def _pull(self, job_id: int, job: Job, provider, dest: Path, keep: bool) -> dict:
        if dest.exists():
            if not dest.is_dir():
                raise Conflict(f"{dest} exists and is not a directory; pulling into it is "
                               "refused — pick another --to")
            if any(dest.iterdir()):
                raise Conflict(f"{dest} already exists and is not empty; pulling into it would "
                               "merge with whatever is already there and make verification "
                               "meaningless — pick another --to, or clear it out first")
        files, size = provider.persist_usage(job_id)
        if files == 0 and size == 0:
            # Not an error: a job that never wrote a checkpoint is common, and there is nothing
            # to delete at the provider either.
            return {"job_id": job_id, "files": 0, "bytes": 0, "dest": None, "deleted": False}
        # Downloaded into a private staging directory, never straight into `dest`: the
        # non-empty check above is check-then-act, so two pulls of *different* jobs racing the
        # same `--to` can both pass it before either has written a byte. Downloading straight
        # into `dest` would let each verify and then delete against a directory that both of
        # them wrote into — if their files happened to share names and sizes, both verifications
        # would pass and both remote copies would be deleted, with only one job's data actually
        # surviving underneath. A staging directory made fresh by `mkdtemp`, named uniquely, can
        # never collide with another pull's, so each pull only ever verifies a directory it alone
        # wrote to; claiming `dest` itself is left entirely to the atomic `os.rename` below, which
        # is also this function's only real defence against the same race — the up-front check is
        # just the fast, friendly path for the ordinary case.
        dest.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=dest.parent, prefix=f".pasar-pull-{job_id}-"))
        try:
            provider.download_persist(job_id, staging)
        except Exception as e:
            raise Conflict(
                f"job {job_id}'s download failed before it could be verified ({e}); nothing was "
                f"deleted at the provider, and whatever it wrote is kept at {staging} — check it "
                "by hand, or just try the pull again") from e
        found_files, found_bytes = _disk_usage(staging)
        if found_files != files or found_bytes != size:
            raise Conflict(
                f"job {job_id}'s pull does not match what {job.spec.target} reported: expected "
                f"{files} file(s)/{size} bytes, found {found_files}/{found_bytes} on disk; "
                f"nothing was deleted, at the provider or locally — the download is kept at "
                f"{staging}, check it by hand before trying again")
        try:
            # Atomic, and on Linux refuses outright if `dest` exists and is not empty: exactly
            # the race the up-front check above cannot close on its own. A lost race here is a
            # clean refusal rather than a silent merge or overwrite of whatever won it.
            os.rename(staging, dest)
        except OSError as e:
            raise Conflict(
                f"job {job_id}'s pull was verified, but {dest} was claimed by another pull "
                f"before this one could move into it ({e}); nothing was deleted at the "
                f"provider — the verified download is kept at {staging}: move it into place "
                "by hand, or pull again with a different --to") from None
        deleted = False
        if not keep:
            provider.delete_persist(job_id)
            deleted = True
        now = self.clock()
        self.store.add_machine_event(
            now, "pulled",
            f"job {job_id}: pulled {files} file(s), {size} bytes to {dest}"
            + (" and deleted the remote copy" if deleted else "; kept the remote copy"))
        self.changed()
        return {"job_id": job_id, "files": files, "bytes": size, "dest": str(dest),
                "deleted": deleted}

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
            # And unconditionally, because a sandbox asked to stop and still running is billing:
            # reading its status may be the very thing that is failing.
            ex.enforce_stops()
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
            why, fix = self._unreachable(job.spec.target)
            summary = f"{why}, so this attempt cannot be followed"
            if att is not None and att.end_time is None:
                self.store.update_attempt(job.id, att.n, end_time=now, end_kind=EndKind.FAILED,
                                          reason="target_gone", summary=summary)
            self.store.add_machine_event(
                now, "target_gone",
                f"job {job.id} runs on {job.spec.target} and {why}: "
                f"{att.unit if att else 'its attempt'} may still be running and billing — "
                f"end it at the provider, or {fix}")
            self.store.update_job(job.id, state=State.FAILED, reason="target_gone",
                                  summary=summary, stop_requested=None)
            self.cloud_units.pop(job.id, None)
            self.usage.pop(job.id, None)
        for job in self.store.list_jobs([State.AWAITING, State.QUEUED]):
            if job.spec.target in self.executors:
                continue
            why, fix = self._unreachable(job.spec.target)
            self.store.add_machine_event(
                now, "target_gone", f"job {job.id} is waiting for {job.spec.target} and {why}; "
                "it was cancelled rather than left waiting forever")
            self.store.update_job(
                job.id, state=State.CANCELLED, reason="target_gone",
                summary=f"{why}; nothing was spent — submit it again to a target that works, "
                        f"or {fix}")

    def _unreachable(self, target: str) -> tuple[str, str]:
        """Why a target cannot be reached, and what would fix it. The two cases want different
        things done about them — one is a config file to put back, the other a provider to
        install — and `_submit_cloud` and `approve` already take care to tell them apart, so the
        message somebody reads after their job was settled should too."""
        if target in self.cfg.clouds:
            return (f"{target} is configured but has no provider on this pasard",
                    f"install {target}'s provider and restart pasard")
        return (f"{target} is no longer configured on this pasard",
                f"put {target} back in the config and restart pasard")

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
        keeps the decision (and the deadline it is measured against) with the daemon.

        This is the only thing that stops a running attempt, so it is also where `--max-cost` is
        enforced: `_attempt_seconds` holds the attempt to the shorter of the window its own
        approvals row bought (including any extension `_extend` has granted since) and what its
        `--max-cost` buys at today's rate. A job stopped for either reason pauses and returns for
        approval.

        Driven from the store, not from this tick's statuses: a status read that threw is not a
        reason to let an attempt bill past the time somebody approved."""
        for job in self.store.list_jobs([State.RUNNING]):
            target = self.cfg.clouds.get(job.spec.target)
            if not self._is_cloud(job) or job.stop_requested or target is None:
                continue
            att = self.store.current_attempt(job.id)
            if att is None:
                continue
            approved = self._attempt_seconds(job, att, target)
            if now - att.start_time < approved:
                continue
            try:
                self._executor(job).stop(att.unit)
            except Exception:
                # Nothing was asked to stop, so leave the job running and ask again next tick
                # rather than record a stop that never happened.
                log.exception("job %s could not be paused at its approved run time", job.id)
                continue
            if job.spec.max_cost is not None and approved < self._window(job.spec, target):
                # The pause itself reads the same either way ("paused at its approved run time"),
                # so say which of the two deadlines was the short one while it is still known.
                self.store.add_machine_event(
                    now, "max_cost", f"job {job.id} has run for the ${job.spec.max_cost:.2f} its "
                    "--max-cost allows; pausing it for approval")
            self.store.update_job(job.id, state=State.STOPPING, stop_requested="pause")

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
        elif (self._is_cloud(job) and st is not None and st.result == "success"
              and st.exit_code == 0 and not st.signal):
            # An attempt whose wrapper reported a clean exit has finished, even if a pause was
            # asked for in the same window: recording that as paused would send a job that
            # already succeeded back for approval and pay to run it a second time. A cancel is
            # still above this: the user asked for the job to stop.
            #
            # Cloud only, explicitly. `result` is two vocabularies in one field — the wrapper's
            # account for a cloud attempt, systemd's `Result` for a local one — and this branch
            # is reasoning about the wrapper's. Today a local job reaching it would fall through
            # to the same answer below, but only because "success" happens to mean the same in
            # both; nothing but this guard keeps that true.
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
        if self._is_cloud(job):
            self._settle_spend(job, att, now)
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
            # Only the job's own pauses: a provider reclaim is not its fault, which is why it
            # costs no retry either, and a well-behaved job on a spot-style provider must not
            # spend its budget of pauses on the provider's behalf.
            paused = sum(1 for a in self.store.attempts(job_id)
                         if a.end_kind == EndKind.PAUSED and a.reason == "time_limit")
            if paused >= PAUSE_LIMIT:
                # A pause costs no retry, so nothing else bounds this: a job that never
                # checkpoints would pause, resume from the start and pause again forever, on
                # hardware billed by the second.
                state, reason = State.FAILED, "pause_limit"
                summary = (f"ran out of its approved time {paused} times without finishing; "
                           "submit it again with a longer --time, or make sure it checkpoints "
                           "and resumes")
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

    def _settle_spend(self, job: Job, att, now: float) -> None:
        """Write what a cloud attempt really cost, now that its run time is known.

        Until here the ledger holds the ceiling from launch — the most the attempt could bill —
        because before it runs that is the only figure there is. Leaving it there charges the
        month for time nobody used: a job approved for fifteen minutes that finishes in ninety
        seconds would spend a tenth of a small allowance on its own. The rate somebody approved,
        times the seconds actually run, is not the provider's invoice, but it is within rounding
        of it and it exists now, which the invoice does not — Modal's own figures arrive hours
        later. A provider that can report real billing later writes over this same column.
        """
        rate = next((a["hourly_rate"] for a in self.store.approvals(job.id)
                     if a["attempt"] == att.n), None)
        if not rate or att.start_time is None:
            # Nothing ever priced this attempt, so there is nothing truer than its estimate.
            return
        self.ledger.settle(job.spec.target, job.id, att.n, estimate(rate, now - att.start_time))

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
                    # The same ceiling the launch records against the budget: the most this
                    # attempt can bill, `--max-cost` included.
                    price = self._ceiling(target, job.spec)
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
            if decision.probe is not None:
                # Worth a line somebody can find later: this attempt ran against the target's own
                # arithmetic, and whether it starts or is refused is the only real answer about
                # how much credit is left.
                self.store.add_machine_event(
                    now, "budget_probe",
                    f"job {decision.probe} launched on {name} past its "
                    f"${target.monthly_budget:.2f} budget: pasar's estimate says there is nothing "
                    "left, so the provider is being asked directly")
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
        # A cloud attempt's persist dir is shared by every attempt of the job, so a second one
        # really may have a checkpoint waiting: this is the flag that tells the job to look.
        env.update(PASAR_JOB_ID=str(job.id), PASAR_ATTEMPT=str(n),
                   PASAR_RESUMING="1" if n > 1 else "0",
                   PASAR_GRACE_SECONDS=str(job.spec.grace))
        return env

    def _over_the_approval(self, job: Job, n: int, target: CloudTarget, rate: float,
                           now: float) -> bool:
        """Whether the price moved since somebody approved this attempt. The launch prices from
        live rates, and a rate that moved between the two is a price nobody agreed to: send the
        job back for approval rather than spend it.

        The comparison is made on the hourly rate, which is the same thing as comparing the two
        ceilings *before* `--max-cost` is applied — and it has to be, or a cap would hide a rise
        from the very job that set it. A capped job's ceiling is the submitter's own number; it
        does not move when the market does, so comparing ceilings would compare the cap with
        itself and launch happily at any price. The cap and this guard are separate mechanisms:
        the cap bounds how long the attempt runs, this bounds what an hour may cost before a
        person looks again. The dollar figures in the message price the full approved window at
        the two rates, so they are like for like."""
        approved = next((r["hourly_rate"] for r in self.store.approvals(job.id)
                         if r["attempt"] == n), None)
        if approved is None or rate <= approved + PRICE_TOLERANCE:
            return False
        window = self._window(job.spec, target)
        was, costs = estimate(approved, window), estimate(rate, window)
        self.store.add_machine_event(
            now, "price_rise", f"job {job.id} was approved at up to ${was:.2f} for this run "
            f"but now costs ${costs:.2f}; it is waiting for approval again")
        self.store.update_job(
            job.id, state=State.AWAITING, queue_time=now, reason="price_rose",
            summary=f"the price rose to ${costs:.2f}, above the ${was:.2f} approved for this "
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
            # Two numbers, two jobs: the live rate is what the price-rise guard compares against
            # what was approved, and `price` is the most this attempt can bill — the capped
            # ceiling, which is also what the ledger is told, because `_approved_seconds` stops
            # the attempt at the point that ceiling is reached.
            rate = self._hourly(target, job.spec.gpu)
            price = self._ceiling(target, job.spec)
        except (OSError, ValueError, KeyError) as e:
            self._fail_launch(job, n, pending, now, str(e))
            return
        if self._over_the_approval(job, n, target, rate, now):
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
            # time: the daemon stops the attempt at the time that was approved (_pause_overdue,
            # which is also where --max-cost's shorter deadline is applied), and the wrapper's
            # own pause is only the backstop for a daemon that isn't there.
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
            self.store.insert_attempt(Attempt(job.id, n, unit, now))
            # Only once the attempt exists to hang it on: a spend row with no open attempt beside
            # it reads as settled the moment it is written, and would take this day's budget at
            # the full ceiling for a job that never ran.
            self.ledger.record(target.name, job.id, n, price)
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
            try:
                units = ex.list_units()
            except Exception:
                # This runs once, before the loop starts, and a cloud executor's list reaches
                # out to the provider: an outage there must not take pasard down with it, local
                # jobs and all. The sweep is a tidy-up, so skipping this target costs a stray
                # nobody noticed until the next restart; the attempts this pasard does own were
                # picked back up above.
                log.exception("%s could not be swept for stray units", name)
                continue
            owned = live.get(name, set())
            for unit in units:
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
