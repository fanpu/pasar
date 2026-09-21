"""Memory pool and per-job reservation math."""

from pasar.config import Config


def pool_size(mem_total: int, cfg: Config) -> int:
    """Memory pasar may hand out: everything except the system reserve."""
    return max(0, mem_total - cfg.system_reserve)


def reservation(mem_request: int | None, pool: int, cfg: Config) -> int:
    """What a job holds while running. Whole-GPU jobs (no request) take the entire pool."""
    if mem_request is None:
        return pool
    return mem_request + max(cfg.mem_margin_min, int(mem_request * cfg.mem_margin_frac))
