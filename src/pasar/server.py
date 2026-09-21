"""`pasard`: run the scheduler loop and the HTTP API in one process."""

import asyncio
import logging
import time
from pathlib import Path

import uvicorn

from pasar.api import create_app
from pasar.config import Config, load_config
from pasar.daemon import Daemon
from pasar.db import Store
from pasar.executor.systemd import SystemdExecutor
from pasar.metrics import MetricsRecorder, Prometheus
from pasar.probes import Probe

log = logging.getLogger("pasard")
HOUSEKEEP_EVERY = 3600


def split_bind(bind: str) -> tuple[str, int]:
    host, _, port = bind.rpartition(":")
    return host.strip("[]"), int(port)


def build(cfg: Config, wake=lambda: None):
    data = Path(cfg.data_dir)
    (data / "jobs").mkdir(parents=True, exist_ok=True)
    store = Store(data / "pasar.db")
    prom = Prometheus(cfg.prometheus_url) if cfg.prometheus_url else None
    metrics = MetricsRecorder(prom, store) if prom else None
    daemon = Daemon(cfg, store, SystemdExecutor(), Probe(), data, metrics=metrics)
    return daemon, create_app(daemon, prom=prom, wake=wake)


def uvicorn_servers(app, addresses: list[str]) -> list[uvicorn.Server]:
    """Build one uvicorn server per address. `addresses` should come from
    `cfg.addresses()`, not `cfg.bind` directly: `cfg.bind` holds only extra
    addresses, and the server must always listen on DEFAULT_ADDRESS too."""
    servers = []
    for addr in addresses:
        host, port = split_bind(addr)
        servers.append(uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info")))
    return servers


async def serve(cfg: Config) -> None:
    wakeup = asyncio.Event()
    daemon, app = build(cfg, wake=wakeup.set)
    daemon.reconcile()

    async def loop():
        last_housekeep = 0.0
        while True:
            try:
                daemon.tick()
                if time.time() - last_housekeep > HOUSEKEEP_EVERY:
                    daemon.housekeep()
                    last_housekeep = time.time()
            except Exception:
                log.exception("tick failed")
            try:
                await asyncio.wait_for(wakeup.wait(), timeout=cfg.tick)
            except TimeoutError:
                pass
            wakeup.clear()

    addresses = cfg.addresses()
    servers = uvicorn_servers(app, addresses)
    log.info("pasard listening on %s", ", ".join(addresses))
    await asyncio.gather(loop(), *(s.serve() for s in servers))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(serve(load_config()))
