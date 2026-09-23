"""The interface the daemon uses to run attempts."""

import secrets
from dataclasses import dataclass, field
from typing import Protocol

from pasar.cloud.bundle import Bundle


class LaunchError(Exception):
    """The attempt could not be started at all."""


@dataclass
class CloudLaunchInfo:
    """What a cloud attempt needs on top of a local one. `token` prefixes the control lines the
    in-container wrapper writes, so it is fresh per attempt: a job that printed a guessable one
    could otherwise forge its own exit status."""
    job_id: int
    attempt: int
    bundle: Bundle
    gpu: str                 # "H100" or "H100:4"
    env: dict[str, str]      # the container's whole environment
    limit: int               # seconds of run time approved
    token: str = field(default_factory=lambda: secrets.token_hex(16))


@dataclass
class LaunchRequest:
    unit: str
    job_dir: str  # contains launch.json
    log_path: str
    mem_max: int | None  # cgroup memory limit in bytes (CPU-side backstop)
    grace: int  # seconds between SIGTERM and SIGKILL when stopped
    cloud: CloudLaunchInfo | None = None  # set only for attempts run on rented hardware


@dataclass
class UnitState:
    unit: str
    exited: bool
    result: str | None
    exit_code: int | None
    signal: str | None
    control_group: str | None
    console_url: str | None = None  # cloud only: where a person can watch the sandbox


class Executor(Protocol):
    def launch(self, req: LaunchRequest) -> None: ...

    def status(self, unit: str) -> UnitState | None:
        """None once the unit is gone (e.g. after stop() completes or cleanup())."""

    def stop(self, unit: str) -> None:
        """SIGTERM, then SIGKILL after the grace period. Returns immediately."""

    def kill(self, unit: str) -> None:
        """SIGKILL every process now."""

    def cleanup(self, unit: str) -> None:
        """Forget an exited unit after its result has been read."""

    def list_units(self) -> list[str]: ...
