"""An in-memory provider: everything the cloud machinery needs, nothing that costs money."""

from dataclasses import dataclass

from pasar.cloud.base import Capabilities, CloudLaunch, CloudStatus, Phase
from pasar.cloud.bundle import Bundle, EnvSpec


def launch_request(tmp_path, job_id=1, attempt=1, **kw):
    bundle = Bundle(tmp_path / "b.tar", str(tmp_path), ".", EnvSpec({}, "envkey"), 0)
    return CloudLaunch(job_id=job_id, attempt=attempt, bundle=bundle, image_key="img",
                       command="train.py", rel_cwd=".", gpu="H100", gpu_count=1,
                       env={}, volumes={}, timeout=3600,
                       tags={"pasar_job": str(job_id), "pasar_attempt": str(attempt)}, **kw)


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
        self.boxes: dict[str, _Sandbox] = {}
        self.stopped: list[str] = []
        self.terminated: list[str] = []
        self.images: dict[str, str] = {}
        self.uploads: dict[str, list[str]] = {}
        self.downloads: list[tuple[int, str, str]] = []
        self.fail_launch: str | None = None
        self.next_id = 1

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
        b = self.boxes[handle]
        return b.out[cursor:], len(b.out)

    def request_stop(self, handle: str) -> None:
        self.stopped.append(handle)

    def terminate(self, handle: str) -> None:
        self.terminated.append(handle)
        self.finish(handle, 137, by_provider=False)

    def list(self) -> list[tuple[str, dict[str, str]]]:
        return [(h, b.req.tags) for h, b in self.boxes.items() if b.phase is not Phase.GONE]

    def rates(self) -> dict[str, float]:
        return {"gpu_hour_cost_h100": 3.95, "cpu_hour_cost_sandbox": 0.14,
                "mem_gib_hour_cost_sandbox": 0.024}

    def upload(self, paths: list[str], key: str) -> str:
        self.uploads[key] = list(paths)
        return f"/data/{key}"

    def download(self, job_id: int, path: str, dest: str) -> None:
        self.downloads.append((job_id, path, dest))

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
