"""The interface a cloud provider implements, and the types it exchanges with pasar."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from pasar.cloud.bundle import Bundle, EnvSpec


def parse_gpu(spec: str) -> tuple[str, int]:
    """'H100' or 'A100-80GB:4' -> (type, count)."""
    kind, _, count = spec.partition(":")
    if not kind or (count and not count.isdigit()):
        raise ValueError(f"bad --gpu {spec!r}: expected TYPE or TYPE:COUNT, e.g. H100 or H100:4")
    n = int(count) if count else 1
    if n < 1:
        raise ValueError(f"bad --gpu {spec!r}: count must be at least 1")
    return kind, n


@dataclass
class CloudLaunch:
    job_id: int
    attempt: int
    bundle: Bundle
    image_key: str
    command: str
    rel_cwd: str
    gpu: str
    gpu_count: int
    env: dict[str, str]
    volumes: dict[str, str]
    timeout: int
    tags: dict[str, str]


class Phase(StrEnum):
    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    GONE = "gone"


@dataclass
class CloudStatus:
    phase: Phase
    exit_code: int | None = None
    ended_by_provider: bool = False
    gpu_type: str = ""
    console_url: str = ""
    times: dict[str, float] = field(default_factory=dict)  # phase -> unix time


@dataclass(frozen=True)
class Capabilities:
    graceful_stop: bool = True
    replay_output: bool = True
    billing: bool = False


class Provider(Protocol):
    """A rented-GPU backend. Handles are opaque strings the provider assigns at launch."""

    name: str
    caps: Capabilities

    def prepare_image(self, env: EnvSpec) -> str:
        """Build or reuse an image for this environment; return an image key to pass to launch()."""

    def launch(self, req: CloudLaunch) -> str:
        """Start the attempt and return a handle; raises if the provider refuses to schedule it."""

    def status(self, handle: str) -> CloudStatus:
        """Report the attempt's current phase; Phase.GONE once the provider has forgotten it."""

    def read_output(self, handle: str, cursor: int) -> tuple[bytes, int]:
        """Return output since cursor and the new cursor; cursor 0 must replay from the start."""

    def request_stop(self, handle: str) -> None:
        """Ask the attempt to exit on its own; a no-op backend still needs terminate() to end it."""

    def terminate(self, handle: str) -> None:
        """End the attempt immediately, however the provider must, and mark it exited."""

    def list(self) -> list[tuple[str, dict[str, str]]]:
        """List handles the provider still knows about, with the tags each was launched with."""

    def rates(self) -> dict[str, float]:
        """Return this provider's current price list, keyed by billing dimension."""

    def upload(self, paths: list[str], key: str) -> str:
        """Store data under key for later use as a launch volume; return a location to reference it."""

    def download(self, job_id: int, path: str, dest: str) -> None:
        """Fetch a path produced by job_id's attempt into dest on the local machine."""

    def billed_cost(self, handles: list[str], since: float) -> dict[str, float] | None:
        """Return actual billed cost per handle since the given time, or None if unsupported."""
