"""An in-memory provider: everything the cloud machinery needs, nothing that costs money."""

from dataclasses import dataclass
from pathlib import Path

from pasar.cloud.base import (
    Capabilities,
    CloudLaunch,
    CloudStatus,
    GpuRow,
    PersistFile,
    Phase,
    gpu_rows,
)
from pasar.cloud.bundle import Bundle, EnvSpec


def _check_job_id(job_id) -> None:
    """Mirrors `ModalProvider`'s guard (see modal_provider.py) so a caller cannot rely on this
    fake being any more permissive than the real provider about what a persist method accepts."""
    if type(job_id) is not int or job_id <= 0:
        raise ValueError(f"not a job id: {job_id!r}")


def launch_request(tmp_path, job_id=1, attempt=1, **kw):
    bundle = Bundle(tmp_path / "b.tar", str(tmp_path), ".", EnvSpec({}, "envkey"), 0)
    return CloudLaunch(job_id=job_id, attempt=attempt, bundle=bundle, image_key="img",
                       command="train.py", rel_cwd=".", gpu="H100", gpu_count=1,
                       env={}, volumes={}, timeout=3600,
                       tags={"pasar_job": str(job_id), "pasar_attempt": str(attempt)}, **kw)


def run_now(work) -> None:
    """The daemon's background seam, run on the caller's thread. A finished cloud job's
    automatic pull is handed to a thread in production; here it runs before `_finish` returns,
    so a test sees its whole outcome deterministically and no stray thread outlives the test."""
    work()


@dataclass
class _Sandbox:
    req: CloudLaunch
    phase: Phase = Phase.PENDING
    out: bytes = b""
    exit_code: int | None = None
    by_provider: bool = False


class FakeProvider:
    name = "fake"
    caps = Capabilities()

    def __init__(self, clock=None):
        self.clock = clock or (lambda: 0.0)
        self.gpu_names: dict[str, str] = {}  # no GPU spelled differently in its billing
        self.boxes: dict[str, _Sandbox] = {}
        self.stopped: list[str] = []
        self.terminated: list[str] = []
        self.images: dict[str, str] = {}
        self.uploads: dict[str, list[str]] = {}
        self.persisted: dict[int, dict[str, bytes]] = {}  # job_id -> {relpath: bytes}
        # A write counter standing in for each file's mtime, bumped by every `persist`, so a
        # file rewritten at the same size still lists as changed — as a real volume's would.
        self.mtimes: dict[tuple[int, str], int] = {}
        self._writes = 0
        # A job whose persist dir is gone, whichever delete removed it; `recursive_deletes` is
        # the sweep's `delete_persist` only, and `deleted_files` each file a pull's manifest
        # delete removed, so a test can tell the two apart.
        self.deleted_persist: list[int] = []
        self.recursive_deletes: list[int] = []
        self.deleted_files: list[tuple[int, str]] = []
        self.fail_launch: str | None = None
        self.next_id = 1
        # Pull test hooks: a callback run from inside `download_persist` (to prove a pull made
        # from within it — i.e. one already in flight — is refused) and a way to make it write
        # less than `persist_usage` reported (to prove a mismatch is caught rather than trusted).
        self.on_download = None
        # Run once the download has written everything it listed, before it returns: a file
        # this adds is one committed to the volume while the pull was busy downloading.
        self.after_download = None
        self.short_download: set[int] = set()
        self.fail_download: set[int] = set()
        self.rates_calls = 0

    # --- provider interface
    def prepare_image(self, env: EnvSpec) -> str:
        self.images.setdefault(env.key, f"img-{env.key}")
        return self.images[env.key]

    def launch(self, req: CloudLaunch) -> str:
        if self.fail_launch:
            raise RuntimeError(self.fail_launch)
        handle = f"sb-{self.next_id}"
        self.next_id += 1
        self.boxes[handle] = _Sandbox(req)
        return handle

    def status(self, handle: str) -> CloudStatus:
        b = self.boxes.get(handle)
        if b is None:
            return CloudStatus(Phase.GONE)
        return CloudStatus(b.phase, b.exit_code, b.by_provider, gpu_type=b.req.gpu,
                           console_url=f"https://fake/{handle}")

    def read_output(self, handle: str, cursor: int) -> tuple[bytes, int]:
        b = self.boxes.get(handle)
        if b is None:
            return b"", cursor
        return b.out[cursor:], len(b.out)

    def request_stop(self, handle: str) -> None:
        self.stopped.append(handle)

    def terminate(self, handle: str) -> None:
        self.terminated.append(handle)
        b = self.boxes.get(handle)
        if b is not None and b.phase is not Phase.EXITED:
            self.finish(handle, 137, by_provider=False)

    def list(self) -> list[tuple[str, dict[str, str]]]:
        return [(h, b.req.tags) for h, b in self.boxes.items() if b.phase is not Phase.GONE]

    def rates(self) -> dict[str, float]:
        self.rates_calls += 1
        return {"gpu_hour_cost_h100": 3.95, "cpu_hour_cost_sandbox": 0.14,
                "mem_gib_hour_cost_sandbox": 0.024}

    def gpus(self, rates: dict[str, float]) -> list[GpuRow]:
        # No memory table of its own: this fake stands in for a provider pasar has never taught
        # about a given GPU, which is exactly the "unknown GPU still lists, memory blank" case.
        return gpu_rows(rates, {}, {})

    def upload(self, paths: list[str], key: str) -> str:
        self.uploads[key] = list(paths)
        return f"/data/{key}"

    def persist_usage(self, job_id: int) -> tuple[int, int]:
        _check_job_id(job_id)
        files = self.persisted.get(job_id, {})
        return len(files), sum(len(data) for data in files.values())

    def download_persist(self, job_id: int, dest) -> tuple[int, int]:
        _check_job_id(job_id)
        if self.on_download is not None:
            self.on_download(job_id)
        if job_id in self.fail_download:
            raise RuntimeError(f"pretend network failure downloading job {job_id}'s persist dir")
        files = self.persisted.get(job_id)
        if not files:
            return 0, 0
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        items = list(files.items())
        if job_id in self.short_download:
            items = items[:-1]  # write less than persist_usage reported
        for rel, data in items:
            out = dest.joinpath(*rel.split("/"))
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
        written = len(files), sum(len(data) for data in files.values())
        if self.after_download is not None:
            self.after_download(job_id)
        return written

    def persist_manifest(self, job_id: int) -> list[PersistFile]:
        _check_job_id(job_id)
        return [PersistFile(rel, len(data), self.mtimes.get((job_id, rel), 0))
                for rel, data in sorted(self.persisted.get(job_id, {}).items())]

    def delete_persist_files(self, job_id: int, files: list[PersistFile]) -> None:
        _check_job_id(job_id)
        held = self.persisted.get(job_id, {})
        for f in files:
            if held.pop(f.path, None) is not None:
                self.deleted_files.append((job_id, f.path))
        if job_id in self.persisted and not held:
            del self.persisted[job_id]
            self.deleted_persist.append(job_id)

    def delete_persist(self, job_id: int) -> None:
        _check_job_id(job_id)
        self.recursive_deletes.append(job_id)
        self.deleted_persist.append(job_id)
        self.persisted.pop(job_id, None)

    def billed_cost(self, handles, since):
        return None

    # --- test helpers
    def start(self, handle: str) -> None:
        self.boxes[handle].phase = Phase.RUNNING

    def emit(self, handle: str, text: str) -> None:
        self.boxes[handle].out += text.encode()

    def finish(self, handle: str, code: int, by_provider: bool = False) -> None:
        b = self.boxes[handle]
        b.phase, b.exit_code, b.by_provider = Phase.EXITED, code, by_provider

    def reclaim(self, handle: str) -> None:
        self.finish(handle, 137, by_provider=True)

    def persist(self, job_id: int, rel_path: str, data: bytes) -> None:
        self._writes += 1
        self.mtimes[(job_id, rel_path)] = self._writes
        self.persisted.setdefault(job_id, {})[rel_path] = data

    def forget(self, handle: str) -> None:
        self.boxes.pop(handle, None)
