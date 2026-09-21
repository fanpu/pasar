"""Kill jobs that exceed their memory limit, but only under sustained memory pressure."""

from dataclasses import dataclass

from pasar.config import Config


@dataclass
class MachineSample:
    mem_total: int
    mem_available: int
    psi_some_avg10: float  # percent, as in /proc/pressure/memory


def under_pressure(s: MachineSample, cfg: Config) -> bool:
    return (s.psi_some_avg10 / 100 > cfg.pressure_some_avg10
            or s.mem_available < cfg.pressure_mem_available_frac * s.mem_total)


def over_limit(usage: dict[int, int], limits: dict[int, int]) -> dict[int, int]:
    return {j: usage[j] - lim for j, lim in limits.items() if usage.get(j, 0) > lim}


class Watchdog:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pressure_since: float | None = None
        self.last_kill: float | None = None

    def check(self, now: float, sample: MachineSample, usage: dict[int, int],
              limits: dict[int, int]) -> int | None:
        if not under_pressure(sample, self.cfg):
            self.pressure_since = None
            return None
        if self.pressure_since is None:
            self.pressure_since = now
        sustain = self.cfg.pressure_sustain
        if now - self.pressure_since < sustain:
            return None
        if self.last_kill is not None and now - self.last_kill < sustain:
            return None
        excess = over_limit(usage, limits)
        if not excess:
            return None
        self.last_kill = now
        return max(excess, key=lambda j: (excess[j], j))
