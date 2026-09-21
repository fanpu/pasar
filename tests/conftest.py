import pytest

from pasar.config import Config
from pasar.daemon import Daemon
from pasar.db import Store
from pasar.models import JobSpec
from tests.fakes import FakeClock, FakeExecutor, FakeProbe


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
def daemon(tmp_path, clock, executor, probe):
    data = tmp_path / "data"
    data.mkdir()
    return Daemon(Config(), Store(data / "pasar.db"), executor, probe, data, clock=clock)


@pytest.fixture
def make_spec(tmp_path):
    def make(**kw):
        base = {"command": "python train.py", "est_runtime": 3600, "cwd": str(tmp_path)}
        return JobSpec(**{**base, **kw})
    return make
