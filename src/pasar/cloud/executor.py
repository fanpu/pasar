"""Run an attempt on rented hardware behind the interface local attempts already use.

The daemon only ever sees units, so a cloud attempt hides its provider handle in its unit name
and everything else behind this class: the pump turns the container's stdout back into the
job's files, and a stop becomes a request plus a deadline, because a provider can only be
asked. What the wrapper reports about its own exit always beats what the provider reports: a
terminated sandbox looks the same (exit 137) whether it was reclaimed or asked to stop, so
pasar's own record of whether it asked is the only thing that tells those apart.
"""

import json
import logging
import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pasar.cloud.base import CloudLaunch, Phase, parse_gpu
from pasar.cloud.pump import Pump
from pasar.config import CloudTarget
from pasar.executor.base import CloudLaunchInfo, LaunchError, LaunchRequest, UnitState

log = logging.getLogger(__name__)

EVENTS_PATH = "/tmp/pasar/events.jsonl"  # a plain file in the container; the wrapper tails it
TIMEOUT_MARGIN = 60  # seconds the sandbox outlives the job's own limit and grace
STOP_MARGIN = 15  # seconds past the grace before we stop asking and pull the plug
TERMINATE_RETRY = 30  # seconds before asking again when a provider refuses to end an attempt
STATE_NAME = "cloud-{attempt}.json"


def unit_name(target: str, handle: str) -> str:
    return f"cloud:{target}:{handle}"


def parse_unit(unit: str) -> tuple[str, str] | None:
    """(target, handle) for a cloud unit, None for a local one."""
    parts = unit.split(":", 2)
    if len(parts) != 3 or parts[0] != "cloud":
        return None
    return parts[1], parts[2]


def wrapper_command(command: str, token: str, limit: int, grace: int) -> str:
    """One shell line, with the job's own command line as a single quoted word: whatever splits
    this hands the wrapper exactly what the submitter typed, quoting and all."""
    args = ["python", "-m", "pasar_job.run", "--token", token, "--events", EVENTS_PATH,
            "--limit", str(limit), "--grace", str(grace), "--", command]
    return " ".join(shlex.quote(a) for a in args)


@dataclass
class _Live:
    """One attempt we are responsible for, until the daemon cleans it up."""
    job_id: int
    attempt: int
    handle: str
    token: str
    grace: int
    pump: Pump
    asked_to_stop: bool = False
    stop_deadline: float | None = None


class CloudExecutor:
    def __init__(self, provider, target: CloudTarget, store, clock,
                 job_dir: Callable[[int], Path]):
        self.provider = provider
        self.target = target
        self.store = store
        self.clock = clock
        self.job_dir = job_dir
        self._live: dict[str, _Live] = {}

    # ---- the Executor protocol
    def launch(self, req: LaunchRequest) -> None:
        info = req.cloud
        if info is None:
            raise LaunchError("a cloud attempt needs LaunchRequest.cloud")
        handle = unit = None
        try:
            gpu, count = parse_gpu(info.gpu)
            limit = self._limit(info.limit, req.grace)
            command = self._command(req, info, limit)
            image_key = self.provider.prepare_image(info.bundle.env)
            handle = self.provider.launch(CloudLaunch(
                job_id=info.job_id, attempt=info.attempt, bundle=info.bundle,
                image_key=image_key, command=command, rel_cwd=info.bundle.rel_cwd,
                gpu=gpu, gpu_count=count, env=dict(info.env),
                volumes=dict(self.target.volumes),
                timeout=limit + req.grace + TIMEOUT_MARGIN,
                tags={"pasar_job": str(info.job_id), "pasar_attempt": str(info.attempt),
                      "pasar_target": self.target.name},
            ))
            unit = unit_name(self.target.name, handle)
            self._track(unit, info.job_id, info.attempt, handle, info.token, req.grace)
            self._persist(unit)
        except Exception as e:
            if handle is not None:
                # Bookkeeping failed around a sandbox that is already running, and nobody will
                # be watching it: end it rather than hand the job a bill nobody asked for.
                self._live.pop(unit, None)
                self._end(handle)
            raise LaunchError(f"{self.target.name}: {e}") from e

    def status(self, unit: str) -> UnitState | None:
        rec = self._live.get(unit)
        if rec is None:
            return None
        st = self.provider.status(rec.handle)
        # A wrapper that has printed its exit line is done, whatever the provider still thinks:
        # the sandbox around it may take a while to be reaped, and cleanup() ends any that does.
        if rec.pump.exit_info is None and st.phase not in (Phase.EXITED, Phase.GONE):
            if rec.stop_deadline is not None and self.clock() >= rec.stop_deadline:
                # It was asked to leave and has not; only the provider can end it now.
                self._terminate(rec)
                st = self.provider.status(rec.handle)
            if st.phase not in (Phase.EXITED, Phase.GONE):
                return UnitState(unit, False, st.phase.value, None, None, None,
                                 st.console_url or None)
        if rec.pump.exit_info is None:
            # The exit line is the last thing the wrapper writes, so an attempt that ended
            # between two ticks still has its own account of why waiting in the provider. A
            # read that fails here must not stall the attempt forever: the provider's own,
            # blunter report is the fallback the exit states below are written around.
            try:
                rec.pump.poll()
            except Exception:
                log.exception("a last read of %s failed", unit)
            else:
                self._persist(unit)
            if st.phase is Phase.GONE and rec.pump.exit_info is None:
                # The daemon settles this as a lost attempt and never asks again, so this is
                # the last chance to end a sandbox the provider only seems to have forgotten.
                self._terminate(rec)
                self._forget(unit)
                return None
        return self._exit_state(unit, rec, st)

    def stop(self, unit: str) -> None:
        rec = self._live.get(unit)
        if rec is None:
            return
        rec.asked_to_stop = True
        if rec.stop_deadline is None:
            rec.stop_deadline = self.clock() + rec.grace + STOP_MARGIN
        try:
            self.provider.request_stop(rec.handle)
        except Exception:
            log.exception("%s did not accept a stop; it will be terminated at the deadline", unit)
        self._persist(unit)

    def kill(self, unit: str) -> None:
        rec = self._live.get(unit)
        if rec is None:
            return
        self._terminate(rec)
        self._persist(unit)

    def cleanup(self, unit: str) -> None:
        """Forget the attempt, and make sure it is not still billing: an attempt whose wrapper
        reported its exit while the sandbox around it lingers has to be told to end. Ending one
        that is already over costs nothing, so this never asks first."""
        rec = self._live.get(unit)
        if rec is None:
            return
        self._end(rec.handle)
        self._forget(unit)

    def list_units(self) -> list[str]:
        return [unit_name(self.target.name, handle)
                for handle, tags in self.provider.list()
                if tags.get("pasar_target") == self.target.name]

    # ---- cloud-only surface the daemon drives
    def poll_output(self) -> None:
        """Drain every live attempt's stdout once, each tick and before statuses are read."""
        for unit, rec in list(self._live.items()):
            try:
                before = rec.pump.cursor
                rec.pump.poll()
                if rec.pump.cursor != before:
                    self._persist(unit)
            except Exception:
                log.exception("reading output for %s failed", unit)

    def terminate_stray(self, unit: str) -> bool:
        """End a sandbox no job owns. A stray local unit only wastes a cgroup and is left alone
        for a person to look at; a stray sandbox bills by the second, so it is ended."""
        parsed = parse_unit(unit)
        if parsed is None or parsed[0] != self.target.name:
            return False
        self._live.pop(unit, None)
        return self._end(parsed[1])

    def unit_of(self, job_id: int, attempt: int) -> str | None:
        """The unit a just-launched attempt got, since its handle only exists after launch."""
        return next((u for u, r in self._live.items()
                     if r.job_id == job_id and r.attempt == attempt), None)

    def adopt(self, job_id: int, unit: str) -> bool:
        """Pick an attempt back up after a restart, from the state saved beside its output.

        Damaged or half-written state is treated as no state at all: this runs once per active
        cloud job at startup, where one unreadable file must not stop the others being found.
        """
        if unit in self._live:
            return True
        for state in self._saved(job_id):
            if state.get("unit") != unit:
                continue
            try:
                rec = self._track(unit, job_id, int(state["attempt"]), str(state["handle"]),
                                  str(state["token"]), int(state["grace"]))
                rec.pump.cursor = int(state.get("cursor") or 0)
                rec.pump.exit_info = state.get("exit")
                rec.asked_to_stop = bool(state.get("asked_to_stop"))
                deadline = state.get("stop_deadline")
                rec.stop_deadline = None if deadline is None else float(deadline)
            except (KeyError, TypeError, ValueError):
                log.warning("the saved state of %s is unusable", unit)
                self._live.pop(unit, None)
                return False
            return True
        return False

    # ---- internals
    def _limit(self, asked: int, grace: int) -> int:
        """The run time the wrapper enforces, which must end before the sandbox's own timeout
        does. A provider that kills the sandbox first leaves no exit line and looks exactly
        like a reclaim, which would pause the job and pay to run it again from the start."""
        room = self.target.max_runtime - grace - TIMEOUT_MARGIN
        if room <= 0:
            raise LaunchError(f"{self.target.name} allows {self.target.max_runtime}s per "
                              f"attempt, too little for a {grace}s grace period")
        return min(asked, room)

    def _command(self, req: LaunchRequest, info: CloudLaunchInfo, limit: int) -> str:
        spec = json.loads((Path(req.job_dir) / "launch.json").read_text())
        return wrapper_command(spec["command"], info.token, limit, req.grace)

    def _track(self, unit: str, job_id: int, attempt: int, handle: str, token: str,
               grace: int) -> _Live:
        pump = Pump(self.provider, handle, token, self.job_dir(job_id),
                    on_sample=lambda rows: self._samples(job_id, attempt, rows),
                    on_exit=lambda _obj: self._persist(unit))
        rec = _Live(job_id, attempt, handle, token, grace, pump)
        self._live[unit] = rec
        return rec

    def _samples(self, job_id: int, attempt: int, rows: list) -> None:
        if rows:
            self.store.add_gpu_samples(job_id, attempt, self.clock(), rows)

    def _end(self, handle: str) -> bool:
        """Ask the provider to end an attempt now; False if it refused, so it can be asked
        again. Ending one that is already over is a no-op at every provider we support."""
        try:
            self.provider.terminate(handle)
        except Exception:
            log.exception("terminating %s failed", handle)
            return False
        return True

    def _terminate(self, rec: _Live) -> None:
        rec.asked_to_stop = True
        # The deadline is only cleared once the provider has really taken it: a sandbox nobody
        # ends keeps billing until its own timeout, so a refusal has to be asked again.
        rec.stop_deadline = None if self._end(rec.handle) else self.clock() + TERMINATE_RETRY

    def _exit_state(self, unit: str, rec: _Live, st) -> UnitState:
        """What ended the attempt. The wrapper's own account wins whenever it left one: the
        provider reports 137 for every termination, so it cannot tell a job that finished from
        one whose machine was taken away, and calling a finished job reclaimed would pause it
        and pay to run it a second time."""
        info = rec.pump.exit_info
        if info is not None:
            code, signal, reason = info.get("code"), info.get("signal"), info.get("reason")
            if reason == "time_limit":
                result = "time_limit"
            elif reason == "stopped":
                # A reclaim reaches the wrapper as the same SIGTERM a stop does; only our own
                # record of having asked tells them apart.
                result = "stopped" if rec.asked_to_stop else "reclaimed"
            elif signal:
                result = "signal"
            elif code == 0:
                result = "success"
            else:
                result = "exit-code"
        else:
            code, signal = st.exit_code, None
            if rec.asked_to_stop:
                result = "stopped"
            elif st.ended_by_provider:
                result = "reclaimed"
            else:
                result = "success" if code == 0 else "exit-code"
        return UnitState(unit, True, result, code, signal, None, st.console_url or None)

    def _state_path(self, job_id: int, attempt: int) -> Path:
        """One file per attempt, so a relaunch can never read the attempt before it."""
        return self.job_dir(job_id) / STATE_NAME.format(attempt=attempt)

    def _saved(self, job_id: int) -> list[dict]:
        states = []
        try:
            paths = sorted(self.job_dir(job_id).glob(STATE_NAME.format(attempt="*")))
        except OSError:
            return states
        for path in paths:
            try:
                state = json.loads(path.read_text())
            except (OSError, ValueError):
                log.warning("%s is not readable saved state", path)
                continue
            if isinstance(state, dict):
                states.append(state)
        return states

    def _forget(self, unit: str) -> None:
        rec = self._live.pop(unit, None)
        if rec is not None:
            self._state_path(rec.job_id, rec.attempt).unlink(missing_ok=True)

    def _persist(self, unit: str) -> None:
        """Save what a restarted daemon needs to keep following this attempt: where its output
        was read up to, and whatever it has already said about how it ended. Private, because
        the token it holds is what keeps a job from forging its own control lines."""
        rec = self._live.get(unit)
        if rec is None:
            return
        path = self._state_path(rec.job_id, rec.attempt)
        tmp = path.with_name(path.name + ".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"unit": unit, "handle": rec.handle, "token": rec.token,
                           "attempt": rec.attempt, "grace": rec.grace, "cursor": rec.pump.cursor,
                           "asked_to_stop": rec.asked_to_stop, "stop_deadline": rec.stop_deadline,
                           "exit": rec.pump.exit_info}, f)
            # O_CREAT leaves an existing file's mode alone, and a leftover .tmp from a crash
            # would hand the token whatever mode it had.
            os.chmod(tmp, 0o600)
            tmp.replace(path)
        except OSError:
            # Worth a line in the log, but never worth failing a running attempt over: this
            # only costs the ability to pick the attempt back up after a restart.
            log.exception("saving the state of %s failed", unit)
