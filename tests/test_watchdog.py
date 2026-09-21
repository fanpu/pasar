from pasar.config import Config
from pasar.units import GiB
from pasar.watchdog import MachineSample, Watchdog, over_limit, under_pressure

cfg = Config()
CALM = MachineSample(121 * GiB, 80 * GiB, 0.0)
STALLED = MachineSample(121 * GiB, 80 * GiB, 25.0)
LOW = MachineSample(121 * GiB, 5 * GiB, 0.0)


def test_under_pressure():
    assert not under_pressure(CALM, cfg)
    assert under_pressure(STALLED, cfg)
    assert under_pressure(LOW, cfg)


def test_over_limit():
    assert over_limit({1: 10, 2: 30, 3: 5}, {1: 12, 2: 20, 3: 5}) == {2: 10}


def test_kills_only_after_sustained_pressure_and_waits_between_kills():
    w = Watchdog(cfg)
    usage, limits = {1: 30, 2: 25}, {1: 20, 2: 20}
    assert w.check(0, STALLED, usage, limits) is None
    assert w.check(29, STALLED, usage, limits) is None
    assert w.check(30, STALLED, usage, limits) == 1  # furthest over its limit
    assert w.check(35, STALLED, {2: 25}, {2: 20}) is None  # cooling down
    assert w.check(60, STALLED, {2: 25}, {2: 20}) == 2


def test_pressure_resets_when_it_clears():
    w = Watchdog(cfg)
    w.check(0, STALLED, {1: 30}, {1: 20})
    w.check(20, CALM, {1: 30}, {1: 20})
    assert w.pressure_since is None
    assert w.check(40, STALLED, {1: 30}, {1: 20}) is None


def test_no_victim_when_nobody_is_over():
    w = Watchdog(cfg)
    w.check(0, STALLED, {1: 10}, {1: 20})
    assert w.check(100, STALLED, {1: 10}, {1: 20}) is None
