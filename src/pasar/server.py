"""`pasard`: run the scheduler loop and the HTTP API in one process."""

import asyncio
import contextlib
import logging
import signal
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
GRACEFUL_SHUTDOWN_TIMEOUT = 3  # seconds uvicorn waits for in-flight requests before cancelling
SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def split_bind(bind: str) -> tuple[str, int]:
    host, _, port = bind.rpartition(":")
    return host.strip("[]"), int(port)


def build(cfg: Config, wake=lambda: None, shutdown: asyncio.Event | None = None):
    data = Path(cfg.data_dir)
    (data / "jobs").mkdir(parents=True, exist_ok=True)
    store = Store(data / "pasar.db")
    prom = Prometheus(cfg.prometheus_url) if cfg.prometheus_url else None
    metrics = MetricsRecorder(prom, store) if prom else None
    daemon = Daemon(cfg, store, SystemdExecutor(), Probe(), data, metrics=metrics)
    return daemon, create_app(daemon, prom=prom, wake=wake, shutdown=shutdown)


class _Server(uvicorn.Server):
    """A `uvicorn.Server` whose own SIGTERM/SIGINT capture is disabled. `serve()` below runs
    one of these per bind address on a shared loop and installs a single shared signal
    handler itself (via `loop.add_signal_handler`); without this override, each server's
    default `capture_signals()` would call `signal.signal()` on top of the others', and
    whichever registers last would win."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


def uvicorn_servers(app, addresses: list[str]) -> list[uvicorn.Server]:
    """Build one uvicorn server per address. `addresses` should come from
    `cfg.addresses()`, not `cfg.bind` directly: `cfg.bind` holds only extra
    addresses, and the server must always listen on DEFAULT_ADDRESS too."""
    servers = []
    for addr in addresses:
        host, port = split_bind(addr)
        config = uvicorn.Config(app, host=host, port=port, log_level="info",
                                 timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_TIMEOUT)
        servers.append(_Server(config))
    return servers


def tick_and_housekeep(daemon: Daemon, last_housekeep: float) -> float:
    """One scheduling pass (and housekeeping if due), isolated so an exception from either
    doesn't kill the loop. Returns the (possibly updated) last-housekeep timestamp."""
    try:
        daemon.tick()
        if time.time() - last_housekeep > HOUSEKEEP_EVERY:
            daemon.housekeep()
            last_housekeep = time.time()
    except Exception:
        log.exception("tick failed")
    return last_housekeep


async def serve(cfg: Config) -> None:
    wakeup = asyncio.Event()
    shutdown = asyncio.Event()
    daemon, app = build(cfg, wake=wakeup.set, shutdown=shutdown)
    daemon.reconcile()

    async def loop():
        last_housekeep = 0.0
        while not shutdown.is_set():
            last_housekeep = tick_and_housekeep(daemon, last_housekeep)
            try:
                await asyncio.wait_for(wakeup.wait(), timeout=cfg.tick)
            except TimeoutError:
                pass
            wakeup.clear()

    addresses = cfg.addresses()
    servers = uvicorn_servers(app, addresses)
    log.info("pasard listening on %s", ", ".join(addresses))

    def request_shutdown():
        # Idempotent: a second SIGTERM/SIGINT while already shutting down is a no-op here
        # (uvicorn's own graceful-shutdown timeout still bounds how long we wait).
        if shutdown.is_set():
            return
        log.info("shutting down")
        shutdown.set()
        wakeup.set()  # let the tick loop notice `shutdown` immediately, not after cfg.tick
        for s in servers:
            s.should_exit = True

    running_loop = asyncio.get_running_loop()
    for sig in SHUTDOWN_SIGNALS:
        running_loop.add_signal_handler(sig, request_shutdown)

    try:
        await asyncio.gather(loop(), *(s.serve() for s in servers))
    finally:
        for sig in SHUTDOWN_SIGNALS:
            running_loop.remove_signal_handler(sig)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(serve(load_config()))
