"""A throwaway pasard with a cloud target, for the browser tests of the cloud card.

Everything real about it is the daemon and the web app: the one cloud target, `modal-a`, is the
in-memory `FakeProvider` from tests/fakes_cloud.py (never a real provider, never real money),
local jobs go to the fake executor rather than systemd, and all its data lives in the directory
given by `--data`, which the caller (serve-cloud.sh) creates and deletes. It listens only on the
`--port` given, on 127.0.0.1.

`--seed` fills it with a few jobs in every section the card has (awaiting, running, finished)
before it starts serving, so the page looks like a real day's work.

Run from the repository root: `uv run python web/e2e/fake_cloud.py --data DIR --port N [--seed]`.
"""

import argparse
import asyncio
import logging
import signal
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))  # for tests.fakes*, which are not part of the package

from pasar.api import create_app
from pasar.cloud.base import gpu_rows
from pasar.cloud.executor import parse_unit
from pasar.config import CloudTarget, Config
from pasar.daemon import Daemon
from pasar.db import Store
from pasar.models import JobSpec
from pasar.server import tick_and_housekeep, uvicorn_servers
from pasar.units import GiB
from tests.fakes import FakeExecutor, FakeProbe
from tests.fakes_cloud import FakeProvider

TARGET = "modal-a"


class Provider(FakeProvider):
    """The fake provider, with the GPU memory a real one reports, so the card reads like one."""

    def gpus(self, rates):
        return gpu_rows(rates, {}, {"H100": 80})


def build(data: Path):
    target = CloudTarget(name=TARGET, provider="fake", owner="First Owner", daily_budget=30.0,
                         monthly_budget=30.0, max_running=3, max_job_cost=10.0)
    cfg = Config(data_dir=str(data), pull_dir=str(data / "pulls"), pull_min_free=0, tick=1.0,
                 clouds={TARGET: target})
    (data / "jobs").mkdir(parents=True, exist_ok=True)
    provider = Provider(clock=time.time)
    # No `uv sync --dry-run` per cloud submit: the bundle is still built for real from this
    # checkout, which is all a submit needs here.
    daemon = Daemon(cfg, Store(data / "pasar.db"), FakeExecutor(), FakeProbe(), data,
                    providers={TARGET: provider}, platform_check=lambda _cwd: None)
    return daemon, provider


def cloud_spec(**kw) -> JobSpec:
    base = {"command": "python train.py", "est_runtime": 3600, "cwd": str(REPO_ROOT),
            "target": TARGET, "gpu": "H100"}
    return JobSpec(**{**base, **kw})


def handle_of(daemon: Daemon, job_id: int) -> str:
    return parse_unit(daemon.store.current_attempt(job_id).unit)[1]


def seed(daemon: Daemon, provider: FakeProvider) -> None:
    """A little of everything: finished jobs first (so they are the oldest), then running ones,
    then two waiting on a person."""
    daemon.pull_settle = 0

    def launched(**kw) -> int:
        job = daemon.submit(cloud_spec(**kw))
        daemon.approve(job.id)
        daemon.tick()
        return job.id

    done = launched(name="sft-v2", tags=["sft"], submitter="agent-3", est_runtime=1800)
    provider.start(handle_of(daemon, done))
    provider.persist(done, "checkpoint.pt", b"w" * 4096)
    provider.finish(handle_of(daemon, done), 0)
    daemon.tick()

    crashed = launched(name="grid-search-3", tags=["grid"], submitter="agent-2", est_runtime=1800)
    provider.start(handle_of(daemon, crashed))
    daemon.pull_settle = 3600  # its results stay at the target, waiting for their pull
    provider.persist(crashed, "partial.pt", b"p" * 2048)
    provider.finish(handle_of(daemon, crashed), 1)
    daemon.tick()

    dropped = daemon.submit(cloud_spec(name="quick-test", tags=["smoke"], submitter="agent-1",
                                       est_runtime=600))
    daemon.reject(dropped.id)

    local = daemon.submit(JobSpec(command="python train.py", est_runtime=5400, cwd=str(REPO_ROOT),
                                  mem_request=24 * GiB, name="baseline-run", tags=["baseline"],
                                  submitter="agent-2"))
    warm = launched(name="warmup-test", tags=["warmup"], submitter="agent-1", est_runtime=1800)
    sweep = launched(name="eval-sweep", tags=["eval"], submitter="agent-2", est_runtime=3600)
    provider.start(handle_of(daemon, sweep))
    daemon.tick()
    del local, warm

    daemon.submit(cloud_spec(name="rlhf-policy", tags=["rlhf"], submitter="agent-1",
                             est_runtime=2700))
    daemon.submit(cloud_spec(name="train-sft", tags=["sft"], submitter="agent-3",
                             est_runtime=4500))


async def serve(daemon: Daemon, port: int, tick: float) -> None:
    wake = asyncio.Event()
    stop = asyncio.Event()
    app = create_app(daemon, wake=wake.set, shutdown=stop)
    servers = uvicorn_servers(app, [f"127.0.0.1:{port}"])

    async def loop():
        last = time.time()  # no housekeeping pass: nothing here is old enough to need it
        while not stop.is_set():
            last = tick_and_housekeep(daemon, last)
            try:
                await asyncio.wait_for(wake.wait(), timeout=tick)
            except TimeoutError:
                pass
            wake.clear()

    def shutdown():
        stop.set()
        wake.set()
        for s in servers:
            s.should_exit = True

    running = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        running.add_signal_handler(sig, shutdown)
    await asyncio.gather(loop(), *(s.serve() for s in servers))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    daemon, provider = build(args.data)
    if args.seed:
        seed(daemon, provider)
    asyncio.run(serve(daemon, args.port, daemon.cfg.tick))


if __name__ == "__main__":
    main()
