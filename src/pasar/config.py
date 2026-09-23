"""Daemon settings from ~/.config/pasar/config.toml."""

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from pasar.units import GiB, parse_duration, parse_size

DEFAULT_ADDRESS = "127.0.0.1:8750"

_SIZE_KEYS = {"system_reserve", "mem_margin_min", "log_retention_size", "pull_min_free",
              "pull_max"}
_DURATION_KEYS = {"default_grace", "pressure_sustain"}


@dataclass
class CloudTarget:
    """Cloud provider target; budget.daily or budget.monthly gates whether it can be used."""
    name: str
    provider: str
    daily_budget: float
    monthly_budget: float
    profile: str = ""
    owner: str = ""
    group: str = ""
    max_running: int = 2
    timeout_factor: float = 1.5
    max_runtime: int = 86400          # seconds; Modal's own ceiling
    approval_ttl: int = 86400
    env_passthrough: list[str] = field(default_factory=list)
    volumes: dict[str, str] = field(default_factory=dict)
    bundle_max: int = 256 * 1024 * 1024
    data_max: int = 4 * GiB
    base_image: str = ""
    rates: dict[str, float] = field(default_factory=dict)
    # When the budget says a job is unaffordable, let one through anyway and take the provider's
    # answer as the real one. Off by default: it is only safe where the provider has a spending
    # limit of its own, and where it does not this budget is the only thing between a job and a
    # bill. Turn it on for a target whose provider will refuse a launch it cannot pay for.
    probe_past_budget: bool = False
    # The most one job may spend over its whole life: every attempt, every re-approval after a
    # pause or a reclaim, every extension. `--max-cost` bounds one attempt only, so without this
    # a job paused five times could spend five of them. Config rather than a constant because it
    # is meant to be raised as the jobs earn trust, and only by whoever owns this file.
    max_job_cost: float = 10.0


def _cloud_target(name: str, raw: dict) -> CloudTarget:
    known_keys = {"budget", "provider", "profile", "owner", "group", "max_running",
                  "timeout_factor", "max_runtime", "approval_ttl", "env_passthrough", "volumes",
                  "bundle_max", "data_max", "base_image", "rates", "probe_past_budget",
                  "max_job_cost"}
    unknown = sorted(set(raw) - known_keys)
    if unknown:
        raise ValueError(f"unknown config keys in cloud target {name!r}: {', '.join(unknown)}")

    budget = raw.get("budget") or {}
    if "daily" not in budget and "monthly" not in budget:
        raise ValueError(f"cloud target {name!r} needs budget.daily or budget.monthly before it "
                         "can be used")
    # An account whose whole allowance is a monthly credit has no natural daily slice, so the
    # daily cap defaults to the month's: present, so every existing gate still has a number to
    # compare against, but never the binding one.
    daily_budget = float(budget["daily"]) if "daily" in budget else float(budget["monthly"])
    if daily_budget <= 0:
        raise ValueError(f"cloud target {name!r} budget.daily must be positive, got {daily_budget}")

    monthly_budget = float(budget.get("monthly", daily_budget * 10))
    if monthly_budget <= 0:
        raise ValueError(f"cloud target {name!r} budget.monthly must be positive, got {monthly_budget}")

    max_job_cost = float(raw.get("max_job_cost", 10.0))
    if max_job_cost <= 0:
        raise ValueError(f"cloud target {name!r} max_job_cost must be positive, got {max_job_cost}")

    return CloudTarget(
        name=name,
        provider=raw.get("provider") or name,
        daily_budget=daily_budget,
        monthly_budget=monthly_budget,
        profile=raw.get("profile", ""),
        # A Modal username already says whose credit this is, which is all `owner` is for; asking
        # for a second, hand-written name only invites one that drifts out of date.
        owner=raw.get("owner") or raw.get("profile") or name,
        group=raw.get("group", ""),
        max_running=int(raw.get("max_running", 2)),
        timeout_factor=float(raw.get("timeout_factor", 1.5)),
        max_runtime=min(parse_duration(raw.get("max_runtime", "24h")), 86400),
        approval_ttl=parse_duration(raw.get("approval_ttl", "24h")),
        env_passthrough=list(raw.get("env_passthrough", [])),
        volumes=dict(raw.get("volumes", {})),
        bundle_max=parse_size(raw.get("bundle_max", "256MiB")),
        data_max=parse_size(raw.get("data_max", "4GiB")),
        base_image=raw.get("base_image", ""),
        rates={k: float(v) for k, v in (raw.get("rates") or {}).items()},
        probe_past_budget=bool(raw.get("probe_past_budget", False)),
        max_job_cost=max_job_cost,
    )


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
    pull_dir: str = ""
    # A finished cloud job's results are pulled here whole, however big, so the one guard is the
    # disk itself: an automatic pull that would leave less than this free on the destination
    # filesystem pulls nothing, since filling the disk pasard runs on would take it down too.
    pull_min_free: int = 20 * GiB
    # An optional ceiling on one automatic pull, for anyone who wants one; 0 means no limit.
    pull_max: int = 0
    allowed_hosts: list[str] = field(default_factory=list)
    clouds: dict[str, CloudTarget] = field(default_factory=dict)

    def groups(self) -> dict[str, list[CloudTarget]]:
        """Targets that share a `group`, in config order. A group is what `--on` may name when
        any of its members will do; a target with no group is only reachable by its own name."""
        out: dict[str, list[CloudTarget]] = {}
        for target in self.clouds.values():
            if target.group:
                out.setdefault(target.group, []).append(target)
        return out

    def addresses(self) -> list[str]:
        """Return list of addresses with DEFAULT_ADDRESS first, then bind entries.
        Duplicates are removed while preserving order. The `PASAR_ADDRESS` env var, when set,
        replaces DEFAULT_ADDRESS as the first entry (lets a throwaway daemon run next to a real
        pasard on the default port)."""
        first = os.environ.get("PASAR_ADDRESS") or DEFAULT_ADDRESS
        result = [first]
        seen = {first}
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
    clouds_raw = raw.pop("clouds", {})
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
    cfg.pull_dir = cfg.pull_dir or str(Path(cfg.data_dir) / "pulls")
    cfg.clouds = {name: _cloud_target(name, t) for name, t in clouds_raw.items()}
    for group, members in cfg.groups().items():
        if group in cfg.clouds:
            raise ValueError(f"group {group!r} has the same name as a cloud target, so `--on "
                             f"{group}` would be ambiguous")
        providers = {t.provider for t in members}
        if len(providers) > 1:
            raise ValueError(f"group {group!r} mixes providers ({', '.join(sorted(providers))}); "
                             "a group's members have to be interchangeable")
    return cfg
