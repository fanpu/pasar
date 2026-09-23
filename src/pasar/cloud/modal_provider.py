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
from pathlib import Path

from pasar.cloud.base import Capabilities, CloudLaunch, CloudStatus, Phase
from pasar.cloud.bundle import EnvSpec
from pasar.cloud.container import BUNDLE, ENV, LIB, PERSIST, WORK, container_env, entry_script
from pasar.config import CloudTarget

log = logging.getLogger(__name__)

HANDLE_TAG = "pasar_handle"
RATE_TTL = 300          # seconds a price list is served from memory before a thread refreshes it
EXIT_WAIT = 120         # seconds to keep asking for an exit code after the output stream ends
JOIN_TIMEOUT = 10       # seconds close() waits for a watcher thread
MAX_DEAD_BOXES = 64     # finished attempts kept around before the oldest are dropped
DEFAULT_PYTHON = "3.12"  # only the interpreter uv runs under; the job's own comes from its lock
POLL_INTERVAL = 0.05    # seconds a watcher sleeps between checks it cannot block on


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
        self.thread: threading.Thread | None = None

    def write(self, text: str) -> None:
        with self.lock:
            self.buf += text.encode("utf-8", errors="replace")

    def phase(self, phase: Phase, clock, **fields) -> None:
        with self.lock:
            self.snap.phase = phase
            self.snap.times.setdefault(phase.value, clock())
            for key, value in fields.items():
                setattr(self.snap, key, value)


class ModalProvider:
    name = "modal"
    caps = Capabilities(graceful_stop=True, replay_output=True, billing=False)

    def __init__(self, target: CloudTarget, state_dir: Path, sdk=None, clock=time.time):
        # Imported here, never at module scope: modal is an optional dependency and a pasar with
        # no cloud target configured must not need it installed.
        self.sdk = sdk if sdk is not None else importlib.import_module("modal")
        self.target = target
        self.state_dir = Path(state_dir)
        self.clock = clock
        self._boxes: dict[str, _Box] = {}
        self._images: dict[str, object] = {}
        self._volumes: dict[str, object] = {}
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
        box.thread.start()
        return handle

    def _watch(self, box: _Box) -> None:
        """One attempt, start to finish: create (or find) the sandbox, stream its output, learn
        how it ended. Everything here runs off the daemon's tick."""
        try:
            sb = self._start(box) if box.req is not None else self._find(box.handle)
        except Exception as e:
            # There is no exception path left to raise on: the daemon recorded this attempt when
            # launch() returned. Put the reason where a person will actually see it — the job's
            # own log — and end the attempt.
            log.exception("could not start %s", box.handle)
            box.write(f"pasar: modal could not start this attempt: {e}\n")
            box.phase(Phase.EXITED, self.clock, exit_code=1)
            return
        if sb is None:
            box.phase(Phase.GONE, self.clock)
            return
        box.sb = sb
        box.phase(Phase.RUNNING, self.clock, console_url=_dashboard_url(sb))
        if box.cancelled:
            self._end(sb)
        elif box.stop_requested:
            self._signal(sb)
        self._stream(box, sb)
        self._settle(box, sb)

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
        while self._pause_before_create.is_set() and not self._closing:  # test hook; see __init__
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
        with self._lock:
            if name in self._volumes:
                return self._volumes[name]
        volume = self.sdk.Volume.from_name(name, create_if_missing=create)
        with self._lock:
            return self._volumes.setdefault(name, volume)

    def _find(self, handle: str):
        """The sandbox carrying this handle in its tags, or None if Modal has forgotten it."""
        for sb in self.sdk.Sandbox.list(app_id=self._modal_app().app_id,
                                        tags={HANDLE_TAG: handle}):
            return sb
        return None

    def _stream(self, box: _Box, sb) -> None:
        """Copy the sandbox's stdout and stderr into the box's buffer until both end.

        A reader per stream, because iterating one blocks until that stream produces a line, and
        a watcher parked in stdout would hold back the traceback arriving on stderr. The readers
        are daemons and are never joined: a Modal stream iterator has no interruption point, so
        the only thing that ends one is the sandbox ending.
        """
        done = threading.Semaphore(0)
        for which in ("stdout", "stderr"):
            threading.Thread(target=self._read_stream, args=(box, sb, which, done),
                             name=f"modal-{box.handle}-{which}", daemon=True).start()
        finished = 0
        while finished < 2 and not self._closing:
            if done.acquire(timeout=POLL_INTERVAL):
                finished += 1

    def _read_stream(self, box: _Box, sb, which: str, done: threading.Semaphore) -> None:
        try:
            for chunk in getattr(sb, which):
                box.write(chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace"))
                if self._closing:
                    break
        except Exception as e:  # noqa: BLE001 - a dropped stream must not cost us the exit code
            log.debug("%s stream for %s ended: %s", which, box.handle, e)
        finally:
            done.release()

    def _settle(self, box: _Box, sb) -> None:
        """The output ended; find out how. Modal reports the code a little after the streams
        close, so this keeps asking rather than calling a missing code an exit."""
        deadline = self.clock() + EXIT_WAIT
        code = None
        while not self._closing:
            try:
                code = sb.poll()
            except Exception as e:  # noqa: BLE001 - one bad poll, not the end of the attempt
                log.debug("poll for %s failed: %s", box.handle, e)
            if code is not None or self.clock() >= deadline:
                break
            time.sleep(0.25)
        if self._closing and code is None:
            return
        # Nothing we asked for: not our terminate, not our stop, and killed rather than exited.
        # That is Modal taking the sandbox back, which pasar retries rather than fails.
        reclaimed = code in (None, 137) and not box.cancelled and not box.stop_requested
        box.phase(Phase.EXITED, self.clock, exit_code=137 if code is None else code,
                  ended_by_provider=reclaimed)

    def _signal(self, sb) -> None:
        """Ask the attempt to stop: SIGTERM to PID 1, which the entry script made the wrapper,
        so the job gets to write its last checkpoint."""
        try:
            sb.exec("bash", "-c", "kill -TERM 1")
        except Exception as e:  # noqa: BLE001 - terminate() is still there to end it
            log.warning("could not signal a modal sandbox: %s", e)

    def _end(self, sb) -> None:
        try:
            sb.terminate()
        except Exception as e:  # noqa: BLE001 - the sandbox times out on its own eventually
            log.warning("could not terminate a modal sandbox: %s", e)

    # ---- the Provider protocol: what the daemon asks every tick
    def status(self, handle: str) -> CloudStatus:
        box = self._boxes.get(handle)
        if box is None:
            return CloudStatus(Phase.GONE)
        with box.lock:
            snap = box.snap
            return CloudStatus(snap.phase, snap.exit_code, snap.ended_by_provider,
                               gpu_type=box.req.gpu if box.req else "",
                               console_url=snap.console_url, times=dict(snap.times))

    def read_output(self, handle: str, cursor: int) -> tuple[bytes, int]:
        box = self._boxes.get(handle)
        if box is None:
            return b"", cursor
        with box.lock:
            start = cursor - box.base
            if start < 0:  # never serve a window that starts anywhere but `cursor`
                return b"", cursor
            return bytes(box.buf[start:]), box.base + len(box.buf)

    def request_stop(self, handle: str) -> None:
        box = self._boxes.get(handle)
        if box is None:
            return
        box.stop_requested = True
        if box.sb is not None:
            self._signal(box.sb)

    def terminate(self, handle: str) -> None:
        box = self._boxes.get(handle)
        if box is None:
            return
        # Set first, then act: if the sandbox does not exist yet, the watcher reads this the
        # moment it does and ends it there, so nothing is left running with nobody watching.
        box.cancelled = True
        if box.sb is not None:
            self._end(box.sb)

    def _prune(self) -> None:
        """Forget the oldest finished attempts, so a long-lived daemon's memory is bounded."""
        with self._lock:
            dead = [h for h, b in self._boxes.items()
                    if b.snap.phase in (Phase.EXITED, Phase.GONE)]
            for handle in dead[:max(0, len(dead) - MAX_DEAD_BOXES)]:
                self._boxes.pop(handle, None)

    def close(self) -> None:
        """Stop every watcher thread. Only the sandboxes are left alone: a pasard that restarts
        re-attaches to the attempts it launched, and killing them here would burn the work."""
        self._closing = True
        for box in list(self._boxes.values()):
            if box.thread is not None:
                box.thread.join(JOIN_TIMEOUT)
        if self._rates_thread is not None:
            self._rates_thread.join(JOIN_TIMEOUT)


def _pasar_job_source() -> str:
    """The wrapper's own package, injected into every image so an attempt can report its exit
    status whatever the job's lockfile happens to depend on."""
    spec = importlib.util.find_spec("pasar_job")
    if spec is None or not spec.origin:
        raise RuntimeError("pasar_job is not importable, so no cloud attempt could report its "
                           "own exit status; install it alongside pasar")
    return str(Path(spec.origin).parent)


def _dashboard_url(sb) -> str:
    try:
        return sb.get_dashboard_url()
    except Exception:  # noqa: BLE001 - a link to click is never worth failing an attempt over
        return ""
