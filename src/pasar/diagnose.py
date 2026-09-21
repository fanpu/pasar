"""Work out why an attempt failed from systemd's result and the end of its log."""

import re
from dataclasses import dataclass

_GPU_OOM = ("OutOfMemoryError", "CUDA out of memory")
_EXCEPTION = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit)\b.*)$")


@dataclass
class Diagnosis:
    reason: str
    summary: str


def diagnose(*, exit_code: int | None, signal: str | None, result: str | None, log_tail: str,
             xid_errors: int = 0) -> Diagnosis:
    lines = [ln.strip() for ln in log_tail.splitlines() if ln.strip()]
    if result == "oom-kill":
        return Diagnosis("kernel_oom", "killed by the kernel: out of memory")
    for ln in reversed(lines):
        if any(p in ln for p in _GPU_OOM):
            return Diagnosis("gpu_oom", ln)
    if xid_errors:
        return Diagnosis("gpu_xid", f"GPU error (Xid) during the run ({xid_errors} in kernel log)")
    if signal:
        return Diagnosis("signal", f"killed by {signal}")
    exc = next((m.group(1) for ln in reversed(lines) if (m := _EXCEPTION.match(ln))), None)
    nccl = next((ln for ln in reversed(lines) if "NCCL" in ln and "error" in ln.lower()), None)
    return Diagnosis("exit", exc or nccl or f"exited with code {exit_code}")
