"""Daemon settings from ~/.config/pasar/config.toml."""

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from pasar.units import GiB, parse_duration, parse_size

DEFAULT_ADDRESS = "127.0.0.1:8750"

_SIZE_KEYS = {"system_reserve", "mem_margin_min", "log_retention_size"}
_DURATION_KEYS = {"default_grace", "pressure_sustain"}


@dataclass
class Config:
    bind: list[str] = field(default_factory=list)
    system_reserve: int = 16 * GiB
    mem_margin_min: int = 2 * GiB
    mem_margin_frac: float = 0.10
    default_bid: int = 1000
    default_grace: int = 120
    tick: float = 2.0
    pressure_some_avg10: float = 0.10
    pressure_mem_available_frac: float = 0.05
    pressure_sustain: int = 30
    prometheus_url: str | None = None
    grafana_url: str | None = None
    hot_temp_c: float = 85.0
    log_retention_days: int = 30
    log_retention_size: int = 20 * GiB
    mascot_dir: str = ""
    data_dir: str = ""
    allowed_hosts: list[str] = field(default_factory=list)

    def addresses(self) -> list[str]:
        """Return list of addresses with DEFAULT_ADDRESS first, then bind entries.
        Duplicates are removed while preserving order."""
        result = [DEFAULT_ADDRESS]
        seen = {DEFAULT_ADDRESS}
        for addr in self.bind:
            if addr not in seen:
                result.append(addr)
                seen.add(addr)
        return result


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "pasar"


def default_data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "pasar"


def load_config(path: Path | None = None) -> Config:
    path = path or config_dir() / "config.toml"
    raw = tomllib.loads(path.read_text()) if path.exists() else {}
    known = {f.name for f in fields(Config)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(f"unknown config keys in {path}: {', '.join(unknown)}")
    kwargs = {}
    for key, value in raw.items():
        if key in _SIZE_KEYS:
            value = parse_size(value)
        elif key in _DURATION_KEYS:
            value = parse_duration(value)
        elif key == "tick":
            value = float(parse_duration(value)) if isinstance(value, str) else float(value)
        kwargs[key] = value
    cfg = Config(**kwargs)
    cfg.mascot_dir = cfg.mascot_dir or str(config_dir() / "mascot")
    cfg.data_dir = cfg.data_dir or str(default_data_dir())
    return cfg
