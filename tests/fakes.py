from dataclasses import replace

from pasar.executor.base import LaunchError, LaunchRequest, UnitState
from pasar.units import GiB


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeExecutor:
    def __init__(self):
        self.units: dict[str, UnitState] = {}
        self.launched: list[LaunchRequest] = []
        self.stopped: list[str] = []
        self.killed: list[str] = []
        self.fail_launch: str | None = None

    def launch(self, req):
        if self.fail_launch:
            raise LaunchError(self.fail_launch)
        self.launched.append(req)
        self.units[req.unit] = UnitState(req.unit, False, None, None, None, f"/fake/{req.unit}")

    def status(self, unit):
        return self.units.get(unit)

    def stop(self, unit):
        self.stopped.append(unit)

    def kill(self, unit):
        self.killed.append(unit)
        self.exit(unit, signal="SIGKILL", result="signal")

    def cleanup(self, unit):
        self.units.pop(unit, None)

    def list_units(self):
        return list(self.units)

    # test helpers
    def exit(self, unit, code=0, signal=None, result=None):
        result = result or ("success" if code == 0 and signal is None else "exit-code")
        self.units[unit] = replace(self.units[unit], exited=True, result=result,
                                   exit_code=None if signal else code, signal=signal)

    def finish_stop(self, unit):
        self.units.pop(unit)


class FakeProbe:
    def __init__(self):
        self.mem_total = 121 * GiB
        self.mem_available = 100 * GiB
        self.psi = 0.0
        self.gpu: dict[int, int] = {}
        self.cg_mem: dict[str, int] = {}
        self.cg_pids: dict[str, list[int]] = {}
        self.xid = 0

    def meminfo(self):
        return self.mem_total, self.mem_available

    def psi_some_avg10(self):
        return self.psi

    def gpu_process_memory(self):
        return dict(self.gpu)

    def cgroup_memory(self, cg):
        return self.cg_mem.get(cg, 0)

    def cgroup_pids(self, cg):
        return self.cg_pids.get(cg, [])

    def xid_errors(self, start, end):
        return self.xid
