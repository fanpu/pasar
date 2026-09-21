"""The interface the daemon uses to run attempts."""

from dataclasses import dataclass
from typing import Protocol


class LaunchError(Exception):
    """The attempt could not be started at all."""


@dataclass
class LaunchRequest:
    unit: str
    job_dir: str  # contains launch.json
    log_path: str
    mem_max: int | None  # cgroup memory limit in bytes (CPU-side backstop)
    grace: int  # seconds between SIGTERM and SIGKILL when stopped


@dataclass
class UnitState:
    unit: str
    exited: bool
    result: str | None
    exit_code: int | None
    signal: str | None
    control_group: str | None


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
