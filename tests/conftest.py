from pathlib import Path

import pytest

from pasar.cloud.bundle import BundleError
from pasar.cloud.executor import CloudExecutor
from pasar.config import CloudTarget, Config
from pasar.daemon import Daemon
from pasar.db import Store
from pasar.models import JobSpec
from tests.fakes import FakeClock, FakeExecutor, FakeProbe
from tests.fakes_cloud import FakeProvider, run_now

# The pasar checkout itself: a real git repository with a pyproject.toml and uv.lock right
# where build_bundle wants them, so API/CLI tests can submit a cloud job without setting up a
# throwaway repo fixture of their own.
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def executor():
    return FakeExecutor()


@pytest.fixture
def probe():
    return FakeProbe()


@pytest.fixture
def platform_check():
    """Stands in for the submit-time `uv sync --dry-run` gate, which shells out to uv. Records
    the project directories it was asked about; `check_platform` itself is tested for real in
    test_cloud_bundle.py."""
    class Recorder:
        def __init__(self):
            self.calls: list[str] = []
            self.error: str | None = None

        def __call__(self, project_dir: str) -> None:
            self.calls.append(project_dir)
            if self.error:
                raise BundleError(self.error)

    return Recorder()


@pytest.fixture
def daemon(tmp_path, clock, executor, probe, platform_check):
    data = tmp_path / "data"
    data.mkdir()
    return Daemon(Config(), Store(data / "pasar.db"), executor, probe, data, clock=clock,
                  platform_check=platform_check, background=run_now)


@pytest.fixture
def make_spec(tmp_path):
    def make(**kw):
        base = {"command": "python train.py", "est_runtime": 3600, "cwd": str(tmp_path)}
        return JobSpec(**{**base, **kw})
    return make


@pytest.fixture
def cloud_provider():
    return FakeProvider()


@pytest.fixture
def cloud_daemon(daemon, cloud_provider):
    """Wires a "fake" cloud target into the same `daemon` object other fixtures (e.g. `client`)
    already hold — `create_app` closes over `daemon` by reference, so a test that asks for both
    `client` and `cloud_daemon` gets a client backed by one daemon with cloud submits enabled,
    whichever fixture pytest happens to build first."""
    target = CloudTarget(name="fake", provider="fake", daily_budget=50.0, monthly_budget=300.0,
                         max_running=2, env_passthrough=["WANDB_API_KEY"])
    daemon.cfg.clouds = {"fake": target}
    daemon.executors["fake"] = CloudExecutor(cloud_provider, target, daemon.store, daemon.clock,
                                             daemon.job_dir)
    return daemon


@pytest.fixture
def cloud_cwd():
    return str(REPO_ROOT)
