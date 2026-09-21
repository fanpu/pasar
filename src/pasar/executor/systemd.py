"""Run each attempt as a transient systemd user service.

RemainAfterExit keeps a finished unit loaded so its exit status can be read; failed units stay
loaded until reset-failed. A unit stopped with `systemctl stop` disappears once it is down.
"""

import signal as _signal
import subprocess
import sys

from pasar.executor.base import LaunchError, LaunchRequest, UnitState

_CLD_EXITED = 1
_PROPS = "LoadState,ActiveState,SubState,Result,ExecMainCode,ExecMainStatus,ControlGroup"


class SystemdExecutor:
    def __init__(self, python: str = sys.executable, run=subprocess.run):
        self.python = python
        self.run = run

    def _ctl(self, *args: str) -> subprocess.CompletedProcess:
        return self.run(["systemctl", "--user", *args], capture_output=True, text=True)

    def launch(self, req: LaunchRequest) -> None:
        cmd = [
            "systemd-run", "--user", "--quiet", f"--unit={req.unit}",
            "-p", f"StandardOutput=append:{req.log_path}",
            "-p", f"StandardError=append:{req.log_path}",
            "-p", "RemainAfterExit=yes",
            "-p", "KillMode=control-group",
            "-p", "KillSignal=SIGTERM",
            "-p", f"TimeoutStopSec={req.grace}",
            "-p", "MemorySwapMax=0",
        ]
        if req.mem_max:
            cmd += ["-p", f"MemoryMax={req.mem_max}"]
        cmd += [self.python, "-m", "pasar.launch", req.job_dir]
        r = self.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise LaunchError(r.stderr.strip() or f"systemd-run exited {r.returncode}")

    def status(self, unit: str) -> UnitState | None:
        r = self._ctl("show", f"{unit}.service", "-p", _PROPS)
        props = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
        if r.returncode != 0 or props.get("LoadState") in (None, "not-found"):
            return None
        active, sub = props.get("ActiveState"), props.get("SubState")
        if active == "inactive" and props.get("Result") == "success" and sub == "dead":
            return None  # stopped and about to be garbage-collected
        exited = sub == "exited" or active in ("failed", "inactive")
        code = int(props.get("ExecMainCode") or 0)
        status = int(props.get("ExecMainStatus") or 0)
        exit_code = status if exited and code == _CLD_EXITED else None
        sig = None
        if exited and code > _CLD_EXITED:
            try:
                sig = _signal.Signals(status).name
            except ValueError:
                sig = f"signal {status}"
        return UnitState(unit, exited, props.get("Result"), exit_code, sig,
                         props.get("ControlGroup") or None)

    def stop(self, unit: str) -> None:
        self._ctl("stop", "--no-block", f"{unit}.service")

    def kill(self, unit: str) -> None:
        self._ctl("kill", "--signal=SIGKILL", f"{unit}.service")

    def cleanup(self, unit: str) -> None:
        self._ctl("stop", f"{unit}.service")
        self._ctl("reset-failed", f"{unit}.service")

    def list_units(self) -> list[str]:
        r = self._ctl("list-units", "--all", "--plain", "--no-legend", "pasar-job-*")
        return [line.split()[0].removesuffix(".service") for line in r.stdout.splitlines()
                if line.strip()]
