"""Helpers for jobs running under pasar.

Every function is a no-op outside pasar, so scripts can call them unconditionally.
"""

import json
import os
import signal
import sys
from collections.abc import Callable

__all__ = [
    "apply_memory_limit",
    "attempt",
    "checkpoint",
    "job_dir",
    "job_id",
    "memory_limit_bytes",
    "note",
    "on_preempt",
    "progress",
    "resumed",
    "resuming",
]


def job_id() -> int | None:
    value = os.environ.get("PASAR_JOB_ID")
    return int(value) if value else None


def attempt() -> int:
    return int(os.environ.get("PASAR_ATTEMPT", "1"))


def resuming() -> bool:
    """True if an earlier attempt of this job ran (so a checkpoint may exist)."""
    return os.environ.get("PASAR_RESUMING") == "1"


def job_dir() -> str | None:
    return os.environ.get("PASAR_JOB_DIR")


def _emit(event: str, **fields) -> None:
    path = os.environ.get("PASAR_EVENTS")
    if not path:
        return
    record = {"event": event, **{k: v for k, v in fields.items() if v is not None}}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, (json.dumps(record) + "\n").encode())
    finally:
        os.close(fd)


def checkpoint(step: int | None = None) -> None:
    """Report that a checkpoint was just saved."""
    _emit("checkpoint", step=step)


def resumed(step: int | None = None) -> None:
    """Report that the job loaded its checkpoint and is doing useful work again."""
    _emit("resumed", step=step)


def progress(step: int, total_steps: int | None = None) -> None:
    _emit("progress", step=step, total_steps=total_steps)


def note(text: str) -> None:
    _emit("note", text=text)


def on_preempt(fn: Callable[[], None], exit_code: int = 143) -> None:
    """Run `fn` (e.g. save a checkpoint) when pasar stops this job, then exit.

    pasar sends SIGTERM and waits $PASAR_GRACE_SECONDS before SIGKILL. Call from the main thread.
    """

    def handler(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        try:
            fn()
        finally:
            sys.exit(exit_code)

    signal.signal(signal.SIGTERM, handler)


def memory_limit_bytes() -> int | None:
    value = os.environ.get("PASAR_MEM_LIMIT_BYTES")
    return int(value) if value else None


def apply_memory_limit(device: int = 0) -> float | None:
    """Cap PyTorch's allocator at this job's pasar limit, so it raises OutOfMemoryError itself.

    Returns the fraction that was set, or None for whole-GPU jobs and outside pasar.
    """
    limit = memory_limit_bytes()
    if limit is None:
        return None
    import torch

    total = torch.cuda.get_device_properties(device).total_memory
    fraction = min(1.0, limit / total)
    torch.cuda.set_per_process_memory_fraction(fraction, device)
    return fraction
