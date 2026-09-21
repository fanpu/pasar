"""Read machine state: memory, pressure, cgroups, per-process GPU memory, GPU errors."""

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def read_meminfo(path: Path) -> tuple[int, int]:
    values = {}
    for line in path.read_text().splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            values[key] = int(rest.split()[0]) * 1024
    return values["MemTotal"], values["MemAvailable"]


def read_psi_some_avg10(path: Path) -> float:
    for line in path.read_text().splitlines():
        if line.startswith("some "):
            fields = dict(part.split("=") for part in line.split()[1:])
            return float(fields["avg10"])
    return 0.0


def cgroup_memory(d: Path) -> int:
    try:
        return int((d / "memory.current").read_text())
    except (OSError, ValueError):
        return 0


def cgroup_pids(d: Path) -> list[int]:
    pids: list[int] = []
    if not d.is_dir():
        return pids
    for procs in d.rglob("cgroup.procs"):
        try:
            pids += [int(x) for x in procs.read_text().split()]
        except (OSError, ValueError):
            continue
    return pids


def xid_errors_between(start: float, end: float, run=subprocess.run) -> int:
    """Count NVIDIA Xid errors in the kernel log between two timestamps (0 if unreadable)."""
    cmd = ["journalctl", "-k", "-q", "--no-pager", "--since", f"@{int(start)}",
           "--until", f"@{int(end) + 1}", "-g", "NVRM: Xid"]
    try:
        r = run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    return sum(1 for line in r.stdout.splitlines() if "NVRM: Xid" in line)


class Gpu:
    """Per-process GPU memory via NVML. On unified-memory GPUs this is the only way to see it."""

    def __init__(self):
        self.ok = False
        self._handles = []
        try:
            import pynvml

            pynvml.nvmlInit()
            self._nvml = pynvml
            count = pynvml.nvmlDeviceGetCount()
            self._handles = [pynvml.nvmlDeviceGetHandleByIndex(i) for i in range(count)]
            self.ok = True
        except Exception as e:  # noqa: BLE001 - no driver, no GPU, no library
            log.warning("NVML unavailable, GPU memory per process will read as 0: %s", e)

    def process_memory(self) -> dict[int, int]:
        out: dict[int, int] = {}
        if not getattr(self, "ok", False):
            return out
        for h in self._handles:
            try:
                procs = self._nvml.nvmlDeviceGetComputeRunningProcesses(h)
            except Exception as e:  # noqa: BLE001
                log.warning("NVML process query failed: %s", e)
                continue
            for p in procs:
                if p.usedGpuMemory:
                    out[p.pid] = out.get(p.pid, 0) + int(p.usedGpuMemory)
        return out


class Probe:
    def __init__(self, proc: Path = Path("/proc"), cgroup_root: Path = Path("/sys/fs/cgroup"),
                 gpu: Gpu | None = None):
        self.proc = proc
        self.cgroup_root = cgroup_root
        self.gpu = gpu if gpu is not None else Gpu()

    def meminfo(self) -> tuple[int, int]:
        return read_meminfo(self.proc / "meminfo")

    def psi_some_avg10(self) -> float:
        return read_psi_some_avg10(self.proc / "pressure" / "memory")

    def gpu_process_memory(self) -> dict[int, int]:
        return self.gpu.process_memory()

    def _cg(self, control_group: str) -> Path:
        return self.cgroup_root / control_group.lstrip("/")

    def cgroup_memory(self, control_group: str) -> int:
        return cgroup_memory(self._cg(control_group))

    def cgroup_pids(self, control_group: str) -> list[int]:
        return cgroup_pids(self._cg(control_group))

    def xid_errors(self, start: float, end: float) -> int:
        return xid_errors_between(start, end)
