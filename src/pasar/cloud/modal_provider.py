"""Run pasar attempts on Modal sandboxes.

Everything the daemon asks for on a tick — a phase, some output — is answered out of memory. One
watcher thread per attempt does all the talking to Modal, because `Daemon.tick()` runs
synchronously on the same asyncio loop that serves the web UI, and the calls this makes are not
fast: a cold `Sandbox.create` builds the image first, which for a lockfile with torch in it is
minutes, not milliseconds.

Handles are pasar's own (`h-<job>-<attempt>-<random>`), written into the sandbox's tags rather
than taken from Modal's object id. That is what lets `launch()` return before the sandbox exists,
and it survives a pasard restart: the unit name a job's attempt was recorded under still finds
its sandbox, through the tag, on a process that has never seen it.
"""

import importlib.util
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from pasar.cloud.base import Capabilities, CloudLaunch, CloudStatus, Phase
from pasar.cloud.bundle import EnvSpec
from pasar.cloud.container import BUNDLE, ENV, LIB, PERSIST, WORK, container_env, entry_script
from pasar.config import CloudTarget

log = logging.getLogger(__name__)

HANDLE_TAG = "pasar_handle"
TARGET_TAG = "pasar_target"  # how list() tells this target's sandboxes from another target's
RATE_TTL = 300          # seconds a price list is served from memory before a thread refreshes it
EXIT_WAIT = 120         # seconds to keep asking for an exit code after the output stream ends
EXIT_POLL = 1.0         # seconds between those asks
JOIN_TIMEOUT = 10       # seconds close() waits for every watcher thread, in total
MAX_DEAD_BOXES = 64     # finished attempts kept around before the oldest are dropped
DEFAULT_PYTHON = "3.12"  # only the interpreter uv runs under; the job's own comes from its lock
POLL_INTERVAL = 0.05    # seconds a watcher sleeps between checks it cannot block on
TERMINAL = (Phase.EXITED, Phase.GONE)


@dataclass
class _Snapshot:
    """What the watcher thread has learned, read by the daemon without touching the network."""
    phase: Phase = Phase.PENDING
    exit_code: int | None = None
    ended_by_provider: bool = False
    console_url: str = ""
    times: dict[str, float] = field(default_factory=dict)


class _Box:
    """One attempt: its output so far, what phase it is in, and the thread maintaining both."""

    def __init__(self, handle: str, req: CloudLaunch | None):
        self.handle = handle
        self.req = req            # None when re-attaching to an attempt this process never launched
        self.lock = threading.Lock()
        self.buf = bytearray()
        self.base = 0             # byte offset buf[0] sits at, from the first byte ever written
        self.snap = _Snapshot()
        self.sb = None
        self.cancelled = False
        self.stop_requested = False
        self.dropped = False      # nothing is following this attempt any more; see close()
        self.thread: threading.Thread | None = None

    def write(self, data: bytes) -> None:
        with self.lock:
            if self.dropped:
                return
            self.buf += data

    def phase(self, phase: Phase, clock, **fields) -> None:
        with self.lock:
            self.snap.phase = phase
            self.snap.times.setdefault(phase.value, clock())
            for key, value in fields.items():
                setattr(self.snap, key, value)

    def finished(self) -> bool:
        with self.lock:
            return self.snap.phase in TERMINAL


class ModalProvider:
    name = "modal"
    caps = Capabilities(graceful_stop=True, replay_output=True, billing=False)

    def __init__(self, target: CloudTarget, state_dir: Path, sdk=None, clock=time.time,
                 sleep=time.sleep):
        # Imported here, never at module scope: modal is an optional dependency and a pasar with
        # no cloud target configured must not need it installed.
        self.sdk = sdk if sdk is not None else importlib.import_module("modal")
        self.target = target
        self.state_dir = Path(state_dir)
        self.clock = clock
        # Injected alongside the clock, and always used with it: a caller that freezes `clock`
        # and leaves `sleep` real would have every wait against a deadline spin instead.
        self.sleep = sleep
        self._boxes: dict[str, _Box] = {}
        self._images: dict[str, object] = {}
        # Keyed on (name, create), not just name: a non-creating handle fetched for
        # persist_usage/delete_persist must never be handed back to launch(), which needs a
        # creating one and would otherwise fail against a volume that does not exist yet.
        self._volumes: dict[tuple[str, bool], object] = {}
        self._app = None
        self._lock = threading.Lock()
        self._rates: dict[str, float] = {}
        self._rates_at = 0.0
        self._rates_thread: threading.Thread | None = None
        self._closing = False
        # Only a test sets this, to hold a launch in the window between handing back a handle and
        # the sandbox existing — the one window where a terminate has nothing to terminate.
        self._pause_before_create = threading.Event()

    # ---- the Provider protocol: preparing to run
    def prepare_image(self, env: EnvSpec) -> str:
        """Describe the image for this environment and cache it under the environment's key.

        Nothing is built here: Modal builds an image the first time a sandbox uses it, and that
        build is exactly the multi-minute call this must not make on the daemon's tick.
        """
        with self._lock:
            if env.key in self._images:
                return env.key
        directory = self.state_dir / "env" / env.key
        for name, data in env.files.items():
            # The names are repository-root-relative POSIX paths, so a workspace member's
            # pyproject arrives as "packages/x/pyproject.toml" and needs its parent made first.
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        image = self._describe_image(directory, env)
        with self._lock:
            self._images.setdefault(env.key, image)
        return env.key

    def _describe_image(self, directory: Path, env: EnvSpec):
        """base + the lockfile + `uv sync` + pasar_job. Keyed by content by Modal itself, so the
        first job on a lockfile pays the build and every later one starts from the cache."""
        base = self.target.base_image
        image = (self.sdk.Image.from_registry(base, add_python=DEFAULT_PYTHON) if base
                 else self.sdk.Image.debian_slim(python_version=DEFAULT_PYTHON))
        image = image.apt_install("git", "tar").pip_install("uv")
        for name in sorted(env.files):
            image = image.add_local_file(directory / name, f"{ENV}/{name}", copy=True)
        image = (image.workdir(ENV)
                 .env({"UV_PROJECT_ENVIRONMENT": f"{ENV}/.venv",
                       # Hardlinks across the image's layers are not available, and uv warns and
                       # falls back per file without this.
                       "UV_LINK_MODE": "copy"})
                 # --no-install-project so a code change never invalidates this layer; --no-dev
                 # because a cloud attempt runs the job, not the project's test suite.
                 .run_commands("uv sync --frozen --no-install-project --no-dev")
                 .add_local_dir(_pasar_job_source(), f"{LIB}/pasar_job", copy=True,
                                ignore=["**/__pycache__"])
                 .run_commands(f"mkdir -p {WORK} {PERSIST}"))
        return image

    # ---- the Provider protocol: running
    def launch(self, req: CloudLaunch) -> str:
        """Hand back a handle now and do the work on a thread; see the module docstring."""
        self._prune()
        handle = f"h-{req.job_id}-{req.attempt}-{secrets.token_hex(4)}"
        box = _Box(handle, req)
        with self._lock:
            if req.image_key not in self._images:
                raise KeyError(f"no image was prepared for {req.image_key!r}")
            self._boxes[handle] = box
        box.thread = threading.Thread(target=self._watch, args=(box,),
                                      name=f"modal-{handle}", daemon=True)
        try:
            box.thread.start()
        except BaseException:
            # Nothing is following this box and nothing ever will, so it must not be left behind
            # PENDING for a `_prune` that only drops finished attempts. The raise is a refusal
            # the executor can see synchronously, which is exactly what it is: no sandbox exists.
            with self._lock:
                self._boxes.pop(handle, None)
            raise
        return handle

    def _watch(self, box: _Box) -> None:
        """One attempt, start to finish: create (or find) the sandbox, stream its output, learn
        how it ended. Everything here runs off the daemon's tick."""
        sb = None
        try:
            try:
                sb = self._start(box) if box.req is not None else self._find(box.handle)
            except Exception as e:
                # There is no exception path left to raise on: the daemon recorded this attempt
                # when launch() returned. Put the reason where a person will actually see it —
                # the job's own log — and end the attempt.
                log.exception("could not start %s", box.handle)
                box.write(f"pasar: modal could not start this attempt: {e}\n".encode())
                box.phase(Phase.EXITED, self.clock, exit_code=1)
                return
            if sb is None:
                box.phase(Phase.GONE, self.clock)
                return
            box.sb = sb
            box.phase(Phase.RUNNING, self.clock, console_url=_dashboard_url(sb))
            if box.cancelled:
                self._end_quietly(sb, box.handle)
            elif box.stop_requested:
                self._signal(sb, box.handle)
            self._stream(box, sb)
            self._settle(box, sb)
        except Exception as e:
            log.exception("following %s failed", box.handle)
            box.write(f"pasar: pasar stopped following this attempt: {e}\n".encode())
        finally:
            self._leave(box, sb)

    def _leave(self, box: _Box, sb) -> None:
        """A watcher must never walk away from an attempt still in a phase nothing will move.

        The daemon would see `done=False` on every tick for ever: the job never completes, never
        fails, never retries, and keeps its place in the queue, while the sandbox it belongs to
        goes on billing to its own timeout with nobody reading a byte of it. So whatever went
        wrong, the box ends up in a terminal phase and the sandbox is asked to stop.
        """
        if self._closing or box.finished():
            # Shutting down is the one case where RUNNING is the right thing to leave behind:
            # the attempt really is still running, and a restarted pasard re-attaches to it.
            return
        if sb is not None:
            self._end_quietly(sb, box.handle)
        box.write(b"pasar: nothing is following this attempt any more; it has been ended.\n")
        box.phase(Phase.EXITED, self.clock, exit_code=None)

    def _start(self, box: _Box):
        req = box.req
        with self._lock:
            image = self._images[req.image_key]
        # The bundle rides as a mount rather than a copied layer: the code changes with every
        # job, and copying it in would make each job rebuild the environment behind it.
        image = image.add_local_file(str(req.bundle.path), BUNDLE)
        volumes = {PERSIST: self._volume(f"pasar-{self.target.name}", create=True)}
        for path, name in req.volumes.items():
            # Never create one of these: a volume the target names but the workspace does not
            # have would mount an empty directory where a dataset was meant to be, and the job
            # would fail minutes in, having paid for every one of them.
            volumes[path] = self._volume(name, create=False)
        kwargs = {"app": self._modal_app(), "image": image, "timeout": req.timeout,
                  "workdir": "/pasar", "env": container_env(req), "volumes": volumes,
                  "tags": {**req.tags, HANDLE_TAG: box.handle}}
        if req.gpu and req.gpu_count:
            # Omitted entirely for a CPU-only attempt: passing gpu=None is fine, but passing
            # ":0" or a bare "" is not, and an attempt that asked for no accelerator must not
            # be scheduled onto one it would be billed for.
            kwargs["gpu"] = req.gpu if req.gpu_count == 1 else f"{req.gpu}:{req.gpu_count}"
        # The event is a test hook (see __init__); `_closing` is what stops a paused watcher
        # from waking up during shutdown and creating a sandbox nobody would ever follow.
        while self._pause_before_create.is_set() and not self._closing:
            time.sleep(0.005)
        return self.sdk.Sandbox.create("bash", "-c", entry_script(req), **kwargs)

    def _modal_app(self):
        with self._lock:
            if self._app is not None:
                return self._app
        app = self.sdk.App.lookup(f"pasar-{self.target.name}", create_if_missing=True)
        with self._lock:
            self._app = self._app or app
        return self._app

    def _volume(self, name: str, create: bool):
        key = (name, create)
        with self._lock:
            if key in self._volumes:
                return self._volumes[key]
        volume = self.sdk.Volume.from_name(name, create_if_missing=create)
        with self._lock:
            return self._volumes.setdefault(key, volume)

    def _find(self, handle: str):
        """The sandbox carrying this handle in its tags, or None if Modal has forgotten it."""
        for sb in self.sdk.Sandbox.list(app_id=self._modal_app().app_id,
                                        tags={HANDLE_TAG: handle}):
            return sb
        return None

    def _attach(self, handle: str) -> _Box | None:
        """The box for a handle, starting one that re-attaches by tag if this process has never
        seen it — which is every attempt, after a pasard restart."""
        with self._lock:
            box = self._boxes.get(handle)
            if box is not None:
                return box
            if self._closing:
                return None
            box = _Box(handle, None)
            self._boxes[handle] = box
        box.thread = threading.Thread(target=self._watch, args=(box,),
                                      name=f"modal-{handle}", daemon=True)
        try:
            box.thread.start()
        except BaseException:
            with self._lock:
                self._boxes.pop(handle, None)
            raise
        return box

    def _stream(self, box: _Box, sb) -> None:
        """Copy the sandbox's stdout and stderr into the box's buffer until both end.

        A reader per stream, because iterating one blocks until that stream produces a line, and
        a watcher parked in stdout would hold back the traceback arriving on stderr. The readers
        are daemons and are never joined: a Modal stream iterator has no interruption point, so
        the only thing that ends one is the sandbox ending. The watcher itself waits in short
        steps instead, so a close() during a run that never ends still returns promptly.
        """
        done = threading.Semaphore(0)
        for which in ("stdout", "stderr"):
            threading.Thread(target=self._drain, args=(box, sb, which, done),
                             name=f"modal-{box.handle}-{which}", daemon=True).start()
        finished = 0
        while finished < 2 and not self._closing:
            if done.acquire(timeout=POLL_INTERVAL):
                finished += 1

    def _drain(self, box: _Box, sb, which: str, done: threading.Semaphore) -> None:
        """One stream into the shared buffer, whole lines only.

        The tail of a chunk that is not a complete line is held here rather than appended,
        because the other stream is appending to the same buffer: half a line of stdout landing
        between two halves of a stderr line splices them into each other. For one of the
        wrapper's \\x1e control lines that is not cosmetic — those lines carry the job's exit
        status, its events and its GPU samples, and a spliced one is simply lost.

        Holding bytes rather than text also means a multi-byte character split across two chunks
        is decoded once, whole, by the pump, instead of twice as two replacement characters.
        """
        rest = b""
        note = b""
        try:
            for chunk in sb.stdout if which == "stdout" else sb.stderr:
                rest += chunk.encode("utf-8", "replace") if isinstance(chunk, str) else chunk
                cut = rest.rfind(b"\n")
                if cut >= 0:
                    box.write(rest[:cut + 1])
                    rest = rest[cut + 1:]
                if self._closing:
                    break
        except Exception as e:  # noqa: BLE001 - a dropped stream must not cost us the exit code
            # There is no reconnecting to a Modal stream, so this is the end of that half of the
            # job's output. Say so in the job's own log: a silent truncation reads exactly like
            # a job that went quiet, and on a four-hour run that is hours of guessing.
            log.warning("output of %s (%s) stopped early: %s", box.handle, which, e)
            note = f"pasar: modal stopped sending this attempt's {which}: {e}\n".encode()
        finally:
            if rest:
                # A last line with no newline would otherwise never reach the job's log, since
                # the pump only ever consumes whole lines.
                box.write(rest if rest.endswith(b"\n") else rest + b"\n")
            if note:
                box.write(note)
            done.release()

    def _settle(self, box: _Box, sb) -> None:
        """How it ended. The stream closing is not the same moment the sandbox is reaped, so
        this keeps asking for a while before giving up."""
        deadline = self.clock() + EXIT_WAIT
        code, failures = None, 0
        while not self._closing:
            try:
                code = sb.poll()
            except Exception:
                # Reported once and then retried quietly: a status endpoint that is unwell for a
                # moment is the ordinary case, and a traceback a second for two minutes buries
                # the one line that matters.
                failures += 1
                if failures == 1:
                    log.exception("could not read the exit status of %s; still asking",
                                  box.handle)
            if code is not None or self.clock() >= deadline:
                break
            self.sleep(EXIT_POLL)
        if code is None:
            if self._closing:
                return  # a shutdown, not an ending: the attempt is still running out there
            # Not knowing is not evidence. Calling this a reclaim would turn pasar's own
            # blindness into "Modal took the machine away", which pauses the job, asks a person
            # to approve another paid attempt, and does not count toward PAUSE_LIMIT — so it can
            # repeat without bound. A failure is what an unreadable status is.
            box.write(b"pasar: modal never reported an exit status for this attempt, so how it "
                      b"ended is unknown; it is recorded as a failure.\n")
            box.phase(Phase.EXITED, self.clock, exit_code=None)
            return
        # Modal reports 137 for every termination and cannot say who asked. An unexplained one is
        # a reclaim, which pauses the job rather than failing it, so it is worth getting right.
        box.phase(Phase.EXITED, self.clock, exit_code=code,
                  ended_by_provider=(code == 137 and not box.cancelled and not box.stop_requested))

    def _signal(self, sb, handle: str) -> None:
        """Ask the attempt to stop: SIGTERM to PID 1, which the entry script made the wrapper,
        so the job gets to write its last checkpoint."""
        try:
            sb.exec("bash", "-c", "kill -TERM 1")
        except Exception:
            # The executor's stop deadline is what guarantees the sandbox stops billing; a
            # refused signal only costs the job its chance to save a checkpoint first.
            log.exception("could not signal %s (%s)", handle, _object_id(sb))

    def _end(self, sb) -> None:
        """Terminate, and let a refusal reach the caller.

        `CloudExecutor._end` turns a raise into a `False` and re-arms its terminate retry; a
        refusal it never hears about is a rented GPU billing to its own timeout while pasar has
        already written the attempt down as ended. Use `_end_quietly` where there is no caller.
        """
        sb.terminate()

    def _end_quietly(self, sb, handle: str) -> None:
        """The watcher's own path, where the only caller is a thread that is about to exit."""
        try:
            self._end(sb)
        except Exception:
            log.exception("could not terminate %s (%s)", handle, _object_id(sb))

    # ---- the Provider protocol: following an attempt
    def status(self, handle: str) -> CloudStatus:
        box = self._attach(handle)
        if box is None:
            return CloudStatus(Phase.GONE)
        with box.lock:
            snap = box.snap
            return CloudStatus(snap.phase, snap.exit_code, snap.ended_by_provider,
                               gpu_type=box.req.gpu if box.req else "",
                               console_url=snap.console_url, times=dict(snap.times))

    def read_output(self, handle: str, cursor: int) -> tuple[bytes, int]:
        box = self._attach(handle)
        if box is None:
            return b"", cursor
        with box.lock:
            if cursor < box.base:
                # The pump's cursor only ever moves forward, and it is persisted across restarts,
                # so this cannot happen from that side. Failing loudly beats serving the wrong
                # window, which would splice the job's log together out of order.
                raise ValueError(f"{handle} was read from {cursor}, behind {box.base}")
            # Clamped, because a cursor at or past the end is not an error: it is what a poll
            # that arrives before the next line does looks like. Moving `base` to `cursor` there
            # would put it past bytes that were never written, and every byte that did arrive
            # afterwards would then be served at an offset short of where it belongs.
            start = min(cursor - box.base, len(box.buf))
            data = bytes(box.buf[start:])
            # Everything before this has been handed over, and the pump never asks for it again;
            # a chatty 24-hour job would otherwise keep its whole log in the daemon's memory as
            # well as on disk. Note it trims to `cursor`, not to the end: the pump stops at the
            # last complete line and asks again from there, so the tail it did not consume has
            # to still be here next time.
            del box.buf[:start]
            box.base += start
        return data, cursor + len(data)

    def request_stop(self, handle: str) -> None:
        """Ask the job to save a checkpoint and go. Modal has no stop API for a sandbox, so this
        is a signal to PID 1, which the entry script arranged to be the wrapper."""
        box = self._boxes.get(handle)
        if box is not None:
            box.stop_requested = True
        sb = self._sandbox_for(handle, box)
        if sb is not None:
            self._signal(sb, handle)

    def terminate(self, handle: str) -> None:
        """End the attempt now, and raise if Modal refuses — see `_end`."""
        box = self._boxes.get(handle)
        if box is not None:
            # Set first, then act: a sandbox still being created has nothing to terminate yet,
            # and the watcher checks this flag the moment it has one.
            box.cancelled = True
        sb = self._sandbox_for(handle, box)
        if sb is not None:
            self._end(sb)

    def _sandbox_for(self, handle: str, box: _Box | None):
        """The sandbox to act on right now, or None if there is nothing to act on yet.

        The last case is the one that matters: a handle this process never launched — after a
        pasard restart, or once `_prune` has dropped the box — still has a sandbox out there,
        and the `pasar_handle` tag is how it is found. Without this, terminate() would report
        success for a sandbox it never even looked for, and that sandbox would bill on.
        """
        if box is not None:
            if box.sb is not None:
                return box.sb
            if box.thread is not None and box.thread.is_alive():
                # Still being created. The watcher acts on the flag the caller just set, the
                # moment `Sandbox.create` hands it something to act on.
                return None
        return self._find(handle)

    def list(self) -> list[tuple[str, dict[str, str]]]:
        """Every sandbox this target still has running, by pasar's handle. Only running ones: a
        sandbox that has already ended is not a stray anybody needs to go and terminate."""
        found = []
        for sb in self.sdk.Sandbox.list(app_id=self._modal_app().app_id,
                                        tags={TARGET_TAG: self.target.name}):
            tags = sb.get_tags()
            handle = tags.get(HANDLE_TAG)
            if handle and sb.poll() is None:
                found.append((handle, tags))
        return found

    # ---- the Provider protocol: what it costs
    def rates(self) -> dict[str, float]:
        """Modal's live price list. The first call fetches it; later ones are served from memory
        and refreshed on a thread, because this is read on the daemon's tick."""
        now = self.clock()
        with self._lock:
            cached, age = dict(self._rates), now - self._rates_at
        if cached and age < RATE_TTL:
            return cached
        if cached:
            self._refresh_rates_soon()
            return cached
        return self._fetch_rates()

    def _fetch_rates(self) -> dict[str, float]:
        raw = dict(self.sdk.Workspace.from_context().billing.rates())
        rates = _aliased({k: float(v) for k, v in raw.items()})
        with self._lock:
            self._rates, self._rates_at = rates, self.clock()
        return rates

    def _refresh_rates_soon(self) -> None:
        with self._lock:
            if self._closing or (self._rates_thread and self._rates_thread.is_alive()):
                return
            self._rates_thread = threading.Thread(target=self._refresh_rates,
                                                  name="modal-rates", daemon=True)
            thread = self._rates_thread
        thread.start()

    def _refresh_rates(self) -> None:
        try:
            self._fetch_rates()
        except Exception:
            # The stale list stays in place: an estimate a few minutes old is much better than
            # refusing to price a job at all, which is what an empty table does.
            log.exception("could not refresh %s's rates", self.target.name)

    # ---- not built yet
    def upload(self, paths: list[str], key: str) -> str:
        raise NotImplementedError(
            "pasar cannot upload data to Modal yet; put large data on a named volume with "
            "modal's own CLI and mount it from the target's `volumes`")

    # ---- the Provider protocol: a job's persist dir
    def persist_usage(self, job_id: int) -> tuple[int, int]:
        _check_job_id(job_id)
        files = self._persist_files(job_id)
        return len(files), sum(e.size for e in files)

    def download_persist(self, job_id: int, dest: Path) -> tuple[int, int]:
        """See `Provider.download_persist`. Each file is streamed straight to disk, one chunk at
        a time, rather than built up in memory first: a checkpoint can be tens of GiB, and Modal
        hands `read_file` back as an iterator precisely so this never has to hold a whole one.

        What lands on disk is counted as it is written, not copied from the remote listing:
        `persist_usage`'s counts and this method's return are compared by the caller before it
        deletes the only copy, and that comparison proves nothing if both numbers trace back to
        the same listing rather than to what actually arrived. A file that writes fewer bytes
        than the volume reported for it raises, rather than being reported as if it landed whole.
        """
        _check_job_id(job_id)
        files = self._persist_files(job_id)
        if not files:
            return 0, 0
        volume = self._volume(f"pasar-{self.target.name}", create=False)
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        root = str(job_id)
        total_bytes = 0
        for entry in files:
            rel = _relative_persist_path(entry.path, root)
            out = dest.joinpath(*rel.parts)
            out.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with out.open("wb") as f:
                for chunk in volume.read_file(entry.path):
                    written += f.write(chunk)
            if written != entry.size:
                raise RuntimeError(
                    f"job {job_id}'s {entry.path!r} wrote {written} byte(s) but the volume "
                    f"reported {entry.size}; refusing to hand back a count the caller would "
                    "trust before deleting the only copy")
            total_bytes += written
        return len(files), total_bytes

    def delete_persist(self, job_id: int) -> None:
        _check_job_id(job_id)
        volume = self._volume(f"pasar-{self.target.name}", create=False)
        try:
            volume.remove_file(str(job_id), recursive=True)
        except (FileNotFoundError, self.sdk.exception.NotFoundError):
            pass  # already gone; the caller can race a person's own manual pull or delete

    def _list_persist(self, job_id: int) -> list:
        """Every entry under a job's persist dir, recursively, or `[]` if it was never created."""
        volume = self._volume(f"pasar-{self.target.name}", create=False)
        try:
            return volume.listdir(str(job_id), recursive=True)
        except self.sdk.exception.NotFoundError:
            return []

    def _persist_files(self, job_id: int) -> list:
        """The FILE entries under a job's persist dir, directories skipped. Raises on anything
        that is neither — a symlink, fifo or socket, which real Modal's `FileEntryType` also
        allows — rather than silently dropping it: dropped here, it would still be destroyed by
        a later `delete_persist`, unnoticed, because the counts on both sides of the caller's
        check would still agree with each other."""
        files = []
        for entry in self._list_persist(job_id):
            if entry.type == self.sdk.types.FileEntryType.DIRECTORY:
                continue
            if entry.type != self.sdk.types.FileEntryType.FILE:
                raise RuntimeError(
                    f"job {job_id}'s persist dir has {entry.path!r}, an entry of a kind pasar "
                    f"does not know how to handle ({entry.type!r})")
            files.append(entry)
        return files

    def billed_cost(self, handles: list[str], since: float) -> dict[str, float] | None:
        """Modal's billing report arrives hours late and is not wired up; the live estimate from
        `rates()` is what the budget and the UI use. `caps.billing` is False to match."""
        return None

    # ---- housekeeping
    def _prune(self) -> None:
        """Drop the oldest finished attempts. Their threads are gone and their buffers have been
        trimmed to whatever the pump had not read, so this is tidiness rather than a leak."""
        with self._lock:
            dead = [h for h, b in self._boxes.items()
                    if b.finished() and (b.thread is None or not b.thread.is_alive())]
            for handle in dead[:max(0, len(dead) - MAX_DEAD_BOXES)]:
                self._boxes.pop(handle, None)

    def close(self) -> None:
        """Stop following every attempt. The sandboxes are left running: a pasard that restarts
        re-attaches to them by tag, and ending them here would burn the work.

        One deadline for all the joins rather than one each, because a watcher parked inside
        `Sandbox.create` — minutes, on a cold image — cannot notice `_closing` at all, and
        waiting out each of those in turn is what turns a systemd stop into a SIGKILL, which is
        itself the orphaned-sandbox case. The budget is real time, not the injected clock: a
        shutdown has to finish even for a caller that froze the clock.
        """
        with self._lock:
            self._closing = True
            boxes, rates_thread = list(self._boxes.values()), self._rates_thread
        for box in boxes:
            # The stream readers are not joined, and cannot be: a Modal stream iterator has no
            # interruption point, so a reader blocked on a sandbox that has gone quiet stays
            # blocked. What close() can do is make sure such a reader writes nowhere, rather
            # than growing a buffer nobody will ever read from again.
            with box.lock:
                box.dropped = True
        deadline = time.monotonic() + JOIN_TIMEOUT
        for thread in [box.thread for box in boxes] + [rates_thread]:
            if thread is not None:
                thread.join(max(0.0, deadline - time.monotonic()))


def _aliased(rates: dict[str, float]) -> dict[str, float]:
    """Also price each GPU under the spelling a person types. `hourly_rate` looks up
    `gpu_hour_cost_<--gpu lowercased>`, so `--gpu A100-80GB` asks for `…a100-80gb` while Modal
    lists `…a100_80gb`; an unpriced GPU refuses to estimate, which means the job never starts."""
    out = dict(rates)
    for key, value in rates.items():
        if not key.startswith("gpu_hour_cost_"):
            continue
        name = key[len("gpu_hour_cost_"):]
        for alias in {name.replace("_", "-"), name.replace("-", "_")}:
            out.setdefault(f"gpu_hour_cost_{alias}", value)
    return out


def _pasar_job_source() -> str:
    """The wrapper's own package, injected into every image so an attempt can report its exit
    status whatever the job's lockfile happens to depend on."""
    spec = importlib.util.find_spec("pasar_job")
    if spec is None or not spec.origin:
        raise RuntimeError("pasar_job is not importable, so no cloud attempt could report its "
                           "own exit status; install it alongside pasar")
    return str(Path(spec.origin).parent)


def _check_job_id(job_id: object) -> None:
    """A job_id that is not a positive int must never reach a recursive delete: stringified,
    an empty, negative or otherwise wrong value could land on or above the volume root, or on
    someone else's job. `bool` is an `int` subclass, so it is rejected by type, not just range."""
    if type(job_id) is not int or job_id <= 0:
        raise ValueError(f"not a job id: {job_id!r}")


def _relative_persist_path(path: str, root: str) -> PurePosixPath:
    """`path`, made relative to `root`, refusing anything that would land outside a caller's
    `dest` once joined onto it. `PurePosixPath.relative_to` only checks that `path` starts with
    `root`'s components; it does not normalise `..`, so a listing entry like `1/../../x` would
    otherwise pass straight through and write outside the destination directory."""
    rel = PurePosixPath(path).relative_to(root)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"refusing to write {path!r}: outside its job's persist directory")
    return rel


def _object_id(sb) -> str:
    """Modal's own id for a sandbox, for a log line someone has to act on by hand."""
    return str(getattr(sb, "object_id", "unknown"))


def _dashboard_url(sb) -> str:
    try:
        return sb.get_dashboard_url()
    except Exception:  # noqa: BLE001 - a link to click is never worth failing an attempt over
        return ""
