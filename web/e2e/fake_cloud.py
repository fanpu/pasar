"""A throwaway pasard with a cloud target, for the browser tests of the cloud card.

Everything real about it is the daemon and the web app: the two cloud targets, `modal-a` and
`modal-b`, are each the in-memory `FakeProvider` from tests/fakes_cloud.py (never a real provider, never real money),
local jobs go to the fake executor rather than systemd, and all its data lives in the directory
given by `--data`, which the caller (serve-cloud.sh) creates and deletes. It listens only on the
`--port` given, on 127.0.0.1.

`--seed` fills it with a few jobs in every section the card has (awaiting, running, finished)
before it starts serving, so the page looks like a real day's work. The seed runs on a clock set
an hour and a half back and moved forward as it goes, so the attempts it leaves span those hours on the
schedule's cloud lanes (a paused-and-resumed job among them) instead of all starting at once.

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
OTHER = "modal-b"  # a second account, so the schedule has more than one cloud lane
HOUR = 3600.0


class Clock:
    """Real time, less however far back the seed has wound it."""

    def __init__(self):
        self.back = 0.0

    def __call__(self) -> float:
        return time.time() - self.back

    def at(self, hours_ago: float) -> None:
        self.back = hours_ago * HOUR


class Provider(FakeProvider):
    """The fake provider, with the GPU memory a real one reports, so the card reads like one."""

    def gpus(self, rates):
        return gpu_rows(rates, {}, {"H100": 80})


def build(data: Path, clock: Clock):
    target = CloudTarget(name=TARGET, provider="fake", owner="First Owner", daily_budget=30.0,
                         monthly_budget=30.0, max_running=2, max_job_cost=10.0)
    other = CloudTarget(name=OTHER, provider="fake", owner="Second Owner", daily_budget=30.0,
                        monthly_budget=30.0, max_running=2, max_job_cost=20.0)
    cfg = Config(data_dir=str(data), pull_dir=str(data / "pulls"), pull_min_free=0, tick=1.0,
                 clouds={TARGET: target, OTHER: other})
    (data / "jobs").mkdir(parents=True, exist_ok=True)
    providers = {TARGET: Provider(clock=clock), OTHER: Provider(clock=clock)}
    # No `uv sync --dry-run` per cloud submit: the bundle is still built for real from this
    # checkout, which is all a submit needs here.
    daemon = Daemon(cfg, Store(data / "pasar.db"), FakeExecutor(), FakeProbe(), data,
                    clock=clock, providers=providers, platform_check=lambda _cwd: None)
    return daemon, providers


def cloud_spec(**kw) -> JobSpec:
    base = {"command": "python train.py", "est_runtime": 3600, "cwd": str(REPO_ROOT),
            "target": TARGET, "gpu": "H100"}
    return JobSpec(**{**base, **kw})


def handle_of(daemon: Daemon, job_id: int) -> str:
    return parse_unit(daemon.store.current_attempt(job_id).unit)[1]


def seed(daemon: Daemon, providers: dict[str, FakeProvider], clock: Clock) -> None:
    """A little of everything, in the order it happened over the last hour and a half: finished jobs
    first (so they are the oldest), then running ones, then two waiting on a person. On
    `modal-b`, one job was taken back by the provider mid-run and resumed on a new approval."""
    daemon.pull_settle = 0
    a, b = providers[TARGET], providers[OTHER]

    def launched(**kw) -> int:
        job = daemon.submit(cloud_spec(**kw))
        daemon.approve(job.id)
        daemon.tick()
        return job.id

    def started(provider: FakeProvider, **kw) -> int:
        job_id = launched(**kw)
        provider.start(handle_of(daemon, job_id))
        daemon.tick()
        return job_id

    clock.at(1.4)
    done = started(a, name="sft-v2", tags=["sft"], submitter="agent-3", est_runtime=3600)
    clock.at(1.35)
    bench = started(b, name="tok-bench", tags=["eval"], submitter="agent-1", est_runtime=3600,
                    target=OTHER)
    clock.at(1.2)
    resumed = started(b, name="resume-ft", tags=["sft"], submitter="agent-3",
                      est_runtime=3 * 3600, target=OTHER)
    clock.at(1.15)
    a.persist(done, "checkpoint.pt", b"w" * 4096)
    a.finish(handle_of(daemon, done), 0)
    daemon.tick()
    clock.at(1.0)
    b.finish(handle_of(daemon, bench), 0)
    daemon.tick()
    clock.at(0.85)
    b.reclaim(handle_of(daemon, resumed))  # paused: back to awaiting until a person says yes
    daemon.tick()
    clock.at(0.7)
    daemon.approve(resumed)
    daemon.tick()
    b.start(handle_of(daemon, resumed))
    daemon.tick()

    clock.at(0.65)
    crashed = started(a, name="grid-search-3", tags=["grid"], submitter="agent-2",
                      est_runtime=3600)
    clock.at(0.45)
    sweep = started(a, name="eval-sweep", tags=["eval"], submitter="agent-2", est_runtime=3600)
    clock.at(0.35)
    dropped = daemon.submit(cloud_spec(name="quick-test", tags=["smoke"], submitter="agent-1",
                                       est_runtime=600))
    daemon.reject(dropped.id)
    clock.at(0.25)
    daemon.pull_settle = 3600  # its results stay at the target, waiting for their pull
    a.persist(crashed, "partial.pt", b"p" * 2048)
    a.finish(handle_of(daemon, crashed), 1)
    daemon.tick()

    clock.at(0.05)
    local = daemon.submit(JobSpec(command="python train.py", est_runtime=5400, cwd=str(REPO_ROOT),
                                  mem_request=24 * GiB, name="baseline-run", tags=["baseline"],
                                  submitter="agent-2"))
    warm = launched(name="warmup-test", tags=["warmup"], submitter="agent-1", est_runtime=1800)
    daemon.tick()
    del local, warm, sweep
    # Approved, but the target is already running all it may: it waits to launch.
    held = daemon.submit(cloud_spec(name="pretrain-cont", tags=["pretrain"], submitter="agent-3",
                                    est_runtime=3600))
    daemon.approve(held.id)
    daemon.tick()

    clock.at(0)
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
    clock = Clock()
    daemon, providers = build(args.data, clock)
    if args.seed:
        seed(daemon, providers, clock)
    asyncio.run(serve(daemon, args.port, daemon.cfg.tick))


if __name__ == "__main__":
    main()
