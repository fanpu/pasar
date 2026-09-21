from pasar.projection import project
from pasar.scheduler import Queued, Running

NOW = 1000.0


def test_backfill_then_whole_gpu_job():
    running = [Running(42, 1500, NOW - 100, 37, True), Running(45, 1000, NOW - 9, 27, True)]
    queued = [Queued(48, 800, 5.0, 44, True), Queued(46, 1000, 1.0, 105, True)]
    remaining = {42: 4560, 45: 1260, 48: 2700, 46: 5400}
    out = project(queued, running, remaining, {42: 0, 45: 0, 48: 5.0, 46: 1.0}, 105, NOW)
    assert out[45] == [(NOW - 9, NOW + 1260)]
    assert out[48] == [(NOW + 1260, NOW + 1260 + 2700)]  # fills the gap after #45
    assert out[46] == [(NOW + 4560, NOW + 4560 + 5400)]  # needs #42 gone


def test_projected_preemption_splits_segments():
    running = [Running(1, 900, NOW, 60, True)]
    queued = [Queued(2, 1000, 1.0, 50, True)]
    out = project(queued, running, {1: 600, 2: 300}, {1: 0.0, 2: 1.0}, 100, NOW)
    assert out[2] == [(NOW, NOW + 300)]
    assert out[1] == [(NOW, NOW), (NOW + 300, NOW + 900)]


def test_job_that_never_fits_has_no_segments():
    out = project([Queued(1, 1000, 1.0, 500, True)], [], {1: 60}, {1: 1.0}, 100, NOW)
    assert out[1] == []
