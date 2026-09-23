import datetime as dt

import pytest

from pasar.cloud.cost import Ledger, estimate, hourly_rate
from pasar.db import Store
from pasar.models import Attempt, JobSpec
from tests.fakes import FakeClock

RATES = {"gpu_hour_cost_h100": 3.95, "cpu_hour_cost_sandbox": 0.1419,
         "mem_gib_hour_cost_sandbox": 0.024}


def test_rate_counts_gpus_cpu_and_memory():
    r = hourly_rate(RATES, "H100", 2, cpu=4.0, mem_gib=32.0)
    assert r == pytest.approx(2 * 3.95 + 4 * 0.1419 + 32 * 0.024)


def test_rate_scales_with_gpu_count():
    one = hourly_rate(RATES, "H100", 1, cpu=0.0, mem_gib=0.0)
    three = hourly_rate(RATES, "H100", 3, cpu=0.0, mem_gib=0.0)
    assert three == pytest.approx(3 * one)


def test_rate_is_case_insensitive_about_gpu_names():
    assert hourly_rate(RATES, "h100", 1) == hourly_rate(RATES, "H100", 1)


def test_unknown_gpu_raises_so_nothing_is_approved_blind():
    with pytest.raises(KeyError):
        hourly_rate(RATES, "B200", 1)


def test_missing_cpu_rate_raises():
    rates = {"gpu_hour_cost_h100": 3.95, "mem_gib_hour_cost_sandbox": 0.024}
    with pytest.raises(KeyError):
        hourly_rate(rates, "H100", 1)


def test_missing_mem_rate_raises():
    rates = {"gpu_hour_cost_h100": 3.95, "cpu_hour_cost_sandbox": 0.1419}
    with pytest.raises(KeyError):
        hourly_rate(rates, "H100", 1)


def test_zero_gpu_rate_raises():
    rates = {**RATES, "gpu_hour_cost_h100": 0.0}
    with pytest.raises(KeyError):
        hourly_rate(rates, "H100", 1)


def test_negative_cpu_rate_raises():
    rates = {**RATES, "cpu_hour_cost_sandbox": -0.1}
    with pytest.raises(KeyError):
        hourly_rate(rates, "H100", 1)


def test_estimate_is_rate_times_hours():
    assert estimate(4.0, 5400) == pytest.approx(6.0)


def test_estimate_of_zero_duration_is_zero():
    assert estimate(4.0, 0) == 0.0


def test_estimate_of_negative_duration_is_zero():
    assert estimate(4.0, -100) == 0.0


# ---- Ledger

def spec(**kw):
    return JobSpec(**{"command": "python a.py", "est_runtime": 3600, "cwd": "/tmp", **kw})


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "pasar.db")


@pytest.fixture
def clock():
    return FakeClock(1_700_000_000.0)


@pytest.fixture
def ledger(store, clock):
    return Ledger(store, clock)


def test_empty_ledger_reports_zero(ledger):
    assert ledger.committed("modal") == 0.0
    assert ledger.spent_day("modal") == 0.0
    assert ledger.spent_month("modal") == 0.0


def test_record_and_spent_day_sums_todays_rows(store, ledger, clock):
    j1 = store.insert_job(spec(), 1000, clock(), None)
    j2 = store.insert_job(spec(), 1000, clock(), None)
    ledger.record("modal", j1, 1, 5.0)
    ledger.record("modal", j2, 1, 2.5)
    ledger.record("other", j1, 1, 100.0)
    assert ledger.spent_day("modal") == pytest.approx(7.5)


def test_spent_month_sums_the_whole_month(store, ledger, clock):
    j1 = store.insert_job(spec(), 1000, clock(), None)
    ledger.record("modal", j1, 1, 5.0)
    clock.advance(10 * 86400)
    j2 = store.insert_job(spec(), 1000, clock(), None)
    ledger.record("modal", j2, 1, 2.5)
    assert ledger.spent_month("modal") == pytest.approx(7.5)
    assert ledger.spent_day("modal") == pytest.approx(2.5)


def test_record_overwrites_the_estimate_with_billed_when_given(store, ledger, clock):
    j = store.insert_job(spec(), 1000, clock(), None)
    ledger.record("modal", j, 1, 5.0)
    assert ledger.spent_day("modal") == pytest.approx(5.0)
    ledger.record("modal", j, 1, 5.0, billed=7.25)
    assert ledger.spent_day("modal") == pytest.approx(7.25)


def test_committed_never_drops_below_the_estimate_while_running(store, ledger, clock):
    """A running job is still billing, so its estimate can never be counted as already spent —
    not even once it is partway through its own estimated runtime, where elapsed/est_runtime
    alone would suggest a smaller number."""
    j = store.insert_job(spec(est_runtime=3600), 1000, clock(), None)
    store.insert_attempt(Attempt(j, 1, "cloud:modal:sb-1", clock()))
    ledger.record("modal", j, 1, 4.0)
    clock.advance(1800)  # half the estimated runtime has elapsed
    assert ledger.committed("modal") == pytest.approx(4.0)


def test_committed_grows_past_the_estimate_once_a_job_overruns_it(store, ledger, clock):
    j = store.insert_job(spec(est_runtime=3600), 1000, clock(), None)
    store.insert_attempt(Attempt(j, 1, "cloud:modal:sb-1", clock()))
    ledger.record("modal", j, 1, 4.0)
    clock.advance(7200)  # twice the estimated runtime has elapsed
    assert ledger.committed("modal") == pytest.approx(8.0)


def test_committed_treats_a_non_positive_est_runtime_as_full_estimate(store, ledger, clock):
    j = store.insert_job(spec(est_runtime=0), 1000, clock(), None)
    store.insert_attempt(Attempt(j, 1, "cloud:modal:sb-1", clock()))
    ledger.record("modal", j, 1, 4.0)
    clock.advance(9999)
    assert ledger.committed("modal") == pytest.approx(4.0)


def test_committed_ignores_finished_attempts(store, ledger, clock):
    j = store.insert_job(spec(est_runtime=3600), 1000, clock(), None)
    store.insert_attempt(Attempt(j, 1, "cloud:modal:sb-1", clock(), end_time=clock() + 100))
    ledger.record("modal", j, 1, 4.0)
    assert ledger.committed("modal") == 0.0


def test_committed_ignores_billed_rows(store, ledger, clock):
    j = store.insert_job(spec(est_runtime=3600), 1000, clock(), None)
    store.insert_attempt(Attempt(j, 1, "cloud:modal:sb-1", clock()))
    ledger.record("modal", j, 1, 4.0, billed=3.0)
    assert ledger.committed("modal") == 0.0


def test_record_keeps_billed_when_re_recorded_with_none(store, ledger, clock):
    """A restart re-running launch-time bookkeeping calls record() again with billed=None; a
    real billed figure already on the row must survive that, not revert to the estimate."""
    j = store.insert_job(spec(), 1000, clock(), None)
    ledger.record("modal", j, 1, 5.0, billed=7.25)
    ledger.record("modal", j, 1, 5.0)
    assert ledger.spent_day("modal") == pytest.approx(7.25)


def test_committed_ignores_a_stale_attempt_after_a_retry(store, ledger, clock):
    """A row recorded for attempt 1 must not still count once the job is on attempt 2."""
    j = store.insert_job(spec(est_runtime=3600), 1000, clock(), None)
    store.insert_attempt(Attempt(j, 1, "cloud:modal:sb-1", clock(), end_time=clock() + 10))
    ledger.record("modal", j, 1, 4.0)
    store.insert_attempt(Attempt(j, 2, "cloud:modal:sb-2", clock() + 10))
    assert ledger.committed("modal") == 0.0


def test_day_boundary_splits_spend_between_days(store, ledger, clock):
    """Days are local calendar days: build the boundary from naive local datetimes (Python
    interprets a naive `.timestamp()` using the host's own timezone, DST and all, for that
    date) rather than hardcoding UTC, so this stays correct wherever the daemon runs and needs
    no real wall-clock date."""
    midnight = dt.datetime(2026, 1, 2, 0, 0, 0)  # noqa: DTZ001 - naive on purpose, see docstring
    before = (midnight - dt.timedelta(seconds=1)).timestamp()
    after = (midnight + dt.timedelta(seconds=1)).timestamp()

    clock.t = before
    j1 = store.insert_job(spec(), 1000, clock(), None)
    ledger.record("modal", j1, 1, 5.0)

    clock.t = after
    j2 = store.insert_job(spec(), 1000, clock(), None)
    ledger.record("modal", j2, 1, 9.0)

    assert ledger.spent_day("modal") == pytest.approx(9.0)

    clock.t = before
    assert ledger.spent_day("modal") == pytest.approx(5.0)
