"""Core records: job specs, jobs, and attempts."""

import json
import os
import shlex
from dataclasses import asdict, dataclass, field
from enum import StrEnum


class State(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    AWAITING = "awaiting"  # cloud only: submitted, waiting for a person to approve the cost


TERMINAL = frozenset({State.COMPLETED, State.FAILED, State.CANCELLED})


class EndKind(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    PREEMPTED = "preempted"
    CANCELLED = "cancelled"
    PAUSED = "paused"  # stopped at its approved run time, resumable


@dataclass
class JobSpec:
    command: str
    est_runtime: int  # seconds
    cwd: str
    mem_request: int | None = None  # bytes; None means the whole GPU
    bid: int = 1000
    preemptible: bool = True
    preempt: bool = False  # may stop lower-bid running jobs to start now
    grace: int = 120
    retries: int = 0
    name: str = ""
    note: str = ""
    tags: list[str] = field(default_factory=list)
    submitter: str = ""
    env: dict[str, str] | None = None  # never persisted in the database
    target: str = "local"
    gpu: str | None = None          # cloud only: "H100" or "H100:4"
    env_keys: list[str] = field(default_factory=list)  # cloud only: env vars to pass through
    data: list[str] = field(default_factory=list)      # cloud only: local paths to upload
    max_cost: float | None = None   # cloud only: dollars one attempt may spend. Submit is
                                     # refused if the estimate already exceeds it; otherwise the
                                     # daemon pauses the attempt once it has spent this much, so
                                     # the cap shortens the run as well as bounding the bill

    def to_json(self) -> str:
        d = asdict(self)
        d.pop("env")
        return json.dumps(d)

    @classmethod
    def from_json(cls, text: str) -> "JobSpec":
        return cls(**json.loads(text))


@dataclass
class Job:
    id: int
    spec: JobSpec
    state: State
    bid: int
    queue_time: float
    submit_time: float
    retries_used: int = 0
    reason: str | None = None
    summary: str = ""
    git_commit: str | None = None
    stop_requested: str | None = None  # "preempt" | "cancel" while stopping
    events_offset: int = 0


@dataclass
class Attempt:
    job_id: int
    n: int
    unit: str
    start_time: float
    end_time: float | None = None
    end_kind: EndKind | None = None
    exit_code: int | None = None
    signal: str | None = None
    reason: str | None = None
    summary: str = ""
    log_tail: str = ""
    peak_mem: int = 0
    wasted_work: float | None = None
    restart_cost: float | None = None


_LAUNCHERS = {"python", "python3", "bash", "sh", "zsh", "uv", "run", "env", "exec", "nohup",
              "torchrun", "accelerate", "launch"}


def default_name(command: str) -> str:
    """A short job name from its command: the script or module being run."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for tok in tokens:
        base = os.path.basename(tok)
        if not tok or "=" in tok or tok.startswith("-"):
            continue
        if base in _LAUNCHERS or base.startswith("python"):
            continue
        stem, ext = os.path.splitext(base)
        return stem if ext in {".py", ".sh", ".bash"} else base
    return "job"
