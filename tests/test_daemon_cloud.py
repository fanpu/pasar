"""Cloud jobs through the daemon: approval, the cloud lane, and how a cloud attempt ends.

Local behaviour is covered by test_daemon_tick.py and test_daemon_preempt.py; what matters here
is that a cloud job never touches the local memory pool, never costs money without a person
saying so, and never leaves a sandbox running that nothing owns.
"""

import json
import logging
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from pasar.cloud.cost import estimate, hourly_rate
from pasar.cloud.executor import parse_unit
from pasar.cloud.modal_provider import ModalProvider
from pasar.config import CloudTarget, Config
from pasar.daemon import CLOUD_RATE_TTL, PAUSE_LIMIT, PULL_SETTLE, Conflict, Daemon
from pasar.db import Store
from pasar.executor.base import UnitState
from pasar.models import EndKind, JobSpec, State
from pasar.units import GiB
from pasar.views import cloud_status_view, cloud_view
from tests.fakes_cloud import FakeProvider, launch_request, run_now
from tests.fakes_modal import FakeSDK


class StubMetrics:
    """Stands in for the GPU metric recorder, which only knows about the local machine."""

    def __init__(self):
        self.recorded: list[tuple[int, int]] = []

    def record(self, job_id, attempt, start, end):
        self.recorded.append((job_id, attempt))


@pytest.fixture
def repo(tmp_path):
    """A git repository with a lockfile: the least a bundle needs."""
    d = tmp_path / "repo"
    d.mkdir()
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    (d / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0"\n')
    (d / "uv.lock").write_text("version = 1\n")
    (d / "train.py").write_text("print('hi')\n")
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    return d


@pytest.fixture
def make_cloud(tmp_path, clock, executor, probe, platform_check):
    data = tmp_path / "data"
    data.mkdir()

    def make(provider=None, metrics=None, **target_kw):
        provider = provider or FakeProvider()
        base = {"name": "fake", "provider": "fake", "daily_budget": 50.0,
                "monthly_budget": 300.0, "max_running": 2,
                "env_passthrough": ["WANDB_API_KEY"]}
        cfg = Config(clouds={"fake": CloudTarget(**{**base, **target_kw})})
        daemon = Daemon(cfg, Store(data / "pasar.db"), executor, probe, data, clock=clock,
                        metrics=metrics, providers={"fake": provider},
                        platform_check=platform_check, background=run_now)
        # A finished job's automatic pull runs as soon as it is due, and here it is due at
        # once: most tests are about what a pull does, not when. The settle delay has tests of
        # its own, which put `PULL_SETTLE` back.
        daemon.pull_settle = 0
        return daemon, provider

    return make


@pytest.fixture
def cloud(make_cloud):
    return make_cloud()


def cloud_spec(repo, **kw):
    base = {"command": "python train.py", "est_runtime": 3600, "cwd": str(repo),
            "target": "fake", "gpu": "H100"}
    return JobSpec(**{**base, **kw})


def handle_of(daemon, job_id):
    return parse_unit(daemon.store.current_attempt(job_id).unit)[1]


def ctl(daemon, job_id, obj, attempt=1):
    """A wrapper control line for this attempt, signed with the token the daemon generated."""
    token = json.loads((daemon.job_dir(job_id) / f"cloud-{attempt}.json").read_text())["token"]
    return "\x1epasar:" + token + " " + json.dumps(obj) + "\n"


def start(daemon, repo, **kw):
    """Submit, approve and launch one cloud job; returns its id."""
    job = daemon.submit(cloud_spec(repo, **kw))
    daemon.approve(job.id)
    daemon.tick()
    return job.id


# ---- submit and approval

def test_cloud_submit_lands_in_awaiting_and_launches_nothing(cloud, repo):
    daemon, provider = cloud
    job = daemon.submit(cloud_spec(repo))
    assert job.state == State.AWAITING
    assert (daemon.job_dir(job.id) / "bundle.tar").exists()
    daemon.tick()
    assert daemon.job(job.id).state == State.AWAITING
    assert provider.boxes == {}


def test_approve_moves_it_to_queued_and_the_next_tick_launches_it(cloud, repo, clock):
    daemon, provider = cloud
    job = daemon.submit(cloud_spec(repo))
    clock.advance(60)
    approved = daemon.approve(job.id)
    assert approved.state == State.QUEUED and approved.queue_time == clock.t
    assert daemon.store.approvals(job.id)[0]["attempt"] == 1
    daemon.tick()
    assert daemon.job(job.id).state == State.RUNNING
    unit = daemon.store.current_attempt(job.id).unit
    assert parse_unit(unit) == ("fake", "sb-1")
    assert provider.boxes["sb-1"].req.gpu == "H100"
    spend = daemon.store.cloud_spend("fake")
    assert len(spend) == 1 and spend[0]["estimated"] > 0


def test_reject_cancels_it(cloud, repo):
    daemon, provider = cloud
    job = daemon.submit(cloud_spec(repo))
    assert daemon.reject(job.id).state == State.CANCELLED
    daemon.tick()
    assert provider.boxes == {} and daemon.job(job.id).reason == "rejected"


def test_approve_and_reject_only_apply_to_awaiting_jobs(cloud, repo):
    daemon, _ = cloud
    job = daemon.submit(cloud_spec(repo))
    daemon.approve(job.id)
    with pytest.raises(Conflict):
        daemon.approve(job.id)
    with pytest.raises(Conflict):
        daemon.reject(job.id)


def test_unapproved_job_expires_after_approval_ttl(make_cloud, repo, clock):
    daemon, provider = make_cloud(approval_ttl=600)
    job = daemon.submit(cloud_spec(repo))
    clock.advance(599)
    daemon.tick()
    assert daemon.job(job.id).state == State.AWAITING
    clock.advance(2)
    daemon.tick()
    assert daemon.job(job.id).state == State.CANCELLED
    assert daemon.job(job.id).reason == "approval_expired"
    assert provider.boxes == {}


def test_cancelling_an_awaiting_job_does_not_leave_it_stopping(cloud, repo):
    daemon, _ = cloud
    job = daemon.submit(cloud_spec(repo))
    assert daemon.cancel(job.id).state == State.CANCELLED


def test_submit_rejects_unknown_target_and_missing_gpu(cloud, repo):
    daemon, _ = cloud
    with pytest.raises(ValueError, match="target"):
        daemon.submit(cloud_spec(repo, target="nowhere"))
    with pytest.raises(ValueError, match="gpu"):
        daemon.submit(cloud_spec(repo, gpu=None))
    with pytest.raises(ValueError, match="mem"):
        daemon.submit(cloud_spec(repo, mem_request=8 * GiB))


def test_submit_rejects_a_gpu_the_target_has_no_price_for(cloud, repo):
    daemon, _ = cloud
    with pytest.raises(ValueError, match="price"):
        daemon.submit(cloud_spec(repo, gpu="B200"))


def test_a_lockfile_that_cannot_resolve_for_the_cloud_is_refused_at_submit(make_cloud, repo,
                                                                           platform_check):
    # The gate costs a second of uv and saves renting a GPU for an image that could only ever
    # have failed to build.
    daemon, provider = make_cloud()
    platform_check.error = "this project's uv.lock does not resolve for x86_64-manylinux_2_28"
    with pytest.raises(ValueError, match="does not resolve"):
        daemon.submit(cloud_spec(repo))
    assert daemon.store.list_jobs() == []
    daemon.tick()
    assert provider.boxes == {}


def test_the_platform_gate_is_asked_once_per_environment(make_cloud, repo, platform_check):
    # It shells out to uv, and a sweep is one submit per configuration: the answer only changes
    # when the lockfile does, which is exactly what the environment key hashes.
    daemon, _ = make_cloud()
    daemon.submit(cloud_spec(repo))
    daemon.submit(cloud_spec(repo))
    assert len(platform_check.calls) == 1
    assert Path(platform_check.calls[0]).resolve() == repo.resolve()


def test_the_provider_is_not_asked_for_its_rates_once_per_price(make_cloud, repo, clock,
                                                                monkeypatch):
    # Almost nothing about a cloud job's price is stored, so a job view prices a capped job
    # twice and every tick prices each running capped job again: `pasar ls` over twenty of them
    # was forty provider round-trips inside one two-second tick.
    daemon, provider = make_cloud()
    calls = []
    real = FakeProvider.rates
    monkeypatch.setattr(provider, "rates", lambda: (calls.append(None), real(provider))[1])

    job_id = start(daemon, repo, max_cost=6.0)  # submit, approve and launch all price the job
    assert len(calls) == 1
    for _ in range(3):
        daemon.tick()
        daemon.cloud_estimate(daemon.job(job_id))
    assert len(calls) == 1  # still the one answer, within its lifetime

    clock.advance(CLOUD_RATE_TTL + 1)
    daemon.cloud_estimate(daemon.job(job_id))
    assert len(calls) == 2  # and asked again once it is stale


def test_cloud_gpus_reuses_the_same_cached_rates_cloud_rates_already_fetched(make_cloud,
                                                                             monkeypatch):
    # `cloud_status_view` calls `cloud_rates` and `cloud_gpus` for the same target on every
    # `pasar cloud` / `GET /api/cloud` request; `cloud_gpus` must not be a second provider
    # round-trip on top of the one `cloud_rates` already made.
    daemon, provider = make_cloud()
    target = daemon.cfg.clouds["fake"]
    calls = []
    real = FakeProvider.rates
    monkeypatch.setattr(provider, "rates", lambda: (calls.append(None), real(provider))[1])

    daemon.cloud_rates(target)
    assert len(calls) == 1
    daemon.cloud_gpus(target)
    assert len(calls) == 1  # cloud_gpus read the cache cloud_rates just filled, not a new call


def test_cloud_gpus_is_empty_not_an_error_when_rates_fail(make_cloud, monkeypatch):
    daemon, provider = make_cloud()
    target = daemon.cfg.clouds["fake"]

    def boom():
        raise RuntimeError("the provider is unreachable")
    monkeypatch.setattr(provider, "rates", boom)
    assert daemon.cloud_gpus(target) == []


def test_a_local_submit_never_shells_out_to_uv(cloud, tmp_path, platform_check):
    daemon, _ = cloud
    daemon.submit(JobSpec(command="python train.py", est_runtime=60, cwd=str(tmp_path)))
    assert platform_check.calls == []


def test_an_unpackageable_job_is_refused_as_a_bad_request(cloud, tmp_path):
    # A BundleError would reach the API as a server error; the submitter is the one who can fix
    # this, so it has to arrive as a rejected request.
    daemon, _ = cloud
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    with pytest.raises(ValueError, match="git repository"):
        daemon.submit(cloud_spec(plain))
    assert daemon.store.list_jobs() == []


def test_approving_on_a_target_without_a_provider_is_refused(make_cloud, repo, tmp_path, clock,
                                                             executor, probe):
    # Nothing would launch it and nothing would expire it: it would sit in the queue for good.
    daemon, _ = make_cloud()
    job = daemon.submit(cloud_spec(repo))
    data = tmp_path / "data"
    cfg = Config(clouds={"fake": CloudTarget(name="fake", provider="fake", daily_budget=50.0,
                                             monthly_budget=300.0)})
    without = Daemon(cfg, Store(data / "pasar.db"), executor, probe, data, clock=clock)
    with pytest.raises(Conflict, match="no provider"):
        without.approve(job.id)
    assert without.job(job.id).state == State.AWAITING


def test_an_approval_that_cannot_be_priced_is_refused_and_writes_nothing(cloud, repo, clock,
                                                                         monkeypatch):
    # A row with a NULL price is an approval nobody could have looked at, and its NULL
    # hourly_rate also switches off the price-rise guard, so the job would later launch at
    # whatever the market asked.
    daemon, provider = cloud
    job = daemon.submit(cloud_spec(repo))
    monkeypatch.setattr(provider, "rates", dict)  # the provider stops reporting prices
    clock.advance(60)
    with pytest.raises(Conflict, match="could not be priced"):
        daemon.approve(job.id)
    assert daemon.job(job.id).state == State.AWAITING
    assert daemon.store.approvals(job.id) == []
    daemon.tick()
    assert provider.boxes == {} and daemon.store.cloud_spend("fake") == []


def test_a_target_without_a_provider_is_refused_rather_than_called_unknown(tmp_path, clock,
                                                                          executor, probe, repo):
    data = tmp_path / "no-provider"
    data.mkdir()
    cfg = Config(clouds={"fake": CloudTarget(name="fake", provider="fake", daily_budget=50.0,
                                             monthly_budget=300.0)})
    daemon = Daemon(cfg, Store(data / "pasar.db"), executor, probe, data, clock=clock)
    with pytest.raises(ValueError, match="no provider"):
        daemon.submit(cloud_spec(repo))


def test_cloud_jobs_reject_restart_and_retries(cloud, repo):
    daemon, provider = cloud
    with pytest.raises(ValueError, match="retries"):
        daemon.submit(cloud_spec(repo, retries=2))
    job_id = start(daemon, repo)
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()
    assert daemon.job(job_id).state == State.COMPLETED
    with pytest.raises(Conflict, match="submit"):
        daemon.restart(job_id)


def test_only_allowed_environment_variables_reach_the_container(cloud, repo, monkeypatch):
    daemon, provider = cloud
    monkeypatch.setenv("WANDB_API_KEY", "from-the-daemon")
    job_id = start(daemon, repo, env_keys=["MY_VAR"],
                   env={"MY_VAR": "v", "SECRET": "s", "AWS_SECRET_ACCESS_KEY": "nope"})
    env = provider.boxes[handle_of(daemon, job_id)].req.env
    assert env["MY_VAR"] == "v" and env["WANDB_API_KEY"] == "from-the-daemon"
    assert "SECRET" not in env and "AWS_SECRET_ACCESS_KEY" not in env
    assert env["PASAR_ATTEMPT"] == "1" and env["PASAR_RESUMING"] == "0"


# ---- staying out of the local lane

def test_cloud_job_does_not_consume_local_memory_or_block_local_jobs(cloud, repo, tmp_path,
                                                                     executor):
    daemon, _ = cloud
    job_id = start(daemon, repo)
    local = daemon.submit(JobSpec(command="python x.py", est_runtime=60, cwd=str(tmp_path)))
    daemon.tick()
    assert daemon.job(local.id).state == State.RUNNING  # the whole GPU was still free
    assert job_id not in daemon.usage and job_id not in daemon.units
    assert job_id not in daemon.usage_history
    _, running = daemon.snapshot()
    assert [r.job_id for r in running] == [local.id]
    assert executor.launched[0].unit == f"pasar-job-{local.id}-1"


def test_watchdog_ignores_cloud_jobs(cloud, repo, probe, clock):
    daemon, provider = cloud
    job_id = start(daemon, repo)
    probe.mem_available = 1 * GiB  # sustained pressure, well under the margin
    probe.psi = 50.0
    for _ in range(3):
        clock.advance(30)
        daemon.tick()
    assert daemon.job(job_id).state == State.RUNNING
    assert daemon.usage == {} and provider.terminated == []
    assert [e for e in daemon.store.machine_events() if e["kind"] == "oom_kill"] == []


def test_local_and_cloud_run_at_the_same_time(cloud, repo, tmp_path, executor):
    daemon, provider = cloud
    job = daemon.submit(cloud_spec(repo))
    daemon.approve(job.id)
    local = daemon.submit(JobSpec(command="python x.py", est_runtime=60, cwd=str(tmp_path)))
    daemon.tick()
    assert daemon.job(job.id).state == State.RUNNING
    assert daemon.job(local.id).state == State.RUNNING
    assert len(provider.boxes) == 1 and len(executor.launched) == 1


# ---- how a cloud attempt ends

def test_cloud_exit_is_diagnosed_from_the_log_tail(cloud, repo):
    daemon, provider = cloud
    job_id = start(daemon, repo)
    h = handle_of(daemon, job_id)
    provider.emit(h, "loading\ntorch.OutOfMemoryError: CUDA out of memory\n")
    provider.emit(h, ctl(daemon, job_id, {"t": "exit", "code": 1, "signal": None, "reason": None}))
    daemon.tick()
    job = daemon.job(job_id)
    assert job.state == State.FAILED and job.reason == "gpu_oom"
    assert "CUDA out of memory" in daemon.store.attempts(job_id)[0].log_tail


def test_a_local_gpu_error_is_not_blamed_on_a_cloud_job(cloud, repo, probe):
    daemon, provider = cloud
    job_id = start(daemon, repo)
    probe.xid = 3  # the local GPU threw Xid errors; the cloud job never touched it
    provider.emit(handle_of(daemon, job_id),
                  ctl(daemon, job_id, {"t": "exit", "code": 2, "signal": None, "reason": None}))
    daemon.tick()
    assert daemon.job(job_id).reason == "exit"


def test_provider_reclaim_ends_paused_and_returns_to_awaiting(cloud, repo):
    daemon, provider = cloud
    job_id = start(daemon, repo)
    provider.reclaim(handle_of(daemon, job_id))
    daemon.tick()
    job = daemon.job(job_id)
    assert job.state == State.AWAITING and job.reason == "cloud_preempted"
    assert job.retries_used == 0
    assert daemon.store.attempts(job_id)[0].end_kind == EndKind.PAUSED
    daemon.tick()  # nothing restarts it without a person
    assert len(daemon.store.attempts(job_id)) == 1


def test_time_limit_ends_paused_and_returns_to_awaiting(cloud, repo):
    daemon, provider = cloud
    job_id = start(daemon, repo)
    provider.emit(handle_of(daemon, job_id),
                  ctl(daemon, job_id, {"t": "exit", "code": None, "signal": "SIGTERM",
                                       "reason": "time_limit"}))
    daemon.tick()
    job = daemon.job(job_id)
    assert job.state == State.AWAITING and job.reason == "time_limit"
    assert daemon.store.attempts(job_id)[0].end_kind == EndKind.PAUSED


def test_approving_a_paused_job_resumes_it_as_a_new_attempt(cloud, repo):
    daemon, provider = cloud
    job_id = start(daemon, repo)
    provider.reclaim(handle_of(daemon, job_id))
    daemon.tick()
    daemon.approve(job_id)
    daemon.tick()
    assert daemon.job(job_id).state == State.RUNNING
    assert len(daemon.store.attempts(job_id)) == 2
    env = provider.boxes[handle_of(daemon, job_id)].req.env
    assert env["PASAR_ATTEMPT"] == "2"


def test_a_second_cloud_attempt_is_told_it_may_have_a_checkpoint(cloud, repo):
    """The persist dir outlives the attempt, so the second one really may find a checkpoint —
    and a job that is not told will redo everything the first attempt paid for."""
    daemon, provider = cloud
    job_id = start(daemon, repo)
    first = handle_of(daemon, job_id)
    provider.reclaim(first)
    daemon.tick()
    daemon.approve(job_id)
    daemon.tick()
    assert daemon.job(job_id).state == State.RUNNING
    second = handle_of(daemon, job_id)
    assert first != second
    assert provider.boxes[first].req.env["PASAR_RESUMING"] == "0"
    assert provider.boxes[second].req.env["PASAR_RESUMING"] == "1"


def out_of_time(daemon, provider, job_id, attempt):
    """The wrapper's exit line for an attempt that ran out of its approved time."""
    provider.emit(handle_of(daemon, job_id),
                  ctl(daemon, job_id, {"t": "exit", "code": None, "signal": "SIGTERM",
                                       "reason": "time_limit"}, attempt=attempt))


def test_pausing_does_not_loop_forever(cloud, repo):
    daemon, provider = cloud
    job_id = start(daemon, repo)
    for n in range(1, PAUSE_LIMIT):
        out_of_time(daemon, provider, job_id, n)
        daemon.tick()
        assert daemon.job(job_id).state == State.AWAITING
        daemon.approve(job_id)
        daemon.tick()
    out_of_time(daemon, provider, job_id, PAUSE_LIMIT)
    daemon.tick()
    job = daemon.job(job_id)
    assert job.state == State.FAILED and job.reason == "pause_limit"


def test_a_provider_reclaim_does_not_count_toward_the_pause_limit(make_cloud, repo):
    # A reclaim is not the job's fault — that is why it consumes no retry — so a checkpointing
    # job on a spot-style provider must not burn its pause budget on the provider's behalf.
    # The budget is raised so that only the pause limit is under test here.
    daemon, provider = make_cloud(daily_budget=1000.0, monthly_budget=5000.0)
    job_id = start(daemon, repo)
    for _ in range(PAUSE_LIMIT + 2):
        provider.reclaim(handle_of(daemon, job_id))
        daemon.tick()
        assert daemon.job(job_id).state == State.AWAITING
        daemon.approve(job_id)
        daemon.tick()
    assert daemon.job(job_id).state == State.RUNNING
    # and the pauses that are the job's own still count, over its whole life
    attempt = PAUSE_LIMIT + 3  # the one running now, after PAUSE_LIMIT + 2 reclaims
    for _ in range(PAUSE_LIMIT - 1):
        out_of_time(daemon, provider, job_id, attempt)
        daemon.tick()
        assert daemon.job(job_id).state == State.AWAITING
        daemon.approve(job_id)
        daemon.tick()
        attempt += 1
    out_of_time(daemon, provider, job_id, attempt)
    daemon.tick()
    assert daemon.job(job_id).reason == "pause_limit"


def test_the_daemon_stops_a_cloud_job_at_its_approved_run_time(make_cloud, repo, clock):
    daemon, provider = make_cloud(timeout_factor=1.5)
    job_id = start(daemon, repo)
    h = handle_of(daemon, job_id)
    assert provider.boxes[h].req.command.split(" --limit ")[1].startswith("86220")
    clock.advance(5399)
    daemon.tick()
    assert provider.stopped == []
    clock.advance(2)
    daemon.tick()
    assert provider.stopped == [h] and daemon.job(job_id).state == State.STOPPING
    provider.emit(h, ctl(daemon, job_id, {"t": "exit", "code": 143, "signal": None,
                                          "reason": "stopped"}))
    daemon.tick()
    job = daemon.job(job_id)
    assert job.state == State.AWAITING and job.reason == "time_limit"
    assert daemon.store.attempts(job_id)[0].end_kind == EndKind.PAUSED


def test_a_max_cost_stops_the_job_before_its_full_window(make_cloud, repo, clock):
    # A dollar cap only means something if it takes time away: at $5.278/h a $6 cap buys 4092s,
    # where the target's own window would have run the same job for 5400s (1h estimate x 1.5).
    daemon, provider = make_cloud(timeout_factor=1.5)
    capped = start(daemon, repo, max_cost=6.0)
    plain = start(daemon, repo)
    clock.advance(4093)
    daemon.tick()
    assert daemon.job(capped).state == State.STOPPING
    assert provider.stopped == [handle_of(daemon, capped)]
    assert daemon.job(plain).state == State.RUNNING  # the cap, and only the cap, stopped it
    events = [e for e in daemon.store.machine_events() if e["kind"] == "max_cost"]
    assert len(events) == 1 and "$6.00" in events[0]["text"]
    clock.advance(5401 - 4093)
    daemon.tick()
    assert daemon.job(plain).state == State.STOPPING


def test_a_rate_rise_mid_run_shortens_what_a_capped_job_may_still_run(make_cloud, repo, clock,
                                                                      monkeypatch):
    # `--max-cost` is a spend ceiling, not a run time somebody agreed to. If the rate rises while
    # the attempt runs, the same dollars buy less time, and the pause has to move with them or the
    # job bills straight past the cap. (The *window* an approval bought stays frozen — that is
    # what the person agreed to; only the cap is re-derived, and enforcement is the lesser.)
    daemon, provider = make_cloud()
    job_id = start(daemon, repo, max_cost=6.0)
    started = daemon.store.current_attempt(job_id).start_time
    h = handle_of(daemon, job_id)
    at_the_old_rate = 6.0 / hourly_rate(provider.rates(), "h100", 1) * 3600  # 4092s
    monkeypatch.setattr(provider, "rates", lambda: {"gpu_hour_cost_h100": 7.90,
                                                    "cpu_hour_cost_sandbox": 0.14,
                                                    "mem_gib_hour_cost_sandbox": 0.024})
    at_the_new_rate = 6.0 / hourly_rate(provider.rates(), "h100", 1) * 3600  # 2340s
    clock.t = started + at_the_new_rate - 1
    daemon.tick()
    assert daemon.job(job_id).state == State.RUNNING  # the cap's dollars are not spent yet
    clock.advance(2)
    daemon.tick()
    assert clock.t - started < at_the_old_rate  # the frozen window has not run out; the cap has
    assert daemon.job(job_id).state == State.STOPPING
    assert provider.stopped == [h]
    events = [e for e in daemon.store.machine_events() if e["kind"] == "max_cost"]
    assert len(events) == 1 and "$6.00" in events[0]["text"]


def test_a_capped_job_cannot_bill_more_than_the_ledger_committed(make_cloud, repo, clock):
    # The budget gate admits jobs against `committed()`; for a capped job that figure is the cap,
    # so the cap has to be what the job can actually bill, not a smaller number recorded next to
    # an attempt still free to run its full window.
    daemon, provider = make_cloud()
    job_id = start(daemon, repo, max_cost=6.0)
    committed = daemon.ledger.committed("fake")
    assert committed == pytest.approx(6.0)
    rate = hourly_rate(provider.rates(), "h100", 1)
    started = daemon.store.current_attempt(job_id).start_time
    clock.advance(6.0 / rate * 3600 + 1)  # one second past the run time $6 buys
    daemon.tick()
    assert daemon.job(job_id).state == State.STOPPING
    assert estimate(rate, clock.t - started) == pytest.approx(committed, abs=0.01)


def test_a_rate_rise_bounces_a_capped_job_too(cloud, repo, monkeypatch, clock):
    # The cap and the price-rise guard are separate: pricing the relaunch against the cap would
    # compare the submitter's own number with itself and launch at any rate at all.
    daemon, provider = cloud
    job = daemon.submit(cloud_spec(repo, max_cost=6.0))
    daemon.approve(job.id)
    monkeypatch.setattr(provider, "rates", lambda: {"gpu_hour_cost_h100": 7.90,
                                                    "cpu_hour_cost_sandbox": 0.14,
                                                    "mem_gib_hour_cost_sandbox": 0.024})
    clock.advance(60)
    daemon.tick()
    job = daemon.job(job.id)
    assert job.state == State.AWAITING and job.reason == "price_rose"
    assert provider.boxes == {} and daemon.store.cloud_spend("fake") == []


def test_a_job_that_finishes_inside_the_pause_window_is_not_paused(make_cloud, repo, clock):
    # The daemon asked for a pause and the job finished before it took effect. Recording that as
    # paused sends a finished job back for approval and pays to run it a second time.
    daemon, provider = make_cloud(timeout_factor=1.0)
    job_id = start(daemon, repo)
    h = handle_of(daemon, job_id)
    clock.advance(3601)
    daemon.tick()
    assert provider.stopped == [h] and daemon.job(job_id).state == State.STOPPING
    provider.emit(h, ctl(daemon, job_id, {"t": "exit", "code": 0, "signal": None, "reason": None}))
    daemon.tick()
    job = daemon.job(job_id)
    assert job.state == State.COMPLETED and job.reason is None
    assert daemon.store.attempts(job_id)[0].end_kind == EndKind.COMPLETED


def test_a_cancel_still_beats_a_clean_exit(cloud, repo):
    daemon, provider = cloud
    job_id = start(daemon, repo)
    h = handle_of(daemon, job_id)
    daemon.cancel(job_id)
    provider.emit(h, ctl(daemon, job_id, {"t": "exit", "code": 0, "signal": None, "reason": None}))
    daemon.tick()
    assert daemon.job(job_id).state == State.CANCELLED


def test_an_overdue_job_is_paused_even_when_its_status_cannot_be_read(make_cloud, repo, clock,
                                                                      monkeypatch):
    # The approved run time is the daemon's promise, not the provider's: a status read that
    # threw this tick must not buy the attempt another tick of billing.
    daemon, provider = make_cloud(timeout_factor=1.0)
    job_id = start(daemon, repo)
    h = handle_of(daemon, job_id)

    def boom(_unit):
        raise RuntimeError("the provider is not answering")

    monkeypatch.setattr(daemon.executors["fake"], "status", boom)
    clock.advance(3601)
    daemon.tick()
    assert provider.stopped == [h] and daemon.job(job_id).state == State.STOPPING


def test_a_sick_provider_does_not_let_a_stopped_sandbox_bill_to_its_own_timeout(
        make_cloud, repo, clock, monkeypatch):
    # Every stop is only a request; the deadline behind it is what ends the sandbox. If that
    # lived behind a provider status call, a provider outage would let a sandbox the daemon
    # already knows is overdue bill on to the 24h ceiling.
    daemon, provider = make_cloud(timeout_factor=1.0)
    job_id = start(daemon, repo)
    h = handle_of(daemon, job_id)

    def boom(_handle):
        raise RuntimeError("the provider is not answering")

    clock.advance(3601)
    daemon.tick()
    assert provider.stopped == [h] and provider.terminated == []
    monkeypatch.setattr(provider, "status", boom)
    clock.advance(20000)
    daemon.tick()
    assert provider.terminated == [h]


def test_metrics_are_not_recorded_for_a_cloud_attempt(make_cloud, repo, tmp_path, executor):
    # The recorder summarises this machine's GPU over the attempt's window, which says nothing
    # about a job that ran somewhere else.
    metrics = StubMetrics()
    daemon, provider = make_cloud(metrics=metrics)
    job_id = start(daemon, repo)
    provider.finish(handle_of(daemon, job_id), 0)
    local = daemon.submit(JobSpec(command="python x.py", est_runtime=60, cwd=str(tmp_path)))
    daemon.tick()
    assert daemon.job(job_id).state == State.COMPLETED
    executor.exit(f"pasar-job-{local.id}-1")
    daemon.tick()
    assert daemon.job(local.id).state == State.COMPLETED
    assert metrics.recorded == [(local.id, 1)]


def test_cancelling_a_running_cloud_job_stops_it(cloud, repo):
    daemon, provider = cloud
    job_id = start(daemon, repo)
    h = handle_of(daemon, job_id)
    daemon.cancel(job_id)
    assert provider.stopped == [h]
    provider.finish(h, 143)
    daemon.tick()
    assert daemon.job(job_id).state == State.CANCELLED
    assert h in provider.terminated  # the sandbox is not left billing


# ---- the lane

def test_budget_blocks_a_launch_without_failing_the_job(make_cloud, repo):
    daemon, provider = make_cloud(daily_budget=1.0)
    job = daemon.submit(cloud_spec(repo))
    daemon.approve(job.id)
    daemon.tick()
    assert daemon.job(job.id).state == State.QUEUED
    assert daemon.cloud_decisions["fake"].blocked == {job.id: "budget"}
    assert provider.boxes == {}


def test_a_running_job_is_counted_once_against_the_budget(make_cloud, repo, clock):
    # settled_day + committed, not spent_day + committed: the second double-counts the running
    # attempt and would block a job the budget can afford.
    daemon, provider = make_cloud(daily_budget=17.0, max_running=2)
    first = start(daemon, repo)
    clock.advance(60)
    second = daemon.submit(cloud_spec(repo))
    daemon.approve(second.id)
    daemon.tick()
    assert daemon.job(first).state == State.RUNNING
    assert daemon.job(second.id).state == State.RUNNING
    assert len(provider.boxes) == 2


def test_a_price_above_the_approved_ceiling_is_not_launched(cloud, repo, monkeypatch, clock):
    # The launch re-prices from the live rates; what a person agreed to pay is the approval's
    # ceiling, so a rate rise between the two has to go back to them rather than be charged.
    daemon, provider = cloud
    job = daemon.submit(cloud_spec(repo))
    daemon.approve(job.id)
    ceiling = daemon.store.approvals(job.id)[0]["max_cost"]
    monkeypatch.setattr(provider, "rates", lambda: {"gpu_hour_cost_h100": 7.90,
                                                    "cpu_hour_cost_sandbox": 0.14,
                                                    "mem_gib_hour_cost_sandbox": 0.024})
    clock.advance(60)
    daemon.tick()
    job = daemon.job(job.id)
    assert job.state == State.AWAITING and job.reason == "price_rose"
    assert job.queue_time == clock.t  # it waits for approval afresh
    assert provider.boxes == {} and daemon.store.cloud_spend("fake") == []
    events = [e for e in daemon.store.machine_events() if e["kind"] == "price_rise"]
    assert len(events) == 1 and f"{ceiling:.2f}" in events[0]["text"]
    daemon.approve(job.id)  # approving at the new price runs it
    daemon.tick()
    assert daemon.job(job.id).state == State.RUNNING
    assert daemon.store.cloud_spend("fake")[0]["estimated"] > ceiling


def test_a_steady_price_launches_at_the_approved_figure(cloud, repo):
    daemon, _ = cloud
    job_id = start(daemon, repo)
    assert daemon.store.cloud_spend("fake")[0]["estimated"] == pytest.approx(
        daemon.store.approvals(job_id)[0]["max_cost"])


def test_concurrency_cap_holds_the_rest_of_the_queue(make_cloud, repo):
    daemon, provider = make_cloud(max_running=1)
    start(daemon, repo)
    second = daemon.submit(cloud_spec(repo))
    daemon.approve(second.id)
    daemon.tick()
    assert daemon.job(second.id).state == State.QUEUED
    assert daemon.cloud_decisions["fake"].blocked == {second.id: "concurrency"}
    assert len(provider.boxes) == 1


def test_a_queued_cloud_job_says_what_holds_it_back(make_cloud, repo):
    # The cloud card shows an approved job still waiting to launch, and why, from this.
    daemon, _ = make_cloud(max_running=1)
    first = start(daemon, repo)
    second = daemon.submit(cloud_spec(repo))
    assert cloud_view(daemon, daemon.job(second.id))["blocked"] is None  # awaiting: not queued yet
    daemon.approve(second.id)
    daemon.tick()
    assert cloud_view(daemon, daemon.job(second.id))["blocked"] == "concurrency"
    assert cloud_view(daemon, daemon.job(first))["blocked"] is None  # running


# ---- startup and strays

def test_reconcile_adopts_running_cloud_attempts(make_cloud, repo):
    daemon, provider = make_cloud()
    job_id = start(daemon, repo)
    restarted, _ = make_cloud(provider=provider)
    restarted.reconcile()
    restarted.tick()
    assert restarted.job(job_id).state == State.RUNNING
    provider.finish(handle_of(restarted, job_id), 0)
    restarted.tick()
    assert restarted.job(job_id).state == State.COMPLETED


def test_without_reconcile_nothing_adopts_the_attempt(make_cloud, repo):
    daemon, provider = make_cloud()
    job_id = start(daemon, repo)
    restarted, _ = make_cloud(provider=provider)
    restarted.tick()
    assert restarted.job(job_id).state == State.FAILED  # the guard test for the one above


def test_reconcile_terminates_a_stray_cloud_handle(make_cloud, tmp_path):
    daemon, provider = make_cloud()
    req = replace(launch_request(tmp_path, job_id=99),
                  tags={"pasar_job": "99", "pasar_target": "fake"})
    handle = provider.launch(req)
    daemon.reconcile()
    assert provider.terminated == [handle]
    events = [e for e in daemon.store.machine_events() if e["kind"] == "stray_unit"]
    assert len(events) == 1 and handle in events[0]["text"]


def test_a_provider_outage_at_startup_does_not_take_pasard_down(make_cloud, repo, executor,
                                                                 monkeypatch):
    # reconcile() runs once, before the loop starts, and a cloud sweep reaches the provider.
    # Letting that throw would stop pasard starting at all, local jobs included.
    daemon, provider = make_cloud()
    job_id = start(daemon, repo)
    executor.units["pasar-job-99-1"] = UnitState("pasar-job-99-1", False, None, None, None, None)
    restarted, _ = make_cloud(provider=provider)

    def boom():
        raise RuntimeError("the provider is not answering")

    monkeypatch.setattr(provider, "list", boom)
    restarted.reconcile()  # must not raise
    strays = [e for e in restarted.store.machine_events() if e["kind"] == "stray_unit"]
    assert len(strays) == 1 and "pasar-job-99-1" in strays[0]["text"]  # the local sweep still ran
    restarted.tick()
    assert restarted.job(job_id).state == State.RUNNING  # its own attempt was still picked up


def test_an_attempt_that_cannot_be_adopted_is_ended_rather_than_left_billing(make_cloud, repo):
    # The saved state is what a restarted daemon follows the attempt by; without it nothing can
    # poll, stop or bill the sandbox, and the next tick settles the job as lost. Losing the job
    # and leaving the sandbox running until its own 24h timeout is the expensive half of that.
    daemon, provider = make_cloud()
    job_id = start(daemon, repo)
    handle = handle_of(daemon, job_id)
    (daemon.job_dir(job_id) / "cloud-1.json").unlink()
    restarted, _ = make_cloud(provider=provider)
    restarted.reconcile()
    assert provider.terminated == [handle]
    events = [e for e in restarted.store.machine_events() if e["kind"] == "orphan_unit"]
    assert len(events) == 1 and handle in events[0]["text"]
    restarted.tick()
    assert restarted.job(job_id).state == State.FAILED


def test_a_running_job_whose_target_is_gone_is_failed_loudly(make_cloud, repo, tmp_path, clock,
                                                             executor, probe):
    # The target was removed from the config while a job was running on it. Nothing here can
    # poll, stop or price that attempt again, so leaving it running means a job that reads as
    # running forever, an exception every tick, and budget committed to an unreachable target.
    daemon, _ = make_cloud()
    job_id = start(daemon, repo)
    unit = daemon.store.current_attempt(job_id).unit
    data = tmp_path / "data"
    after = Daemon(Config(), Store(data / "pasar.db"), executor, probe, data, clock=clock)
    after.reconcile()
    after.tick()
    job = after.job(job_id)
    assert job.state == State.FAILED and job.reason == "target_gone"
    att = after.store.current_attempt(job_id)
    assert att.end_time == clock.t and att.end_kind == EndKind.FAILED
    assert after.ledger.committed("fake") == 0
    events = [e for e in after.store.machine_events() if e["kind"] == "target_gone"]
    assert len(events) == 1 and unit in events[0]["text"]
    after.tick()  # said once, not every tick
    assert len([e for e in after.store.machine_events() if e["kind"] == "target_gone"]) == 1


def test_a_waiting_job_whose_target_is_gone_is_not_left_stranded(make_cloud, repo, tmp_path,
                                                                 clock, executor, probe):
    daemon, _ = make_cloud()
    waiting = daemon.submit(cloud_spec(repo))
    queued = daemon.submit(cloud_spec(repo))
    daemon.approve(queued.id)
    data = tmp_path / "data"
    after = Daemon(Config(), Store(data / "pasar.db"), executor, probe, data, clock=clock)
    after.tick()
    for job_id in (waiting.id, queued.id):
        job = after.job(job_id)
        assert job.state == State.CANCELLED and job.reason == "target_gone"


def fake_target(**kw):
    return CloudTarget(**{"name": "fake", "provider": "fake", "daily_budget": 50.0,
                          "monthly_budget": 300.0, **kw})


def without_its_provider(tmp_path, clock, executor, probe, **target_kw):
    """pasard restarted with the target still configured but its provider not built — a bad
    ~/.modal.toml, a package gone missing: `build_providers` logs it and leaves it out."""
    data = tmp_path / "data"
    cfg = Config(clouds={"fake": fake_target(**target_kw)})
    return Daemon(cfg, Store(data / "pasar.db"), executor, probe, data, clock=clock,
                  background=run_now)


def test_a_target_with_no_provider_says_so_rather_than_calling_itself_unconfigured(
        make_cloud, repo, tmp_path, clock, executor, probe):
    # Two different problems with two different fixes: a target taken out of the config wants
    # putting back, a target whose provider could not be set up wants whatever stopped it fixed
    # (often its credentials, not a missing package). Submit and approve take care to tell them
    # apart; so should the message somebody reads afterwards.
    daemon, _ = make_cloud()
    job_id = start(daemon, repo)
    after = without_its_provider(tmp_path, clock, executor, probe)
    after.tick()

    running = after.job(job_id)
    assert running.state == State.FAILED and "could not be set up" in running.summary
    assert "~/.modal.toml" in running.summary and "restart pasard" in running.summary
    [event] = [e["text"] for e in after.store.machine_events() if e["kind"] == "target_gone"]
    assert "could not be set up" in event and "no longer configured" not in event


def test_a_waiting_job_is_left_waiting_when_only_its_provider_failed(
        make_cloud, repo, tmp_path, clock, executor, probe):
    # The target is still configured; only its provider failed to start. Cancelling a paused
    # job here would stamp it finished with nothing able to pull it — and its checkpoint is what
    # its next attempt resumes from. Left as it is, it simply cannot move until the provider is
    # back: approve refuses, and the lane never launches on a target with no provider.
    daemon, provider = make_cloud()
    paused = pause_with_a_checkpoint(daemon, provider, repo)
    queued = daemon.submit(cloud_spec(repo))
    daemon.approve(queued.id)
    after = without_its_provider(tmp_path, clock, executor, probe)
    after.tick()
    after.tick()

    assert after.job(paused).state == State.AWAITING
    assert after.job(queued.id).state == State.QUEUED
    assert after.store.cloud_finished_at(paused) is None
    with pytest.raises(Conflict, match="no provider"):
        after.approve(paused)
    events = [e["text"] for e in after.store.machine_events() if e["kind"] == "target_gone"]
    assert len(events) == 2  # one per job, not one per tick
    assert all("left" in t and "cannot be approved or launched" in t for t in events)
    assert provider.persisted[paused] == {"checkpoint.pt": b"weights"}

    back, _ = make_cloud(provider)  # the provider is fixed and pasard restarted
    back.tick()
    assert back.job(queued.id).state == State.RUNNING
    assert back.approve(paused).state == State.QUEUED


def test_a_paused_job_cancelled_for_a_target_gone_says_what_it_left_behind(
        make_cloud, repo, tmp_path, clock, executor, probe):
    # "nothing was spent" is only true of a job that never ran.
    daemon, provider = make_cloud()
    paused = pause_with_a_checkpoint(daemon, provider, repo)
    never_ran = daemon.submit(cloud_spec(repo))
    data = tmp_path / "data"
    after = Daemon(Config(), Store(data / "pasar.db"), executor, probe, data, clock=clock)
    after.tick()

    job = after.job(paused)
    assert job.state == State.CANCELLED and "nothing was spent" not in job.summary
    assert "still on fake" in job.summary and "put fake back in the config" in job.summary
    assert "nothing was spent" in after.job(never_ran.id).summary


def test_a_sandbox_whose_attempt_cannot_be_recorded_is_ended(cloud, repo, monkeypatch):
    daemon, provider = cloud
    job = daemon.submit(cloud_spec(repo))
    daemon.approve(job.id)

    def boom(_attempt):
        raise RuntimeError("the database is having a moment")

    monkeypatch.setattr(daemon.store, "insert_attempt", boom)
    daemon.tick()
    assert provider.terminated == ["sb-1"]
    assert daemon.job(job.id).state == State.FAILED
    # And nothing is left on the budget: a spend row with no attempt beside it is closed the
    # moment it is written, and would take the day's budget at the full ceiling for a job that
    # never ran a second.
    assert daemon.store.cloud_spend("fake") == []
    assert daemon.ledger.spent_day("fake") == 0.0


# ---- settling what an attempt really cost

def test_a_short_attempt_is_billed_for_what_it_ran_not_its_ceiling(cloud, repo, clock):
    # The ledger holds the ceiling from launch, because before an attempt runs that is the only
    # figure there is. Left there it charges the month for time nobody used: a job approved for a
    # long window that finishes quickly would eat a small allowance a handful of jobs at a time.
    daemon, provider = cloud
    job_id = start(daemon, repo)
    ceiling = daemon.store.cloud_spend("fake")[0]["estimated"]
    rate = daemon.store.approvals(job_id)[0]["hourly_rate"]
    clock.advance(90)
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()

    assert daemon.job(job_id).state == State.COMPLETED
    row = daemon.store.cloud_spend("fake")[0]
    assert row["billed"] == pytest.approx(estimate(rate, 90), abs=1e-6)
    assert row["billed"] < ceiling / 5
    assert row["estimated"] == pytest.approx(ceiling)  # the estimate stays, for comparison


def test_settling_counts_the_attempt_once_and_frees_the_budget(cloud, repo, clock):
    # committed() prices an attempt while it runs and settled_day() once it stops; settling is
    # what moves it between them, so the two must never both count it.
    daemon, provider = cloud
    job_id = start(daemon, repo)
    assert daemon.ledger.committed("fake") > 0
    assert daemon.ledger.settled_day("fake") == 0
    clock.advance(90)
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()

    rate = daemon.store.approvals(job_id)[0]["hourly_rate"]
    assert daemon.ledger.committed("fake") == 0
    assert daemon.ledger.settled_day("fake") == pytest.approx(estimate(rate, 90), abs=1e-6)


def test_a_paused_attempt_is_settled_too(cloud, repo, clock):
    # A pause is not a refund: the attempt ran, and the next one is a fresh charge on top.
    daemon, provider = cloud
    job_id = start(daemon, repo)
    clock.advance(120)
    provider.emit(handle_of(daemon, job_id),
                  ctl(daemon, job_id, {"t": "exit", "code": 143, "reason": "time_limit"}) + "\n")
    provider.finish(handle_of(daemon, job_id), 143)
    daemon.tick()

    assert daemon.job(job_id).state == State.AWAITING
    row = daemon.store.cloud_spend("fake")[0]
    rate = daemon.store.approvals(job_id)[0]["hourly_rate"]
    assert row["billed"] == pytest.approx(estimate(rate, 120), abs=1e-6)


def test_a_settled_figure_is_never_replaced_by_a_later_estimate(cloud, repo, clock):
    # record() is called again whenever launch-time bookkeeping re-runs; it must not push a real
    # figure back up to a guess.
    daemon, provider = cloud
    job_id = start(daemon, repo)
    clock.advance(90)
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()
    billed = daemon.store.cloud_spend("fake")[0]["billed"]

    daemon.ledger.record("fake", job_id, 1, estimated=99.0)
    assert daemon.store.cloud_spend("fake")[0]["billed"] == pytest.approx(billed)


def test_a_job_the_budget_refuses_still_gets_to_ask_the_provider(make_cloud, repo):
    # The budget is pasar's arithmetic over estimates; the provider's refusal is the fact, and the
    # two have been seen disagreeing in both directions. With a spending limit at the provider,
    # the cheap way to settle it is to launch and find out.
    daemon, _provider = make_cloud(daily_budget=0.01, monthly_budget=0.01,
                                  probe_past_budget=True)
    job = daemon.submit(cloud_spec(repo))
    daemon.approve(job.id)
    daemon.tick()

    assert daemon.job(job.id).state == State.RUNNING
    assert daemon.cloud_decisions["fake"].probe == job.id
    events = daemon.store.machine_events(50)
    assert any(e["kind"] == "budget_probe" and str(job.id) in e["text"] for e in events)


def test_the_budget_still_blocks_when_the_provider_has_no_limit_of_its_own(make_cloud, repo):
    daemon, provider = make_cloud(daily_budget=0.01, monthly_budget=0.01)
    job = daemon.submit(cloud_spec(repo))
    daemon.approve(job.id)
    daemon.tick()

    assert daemon.job(job.id).state == State.QUEUED
    assert daemon.cloud_decisions["fake"].blocked == {job.id: "budget"}
    assert provider.boxes == {}


def test_only_one_job_probes_even_with_a_queue_behind_it(make_cloud, repo):
    daemon, provider = make_cloud(daily_budget=0.01, monthly_budget=0.01, max_running=4,
                                  probe_past_budget=True)
    first = daemon.submit(cloud_spec(repo))
    second = daemon.submit(cloud_spec(repo))
    daemon.approve(first.id)
    daemon.approve(second.id)
    daemon.tick()

    assert daemon.job(first.id).state == State.RUNNING
    assert daemon.job(second.id).state == State.QUEUED
    assert len(provider.boxes) == 1


# ---- pull

def finish(daemon, provider, repo, **kw):
    """Submit, approve, launch and complete one cloud job; returns its id."""
    job_id = start(daemon, repo, **kw)
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()
    assert daemon.job(job_id).state == State.COMPLETED
    return job_id


def test_pull_downloads_verifies_and_deletes_the_remote_copy(cloud, repo, tmp_path):
    daemon, provider = cloud
    job_id = finish(daemon, provider, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    provider.persist(job_id, "nested/metrics.json", b"{}")
    dest = tmp_path / "out"

    result = daemon.pull(job_id, dest=dest)

    assert result == {"job_id": job_id, "files": 2, "bytes": len(b"weights") + len(b"{}"),
                      "dest": str(dest), "deleted": True}
    assert (dest / "checkpoint.pt").read_bytes() == b"weights"
    assert (dest / "nested" / "metrics.json").read_bytes() == b"{}"
    assert provider.deleted_persist == [job_id]
    assert job_id not in provider.persisted
    events = daemon.store.machine_events(50)
    assert any(e["kind"] == "pulled" and str(job_id) in e["text"] and "deleted" in e["text"]
              for e in events)


def test_pull_keep_leaves_the_remote_copy(cloud, repo, tmp_path):
    daemon, provider = cloud
    job_id = finish(daemon, provider, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    dest = tmp_path / "out"

    result = daemon.pull(job_id, dest=dest, keep=True)

    assert result["deleted"] is False
    assert (dest / "checkpoint.pt").read_bytes() == b"weights"
    assert provider.deleted_persist == []
    assert job_id in provider.persisted
    events = daemon.store.machine_events(50)
    assert any(e["kind"] == "pulled" and "kept" in e["text"] for e in events)


def test_pull_default_destination_is_under_pull_dir(cloud, repo):
    daemon, provider = cloud
    job_id = finish(daemon, provider, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")

    result = daemon.pull(job_id)

    expected = daemon.data_dir / "pulls" / str(job_id)
    assert result["dest"] == str(expected)
    assert (expected / "checkpoint.pt").read_bytes() == b"weights"


def test_pull_nothing_on_the_volume_is_not_an_error(cloud, repo, tmp_path):
    daemon, provider = cloud
    job_id = finish(daemon, provider, repo)
    dest = tmp_path / "out"

    result = daemon.pull(job_id, dest=dest)

    assert result == {"job_id": job_id, "files": 0, "bytes": 0, "dest": None, "deleted": False}
    assert not dest.exists()
    assert provider.deleted_persist == []


@pytest.mark.parametrize("state_setup", ["awaiting", "queued", "running"])
def test_pull_refuses_a_job_whose_checkpoint_is_still_live(cloud, repo, state_setup):
    daemon, _provider = cloud
    job = daemon.submit(cloud_spec(repo))
    if state_setup in ("queued", "running"):
        daemon.approve(job.id)
    if state_setup == "running":
        daemon.tick()
        assert daemon.job(job.id).state == State.RUNNING
    else:
        assert daemon.job(job.id).state == State(state_setup)
    with pytest.raises(Conflict, match="live|resum"):
        daemon.pull(job.id)


def test_pull_refuses_a_local_job(daemon, executor, tmp_path):
    job = daemon.submit(JobSpec(command="true", est_runtime=60, cwd=str(tmp_path)))
    daemon.tick()
    unit = daemon.store.current_attempt(job.id).unit
    executor.exit(unit, 0)
    daemon.tick()
    assert daemon.job(job.id).state == State.COMPLETED
    with pytest.raises(Conflict, match="nothing|local"):
        daemon.pull(job.id)


def test_pull_refuses_a_cloud_target_whose_provider_is_gone(cloud, repo):
    daemon, provider = cloud
    job_id = finish(daemon, provider, repo)
    # Simulate the provider having been dropped from this pasard without the job's history
    # changing underneath it (see `_executor`/`approve`'s own "no provider" refusal).
    del daemon.executors["fake"]
    with pytest.raises(Conflict, match="provider"):
        daemon.pull(job_id)


def test_pull_refuses_when_dest_exists_and_is_not_empty(cloud, repo, tmp_path):
    daemon, provider = cloud
    job_id = finish(daemon, provider, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "already-here.txt").write_text("do not touch")

    with pytest.raises(Conflict, match="exists|empty"):
        daemon.pull(job_id, dest=dest)

    assert job_id in provider.persisted
    assert (dest / "already-here.txt").read_text() == "do not touch"
    assert not (dest / "checkpoint.pt").exists()


def test_pull_refuses_a_second_pull_while_one_is_in_flight(cloud, repo, tmp_path):
    daemon, provider = cloud
    job_id = finish(daemon, provider, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    # No real second thread needed to prove the guard: `job_id` is added to `_pulling` before
    # `download_persist` is ever called, so a second `pull()` made from *inside* the first
    # download — reentrant, same thread — is already racing the exact window the guard exists
    # for, and asserting on it is deterministic rather than timing-dependent.
    caught = []

    def on_download(jid):
        try:
            daemon.pull(job_id, dest=tmp_path / "second")
        except Conflict as e:
            caught.append(e)

    provider.on_download = on_download

    result = daemon.pull(job_id, dest=tmp_path / "first")

    assert len(caught) == 1 and "already" in str(caught[0])
    assert result["files"] == 1
    # The guard was released once the first pull finished: a pull afterwards is not refused as
    # "already being pulled" (there is simply nothing left, since the first one deleted it).
    assert daemon.pull(job_id, dest=tmp_path / "third") == {
        "job_id": job_id, "files": 0, "bytes": 0, "dest": None, "deleted": False}


def _staging_dir(parent: Path, job_id: int) -> Path:
    """The private staging directory `_pull` downloaded into, found by its prefix. Only ever
    one, since each pull's `mkdtemp` call names it uniquely."""
    found = [p for p in parent.iterdir() if p.name.startswith(f".pasar-pull-{job_id}-")]
    assert len(found) == 1, f"expected exactly one staging dir for job {job_id}, found {found}"
    return found[0]


def test_pull_mismatch_leaves_both_copies_alone_and_reports_it(cloud, repo, tmp_path):
    daemon, provider = cloud
    job_id = finish(daemon, provider, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    provider.persist(job_id, "metrics.json", b"{}")
    provider.short_download.add(job_id)
    dest = tmp_path / "out"

    with pytest.raises(Conflict) as exc:
        daemon.pull(job_id, dest=dest)
    message = str(exc.value)
    assert "2" in message and "1" in message  # expected vs found file counts

    assert provider.deleted_persist == []
    assert job_id in provider.persisted
    # `dest` was never claimed: the mismatch was caught before the rename that would have
    # claimed it, so it does not even exist.
    assert not dest.exists()
    # The partial download is left exactly as it landed, in its own staging directory, not
    # cleaned up and not merged into `dest`.
    staging = _staging_dir(tmp_path, job_id)
    assert len(list(staging.rglob("*"))) == 1
    assert str(staging) in message


def test_pull_leaves_everything_alone_when_the_download_itself_raises(cloud, repo, tmp_path):
    daemon, provider = cloud
    job_id = finish(daemon, provider, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    provider.fail_download.add(job_id)
    dest = tmp_path / "out"

    with pytest.raises(Conflict, match="pretend network failure") as exc:
        daemon.pull(job_id, dest=dest)

    assert provider.deleted_persist == []
    assert job_id in provider.persisted
    assert not dest.exists()
    # The (empty) staging directory is left in place and named in the error, exactly as a
    # mismatch's would be — there is simply nothing under it, since the fake raises before
    # writing anything.
    staging = _staging_dir(tmp_path, job_id)
    assert str(staging) in str(exc.value)


def test_pull_refuses_when_dest_exists_as_a_plain_file(cloud, repo, tmp_path):
    daemon, provider = cloud
    job_id = finish(daemon, provider, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    dest = tmp_path / "out"
    dest.write_text("not a directory")

    with pytest.raises(Conflict, match="not a directory"):
        daemon.pull(job_id, dest=dest)

    assert job_id in provider.persisted
    assert dest.read_text() == "not a directory"


def test_pull_of_two_different_jobs_racing_the_same_dest_deletes_at_most_one_remote_copy(
        cloud, repo, tmp_path):
    """Reproduces the reviewer's scenario: two pulls of *different* jobs into the same `--to`
    both pass the up-front "dest is empty" check before either has written anything (real
    concurrency isn't needed to prove this — a pull made from inside job A's own download is
    already past that check the same way a truly concurrent one would be, and deterministically
    so). If their files happened to share a name and size, downloading straight into `dest`
    would let both verifications pass and delete both remote copies, even though only one job's
    data can actually end up at `dest`. Downloading into a private staging directory and only
    ever claiming `dest` through an atomic `os.rename` is what keeps that from happening: exactly
    one of the two racing pulls wins the rename (and only it deletes its remote copy), and the
    loser's own verified download — never lost — is left sitting in its own staging directory."""
    daemon, provider = cloud
    job_a = finish(daemon, provider, repo)
    job_b = finish(daemon, provider, repo)
    # Same name, same size: exactly the case where the old direct-into-`dest` download would
    # have had both verifications pass against a directory holding a mix of both jobs' files.
    provider.persist(job_a, "checkpoint.pt", b"AAAAAAA")
    provider.persist(job_b, "checkpoint.pt", b"BBBBBBB")
    dest = tmp_path / "ckpts"

    def on_download(jid):
        if jid == job_a:
            # From inside job A's download, `dest` does not exist yet (A hasn't renamed its
            # staging directory into place) -- so job B's own up-front check also passes here,
            # the same as it would racing on two real threads.
            daemon.pull(job_b, dest=dest)

    provider.on_download = on_download

    with pytest.raises(Conflict, match="claimed") as exc:
        daemon.pull(job_a, dest=dest)

    # Exactly one remote copy was deleted -- job B's, which won the rename -- and its bytes are
    # the ones that actually made it to `dest`, not a mix of both jobs' files.
    assert provider.deleted_persist == [job_b]
    assert job_a in provider.persisted
    assert (dest / "checkpoint.pt").read_bytes() == b"BBBBBBB"
    # Job A's own verified download was not lost: it is exactly where the error says it is.
    staging_a = _staging_dir(tmp_path, job_a)
    assert (staging_a / "checkpoint.pt").read_bytes() == b"AAAAAAA"
    assert str(staging_a) in str(exc.value)


# ---- automatic pull

ROOMY = 1 << 50  # free bytes a test's fake filesystem reports when room is not the point


def roomy(daemon, free=ROOMY):
    """Pin what the destination filesystem reports as free, and record which path was asked:
    the guard must look at the nearest directory that exists, since `pulls/<id>` does not yet."""
    asked: list[Path] = []

    def disk_free(path):
        asked.append(Path(path))
        return free

    daemon.disk_free = disk_free
    return asked


def pulls_of(daemon, job_id):
    return daemon.store.pulls(job_id)


def events_of(daemon, kind):
    return [e["text"] for e in daemon.store.machine_events(200) if e["kind"] == kind]


def end_as(daemon, provider, repo, outcome, **kw):
    """Launch one cloud job and end it `outcome`-wise; returns its id once the tick settled it."""
    job_id = start(daemon, repo, **kw)
    handle = handle_of(daemon, job_id)
    if outcome == "cancelled":
        daemon.cancel(job_id)
    provider.finish(handle, 0 if outcome == "completed" else 1)
    daemon.tick()
    assert daemon.job(job_id).state == State(outcome)
    return job_id


@pytest.mark.parametrize("how", ["time_limit", "reclaimed"])
def test_a_paused_job_is_never_pulled_and_its_checkpoint_never_deleted(cloud, repo, clock, how):
    # The next attempt resumes from exactly this directory: pulling it away (and deleting the
    # remote copy) would make that attempt silently redo everything the first one was paid for.
    daemon, provider = cloud
    roomy(daemon)
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    if how == "time_limit":
        out_of_time(daemon, provider, job_id, 1)
    else:
        provider.reclaim(handle_of(daemon, job_id))
    daemon.tick()
    assert daemon.job(job_id).state == State.AWAITING

    daemon.housekeep()  # the retry sweep must not pick it up either
    clock.advance(3600)
    daemon.housekeep()
    daemon.auto_pull(job_id)  # nor may the pull itself, even if something did schedule it

    assert provider.persisted[job_id] == {"checkpoint.pt": b"weights"}
    assert provider.deleted_persist == []
    assert not (daemon.data_dir / "pulls" / str(job_id)).exists()
    assert daemon.job(job_id).state == State.AWAITING
    assert all(p["files"] is None for p in pulls_of(daemon, job_id))


def test_housekeep_neither_deletes_nor_counts_what_was_pulled(make_cloud, repo, clock):
    daemon, provider = make_cloud()
    roomy(daemon)
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", b"w" * 4096)
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()
    pulled = daemon.data_dir / "pulls" / str(job_id)
    assert (pulled / "checkpoint.pt").stat().st_size == 4096

    jobs = daemon.data_dir / "jobs"
    tree = sum(f.stat().st_size for f in jobs.rglob("*") if f.is_file())
    # Retention sized to fit pasar's own job files exactly, and a pulled file alone bigger than
    # that: were the pull dir counted, housekeep would delete job files to get back under it.
    daemon.cfg.log_retention_size = tree
    (pulled / "extra.bin").write_bytes(b"x" * (tree + 1))
    before = sorted(p for p in jobs.rglob("*"))

    daemon.housekeep()

    assert sorted(p for p in jobs.rglob("*")) == before
    assert (pulled / "extra.bin").stat().st_size == tree + 1

    clock.advance((daemon.cfg.log_retention_days + 1) * 86400)
    daemon.housekeep()  # old enough that its job files go: the pulled copy still stays
    assert not daemon.job_dir(job_id).exists()
    assert (pulled / "checkpoint.pt").stat().st_size == 4096
    assert (pulled / "extra.bin").exists()


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_a_finished_job_is_pulled_whole_and_its_remote_copy_removed(cloud, repo, outcome):
    daemon, provider = cloud
    asked = roomy(daemon)
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    provider.persist(job_id, "nested/metrics.json", b"{}")
    handle = handle_of(daemon, job_id)
    if outcome == "cancelled":
        daemon.cancel(job_id)
    provider.finish(handle, 0 if outcome == "completed" else 1)
    daemon.tick()
    assert daemon.job(job_id).state == State(outcome)

    dest = daemon.data_dir / "pulls" / str(job_id)
    assert (dest / "checkpoint.pt").read_bytes() == b"weights"
    assert (dest / "nested" / "metrics.json").read_bytes() == b"{}"
    assert provider.deleted_persist == [job_id]
    [row] = pulls_of(daemon, job_id)
    assert row["files"] == 2 and row["bytes"] == len(b"weights{}")
    assert row["dest"] == str(dest) and row["remote_deleted"] and row["error"] is None
    assert daemon.store.last_pull(job_id) == row
    assert asked and all(p.exists() for p in asked)
    assert events_of(daemon, "pulled")


def test_the_pull_runs_off_the_tick_not_inside_it(cloud, repo):
    daemon, provider = cloud
    roomy(daemon)
    pending = []
    daemon.background = pending.append
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()

    assert daemon.job(job_id).state == State.COMPLETED
    assert len(pending) == 1  # handed off...
    assert job_id in provider.persisted and pulls_of(daemon, job_id) == []  # ...not yet run
    daemon.housekeep()
    assert len(pending) == 1  # already scheduled: the retry sweep does not queue it twice

    pending.pop()()
    assert provider.deleted_persist == [job_id]
    assert pulls_of(daemon, job_id)[0]["files"] == 1


def test_a_pull_that_would_leave_too_little_free_pulls_nothing(cloud, repo, tmp_path):
    daemon, provider = cloud
    data = b"w" * 1000
    # 1000 bytes to pull, and 999 bytes short of leaving `pull_min_free` behind afterwards.
    roomy(daemon, free=daemon.cfg.pull_min_free + 1)
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", data)
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()

    job = daemon.job(job_id)
    assert job.state == State.COMPLETED and job.reason is None and job.summary == ""
    assert provider.persisted[job_id] == {"checkpoint.pt": data}
    assert provider.deleted_persist == []
    assert not (daemon.data_dir / "pulls" / str(job_id)).exists()
    assert not list((daemon.data_dir / "pulls").glob(".pasar-pull-*"))  # not even a staging dir
    [row] = pulls_of(daemon, job_id)
    assert row["files"] is None and not row["remote_deleted"]
    assert "pull_min_free" in row["error"] and "--to" in row["error"]
    [event] = events_of(daemon, "pull_skipped")
    assert str(job_id) in event and "pull_min_free" in event

    # A person can still pull it somewhere they chose: the margin guards the automatic pull.
    roomy(daemon, free=len(data))
    result = daemon.pull(job_id, dest=tmp_path / "bigger")
    assert result["files"] == 1 and provider.deleted_persist == [job_id]


def test_a_manual_pull_that_cannot_fit_at_all_is_refused(cloud, repo, tmp_path):
    daemon, provider = cloud
    job_id = finish(daemon, provider, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    roomy(daemon, free=len(b"weights") - 1)

    with pytest.raises(Conflict, match="free"):
        daemon.pull(job_id, dest=tmp_path / "out")

    assert provider.persisted[job_id] == {"checkpoint.pt": b"weights"}
    assert not (tmp_path / "out").exists()
    assert "free" in daemon.store.last_pull(job_id)["error"]


def test_a_provider_error_mid_pull_leaves_the_remote_copy_and_the_jobs_result(cloud, repo):
    daemon, provider = cloud
    roomy(daemon)
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    provider.fail_download.add(job_id)
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()

    job = daemon.job(job_id)
    assert job.state == State.COMPLETED and job.reason is None and job.summary == ""
    assert provider.persisted[job_id] == {"checkpoint.pt": b"weights"}
    assert provider.deleted_persist == []
    [row] = pulls_of(daemon, job_id)
    assert row["files"] is None and "pretend network failure" in row["error"]
    assert any(str(job_id) in e for e in events_of(daemon, "pull_failed"))


def test_an_unexpected_provider_error_does_not_break_the_tick(cloud, repo, monkeypatch):
    daemon, provider = cloud
    roomy(daemon)

    def broken(job_id):
        raise RuntimeError("provider fell over")

    monkeypatch.setattr(provider, "persist_manifest", broken)
    job_id = start(daemon, repo)
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()

    assert daemon.job(job_id).state == State.COMPLETED
    assert "provider fell over" in daemon.store.last_pull(job_id)["error"]
    assert any("provider fell over" in e for e in events_of(daemon, "pull_failed"))


@pytest.mark.parametrize("pull_max, pulled", [(0, True), (7, True), (6, False)])
def test_pull_max_zero_is_no_limit_and_a_nonzero_one_still_caps(cloud, repo, pull_max, pulled):
    daemon, provider = cloud
    roomy(daemon)
    daemon.cfg.pull_max = pull_max
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")  # 7 bytes
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()

    row = daemon.store.last_pull(job_id)
    if pulled:
        assert row["files"] == 1 and provider.deleted_persist == [job_id]
    else:
        assert row["files"] is None and "pull_max" in row["error"]
        assert provider.persisted[job_id] == {"checkpoint.pt": b"weights"}
        assert provider.deleted_persist == []
        assert any("pull_max" in e for e in events_of(daemon, "pull_skipped"))


def test_housekeep_retries_a_pull_that_did_not_finish(cloud, repo, clock):
    daemon, provider = cloud
    roomy(daemon)
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    provider.fail_download.add(job_id)
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()
    assert provider.deleted_persist == []

    provider.fail_download.discard(job_id)
    clock.advance(3600)
    daemon.housekeep()

    assert provider.deleted_persist == [job_id]
    assert (daemon.data_dir / "pulls" / str(job_id) / "checkpoint.pt").read_bytes() == b"weights"
    assert daemon.store.last_pull(job_id)["files"] == 1


def test_housekeep_picks_up_a_pull_a_restart_cut_short(make_cloud, repo):
    # pasard went down before the scheduled pull ever ran (or while it ran): nothing was
    # recorded, and the next housekeep, which runs right after startup, has to pick it up.
    daemon, provider = make_cloud()
    roomy(daemon)
    daemon.background = lambda work: None  # the thread died with the process
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()
    assert pulls_of(daemon, job_id) == []

    after, _ = make_cloud(provider)  # a fresh process remembers nothing it had queued
    roomy(after)
    after.housekeep()

    assert provider.deleted_persist == [job_id]


def test_housekeep_does_not_pull_again_once_a_pull_succeeded(cloud, repo, clock, monkeypatch):
    daemon, provider = cloud
    roomy(daemon)
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()

    calls = []
    real = provider.persist_manifest
    monkeypatch.setattr(provider, "persist_manifest", lambda jid: calls.append(jid) or real(jid))
    for _ in range(3):
        clock.advance(3600)
        daemon.housekeep()

    assert calls == []
    assert provider.deleted_persist == [job_id]


def test_housekeep_does_not_retry_a_tried_pull_past_the_retry_window(make_cloud, repo, clock):
    daemon, provider = make_cloud()
    roomy(daemon)
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    provider.fail_download.add(job_id)
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()
    assert daemon.store.cloud_finished_at(job_id) == clock.t
    assert len(pulls_of(daemon, job_id)) == 1
    provider.fail_download.discard(job_id)

    after, _ = make_cloud(provider)
    after.background = lambda work: None  # whatever is queued now is what housekeep decided
    roomy(after)
    clock.advance(after.cfg.cloud_retention_days * 86400 + 1)
    after.housekeep()

    # Not tried again: what happens to the remote copy past the window is the sweep's business
    # (see the tests of it below), and it never brings anything to local disk.
    assert len(pulls_of(after, job_id)) == 1
    assert after._pull_order and all(work == after.sweep for _, work in after._pull_order)


def test_a_job_never_tried_is_pulled_even_past_the_retry_window(make_cloud, repo, clock):
    # pasard went down before its pull ran and stayed down (or its provider did) past the
    # window: the first try is still owed, and the sweep waits for it.
    daemon, provider = make_cloud()
    roomy(daemon)
    daemon.background = lambda work: None
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()

    after, _ = make_cloud(provider)
    roomy(after)
    clock.advance(after.cfg.cloud_retention_days * 86400 + 1)
    after.housekeep()

    assert (after.data_dir / "pulls" / str(job_id) / "checkpoint.pt").read_bytes() == b"weights"
    assert after.store.cloud_swept(job_id)["bytes"] == 0  # the sweep found nothing left


def test_a_pull_that_landed_but_could_not_delete_the_remote_copy_is_not_retried(
        cloud, repo, clock, monkeypatch):
    daemon, provider = cloud
    roomy(daemon)

    def cannot_delete(job_id):
        raise RuntimeError("delete refused")

    monkeypatch.setattr(provider, "delete_persist_files", lambda jid, files: cannot_delete(jid))
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()

    dest = daemon.data_dir / "pulls" / str(job_id)
    assert (dest / "checkpoint.pt").read_bytes() == b"weights"
    row = daemon.store.last_pull(job_id)
    assert row["files"] == 1 and not row["remote_deleted"] and "delete refused" in row["error"]
    assert job_id in provider.persisted

    clock.advance(3600)
    daemon.housekeep()  # it landed: another try would only find `dest` taken, every hour
    assert len(pulls_of(daemon, job_id)) == 1


def test_a_manual_pull_records_its_outcome_too(cloud, repo, tmp_path):
    daemon, provider = cloud
    job_id = finish(daemon, provider, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    taken = tmp_path / "taken"
    taken.mkdir()
    (taken / "x").write_text("x")

    with pytest.raises(Conflict):
        daemon.pull(job_id, dest=taken)
    refused = daemon.store.last_pull(job_id)
    assert refused["files"] is None and "not empty" in refused["error"]

    daemon.pull(job_id, dest=tmp_path / "out", keep=True)
    kept = daemon.store.last_pull(job_id)
    assert kept["files"] == 1 and not kept["remote_deleted"] and kept["error"] is None
    assert kept["dest"] == str(tmp_path / "out")


def disk_that_fills(daemon, free):
    """A destination filesystem with `free` bytes to start with, less whatever pulls have since
    landed under the pull dir: what lets a test see one pull's room check account for another's."""
    pulls = daemon.data_dir / "pulls"

    def disk_free(path):
        used = sum(f.stat().st_size for f in pulls.rglob("*") if f.is_file()) \
            if pulls.exists() else 0
        return free - used

    daemon.disk_free = disk_free


def test_automatic_pulls_run_one_at_a_time_so_each_room_check_sees_the_last(cloud, repo):
    # Two jobs finishing together, 1000 bytes each, with room for one. Started side by side,
    # both room checks would read the same free space and both would go ahead.
    daemon, provider = cloud
    daemon.cfg.pull_min_free = 100
    disk_that_fills(daemon, 100 + 1500)
    pending = []
    daemon.background = pending.append
    first, second = start(daemon, repo), start(daemon, repo)
    for job_id in (first, second):
        provider.persist(job_id, "checkpoint.pt", b"w" * 1000)
        provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()
    assert daemon.job(first).state == daemon.job(second).state == State.COMPLETED

    assert len(pending) == 1  # one worker for both, not a thread each
    pending.pop()()

    assert (daemon.data_dir / "pulls" / str(first) / "checkpoint.pt").exists()
    assert provider.deleted_persist == [first]
    assert provider.persisted[second] == {"checkpoint.pt": b"w" * 1000}
    assert "pull_min_free" in daemon.store.last_pull(second)["error"]
    assert daemon._pull_queued == set()

    daemon.housekeep()  # the worker went idle once the queue ran dry: a new one starts
    assert len(pending) == 1


def pause_with_a_checkpoint(daemon, provider, repo):
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    out_of_time(daemon, provider, job_id, 1)
    daemon.tick()
    assert daemon.job(job_id).state == State.AWAITING
    return job_id


ENDINGS = ["cancel_awaiting", "reject", "expire", "cancel_queued"]


@pytest.mark.parametrize("ending", ENDINGS)
def test_a_paused_job_that_ends_without_running_again_is_pulled(make_cloud, repo, clock, ending):
    daemon, provider = make_cloud(approval_ttl=600)
    roomy(daemon)
    job_id = pause_with_a_checkpoint(daemon, provider, repo)
    assert daemon.store.cloud_finished_at(job_id) is None  # paused is not finished
    clock.advance(60)
    if ending == "cancel_awaiting":
        daemon.cancel(job_id)
    elif ending == "reject":
        daemon.reject(job_id)
    elif ending == "expire":
        clock.advance(600)
        daemon.tick()
    else:
        daemon.approve(job_id)
        daemon.cancel(job_id)
    assert daemon.job(job_id).state == State.CANCELLED

    assert daemon.store.cloud_finished_at(job_id) == clock.t
    assert provider.deleted_persist == [job_id]
    assert (daemon.data_dir / "pulls" / str(job_id) / "checkpoint.pt").read_bytes() == b"weights"


def test_a_job_paused_for_days_then_cancelled_gets_its_full_retry_window(cloud, repo, clock):
    # The window runs from when the job ended, not from its last attempt: measured from the
    # pause, a job cancelled five days later would be past it before its first retry.
    daemon, provider = cloud
    roomy(daemon)
    job_id = pause_with_a_checkpoint(daemon, provider, repo)
    clock.advance(5 * 86400)
    provider.fail_download.add(job_id)
    daemon.cancel(job_id)
    assert provider.deleted_persist == []

    provider.fail_download.discard(job_id)
    clock.advance(3600)
    daemon.housekeep()

    assert provider.deleted_persist == [job_id]


def test_every_way_a_cloud_job_ends_is_stamped(make_cloud, repo, clock, tmp_path, executor,
                                                probe):
    daemon, provider = make_cloud(approval_ttl=600)
    ended = {
        "completed": end_as(daemon, provider, repo, "completed"),
        "failed": end_as(daemon, provider, repo, "failed"),
        "cancelled": end_as(daemon, provider, repo, "cancelled"),
        "rejected": daemon.reject(daemon.submit(cloud_spec(repo)).id).id,
        "cancel_awaiting": daemon.cancel(daemon.submit(cloud_spec(repo)).id).id,
    }
    provider.fail_launch = "no capacity"
    ended["launch_error"] = start(daemon, repo)
    provider.fail_launch = None
    expiring = daemon.submit(cloud_spec(repo)).id
    clock.advance(601)
    daemon.tick()
    ended["expired"] = expiring
    running = start(daemon, repo)
    waiting = daemon.submit(cloud_spec(repo)).id
    for job_id in ended.values():
        assert daemon.job(job_id).state in (State.COMPLETED, State.FAILED, State.CANCELLED)
        assert daemon.store.cloud_finished_at(job_id) is not None, job_id

    data = tmp_path / "data"
    after = Daemon(Config(), Store(data / "pasar.db"), executor, probe, data, clock=clock,
                   background=run_now)
    after.tick()  # both on a target this pasard no longer has
    for job_id in (running, waiting):
        assert after.job(job_id).state in (State.FAILED, State.CANCELLED)
        assert after.store.cloud_finished_at(job_id) == clock.t


def test_housekeep_stamps_a_finished_cloud_job_that_has_no_stamp(make_cloud, repo, clock):
    # A job that finished before this was deployed, or down a path that forgot to stamp it:
    # housekeep stamps it now, which gives it a fresh window and so one pull.
    daemon, provider = make_cloud()
    daemon.background = lambda work: None
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()
    daemon.store._x("DELETE FROM cloud_finished")
    local = daemon.submit(JobSpec(command="true", est_runtime=60, cwd=str(repo)))
    daemon.cancel(local.id)

    after, _ = make_cloud(provider)
    roomy(after)
    clock.advance(30 * 86400)
    after.housekeep()

    assert after.store.cloud_finished_at(job_id) == clock.t
    assert after.store.cloud_finished_at(local.id) is None  # a local job has nothing to pull
    assert provider.deleted_persist == [job_id]


def test_automatic_pulls_give_up_after_three_tries(cloud, repo, clock, tmp_path, monkeypatch):
    daemon, provider = cloud
    roomy(daemon)
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    provider.fail_download.add(job_id)
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()  # try 1
    with pytest.raises(Conflict):
        daemon.pull(job_id, dest=tmp_path / "by-hand")  # a person's try does not count
    clock.advance(3600)
    daemon.housekeep()  # try 2
    assert events_of(daemon, "pull_gave_up") == []
    clock.advance(3600)
    daemon.housekeep()  # try 3

    [event] = events_of(daemon, "pull_gave_up")
    assert str(job_id) in event and f"pasar pull {job_id}" in event
    staged = sorted((daemon.data_dir / "pulls").glob(f".pasar-pull-{job_id}-*"))
    assert len(staged) == 3 and all(str(p) in event for p in staged)

    calls = []
    real = provider.persist_manifest
    monkeypatch.setattr(provider, "persist_manifest", lambda jid: calls.append(jid) or real(jid))
    for _ in range(3):
        clock.advance(3600)
        daemon.housekeep()
    assert calls == []  # no fourth try
    assert len(events_of(daemon, "pull_gave_up")) == 1
    assert provider.deleted_persist == []

    provider.fail_download.discard(job_id)  # a person can still pull it
    assert daemon.pull(job_id, dest=tmp_path / "later")["deleted"] is True


def test_an_automatic_pull_that_finds_nothing_is_not_taken_as_landed(cloud, repo, clock):
    # A volume listed straight after the sandbox exits may not show what it just wrote yet.
    daemon, provider = cloud
    roomy(daemon)
    job_id = end_as(daemon, provider, repo, "completed")  # try 1: nothing there yet
    assert daemon.store.last_pull(job_id)["files"] == 0
    assert daemon.store.last_pull(job_id, landed=True) is None
    clock.advance(3600)
    daemon.housekeep()  # try 2: still nothing
    provider.persist(job_id, "checkpoint.pt", b"weights")
    clock.advance(3600)
    daemon.housekeep()  # try 3: there it is

    assert provider.deleted_persist == [job_id]
    assert daemon.store.last_pull(job_id, landed=True)["files"] == 1


def test_a_job_that_really_left_nothing_costs_three_cheap_looks_and_no_alarm(cloud, repo, clock,
                                                                            monkeypatch):
    daemon, provider = cloud
    calls = []
    real = provider.persist_manifest
    monkeypatch.setattr(provider, "persist_manifest", lambda jid: calls.append(jid) or real(jid))
    job_id = end_as(daemon, provider, repo, "completed")
    for _ in range(5):
        clock.advance(3600)
        daemon.housekeep()

    assert calls == [job_id] * 3
    assert events_of(daemon, "pull_gave_up") == []


# ---- a pull deletes only what it verified

def test_a_file_committed_during_the_download_survives_the_pull(cloud, repo):
    # A mount commits in the background, so a final checkpoint can reach the volume after the
    # pull listed the dir, while it is still downloading. Deleting "the job's dir" once the
    # download verified would take that file with it, never having fetched it.
    daemon, provider = cloud
    roomy(daemon)
    job_id = start(daemon, repo)
    provider.persist(job_id, "ckpt-100.pt", b"early")
    provider.after_download = lambda jid: provider.persist(jid, "final.pt", b"the last one")
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()

    dest = daemon.data_dir / "pulls" / str(job_id)
    assert sorted(p.name for p in dest.iterdir()) == ["ckpt-100.pt"]
    assert provider.persisted[job_id]["final.pt"] == b"the last one"
    assert provider.recursive_deletes == []


def test_a_listing_that_grew_while_pulling_leaves_the_remote_copy_whole(cloud, repo, tmp_path):
    # What is on the volume now is not what was verified: the user has to be able to pull the
    # whole of it again elsewhere, so nothing at all is deleted, and they are told why.
    daemon, provider = cloud
    roomy(daemon)
    job_id = finish(daemon, provider, repo)
    provider.persist(job_id, "ckpt-100.pt", b"early")
    provider.after_download = lambda jid: provider.persist(jid, "final.pt", b"the last one")

    result = daemon.pull(job_id, dest=tmp_path / "out")

    assert result["deleted"] is False and "changed while it was being pulled" in result["note"]
    assert provider.persisted[job_id] == {"ckpt-100.pt": b"early", "final.pt": b"the last one"}
    assert provider.deleted_files == [] and provider.deleted_persist == []
    row = daemon.store.last_pull(job_id)
    assert row["files"] == 1 and not row["remote_deleted"] and row["error"] is None
    assert daemon.store.last_pull(job_id, landed=True) == row
    [event] = events_of(daemon, "pull_changed")
    assert f"job {job_id}" in event and "left in place" in event and "--to" in event


def test_a_file_rewritten_at_the_same_size_while_pulling_blocks_the_delete(cloud, repo, tmp_path):
    # Same name, same size, different bytes: only the listing's mtime can tell, and the copy
    # that was verified is not the one now on the volume.
    daemon, provider = cloud
    roomy(daemon)
    job_id = finish(daemon, provider, repo)
    provider.persist(job_id, "ckpt.pt", b"weights")
    provider.after_download = lambda jid: provider.persist(jid, "ckpt.pt", b"WEIGHTS")

    result = daemon.pull(job_id, dest=tmp_path / "out")

    assert result["deleted"] is False
    assert (tmp_path / "out" / "ckpt.pt").read_bytes() == b"weights"
    assert provider.persisted[job_id] == {"ckpt.pt": b"WEIGHTS"}
    assert provider.deleted_files == []


def test_a_file_that_lands_after_the_last_listing_is_still_not_deleted(cloud, repo, tmp_path,
                                                                        monkeypatch):
    # The re-listing narrows the race; it cannot close it. What closes it is deleting by
    # manifest: one named file at a time, never the directory recursively.
    daemon, provider = cloud
    roomy(daemon)
    job_id = finish(daemon, provider, repo)
    provider.persist(job_id, "ckpt-100.pt", b"early")
    real = provider.persist_manifest
    listings = []

    def listing(jid):
        seen = real(jid)
        listings.append(seen)
        if len(listings) == 2:  # the re-listing just before the delete
            provider.persist(jid, "final.pt", b"the last one")
        return seen

    monkeypatch.setattr(provider, "persist_manifest", listing)
    result = daemon.pull(job_id, dest=tmp_path / "out")

    assert result["deleted"] is True
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["ckpt-100.pt"]
    assert provider.persisted[job_id] == {"final.pt": b"the last one"}
    assert provider.deleted_files == [(job_id, "ckpt-100.pt")]
    assert provider.recursive_deletes == [] and provider.deleted_persist == []


def test_a_pull_deletes_each_verified_file_by_name_not_the_whole_dir(cloud, repo):
    daemon, provider = cloud
    roomy(daemon)
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    provider.persist(job_id, "nested/metrics.json", b"{}")
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()

    assert sorted(provider.deleted_files) == [(job_id, "checkpoint.pt"),
                                              (job_id, "nested/metrics.json")]
    assert provider.recursive_deletes == [] and job_id not in provider.persisted


def test_an_automatic_pull_waits_for_the_volume_to_settle(cloud, repo, clock):
    # A sandbox's last background commit can still be on its way when it exits: pulling at once
    # would list the volume before its final checkpoint is there.
    daemon, provider = cloud
    daemon.pull_settle = PULL_SETTLE
    roomy(daemon)
    job_id = start(daemon, repo)
    provider.persist(job_id, "ckpt.pt", b"weights")
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()
    assert daemon.job(job_id).state == State.COMPLETED
    daemon.housekeep()
    daemon.auto_pull(job_id)  # nor may the pull itself, if something did queue it early
    clock.advance(PULL_SETTLE - 1)
    daemon.tick()
    assert pulls_of(daemon, job_id) == [] and provider.deleted_files == []

    clock.advance(1)
    daemon.tick()
    assert pulls_of(daemon, job_id)[0]["files"] == 1 and job_id not in provider.persisted


def test_the_settle_delay_holds_nothing_up_on_the_tick(cloud, repo, clock):
    # Deferred, not waited out: no tick sleeps, and nothing goes to the worker until it is due.
    daemon, provider = cloud
    daemon.pull_settle = PULL_SETTLE
    roomy(daemon)
    pending = []
    daemon.background = pending.append
    job_id = start(daemon, repo)
    provider.persist(job_id, "ckpt.pt", b"weights")
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()
    daemon.tick()
    assert pending == [] and clock.t < daemon.store.cloud_finished_at(job_id) + PULL_SETTLE

    clock.advance(PULL_SETTLE)
    daemon.tick()
    assert len(pending) == 1
    pending.pop()()
    assert pulls_of(daemon, job_id)[0]["files"] == 1


def test_a_restart_inside_the_settle_delay_still_pulls_once_it_is_over(make_cloud, repo, clock):
    daemon, provider = make_cloud()
    daemon.pull_settle = PULL_SETTLE
    roomy(daemon)
    job_id = start(daemon, repo)
    provider.persist(job_id, "ckpt.pt", b"weights")
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()

    after, _ = make_cloud(provider)  # a fresh process remembers nothing it had deferred
    after.pull_settle = PULL_SETTLE
    roomy(after)
    after.housekeep()  # run at startup: too early yet
    assert pulls_of(after, job_id) == []
    clock.advance(PULL_SETTLE)
    after.tick()
    assert pulls_of(after, job_id)[0]["files"] == 1


# ---- sweeping what nobody pulled

def retention(daemon):
    return daemon.cfg.cloud_retention_days * 86400


def left_unpulled(daemon, provider, repo, data=b"weights"):
    """One finished cloud job whose results nobody pulled: its automatic pull found no room, so
    the copy on the provider's volume is the only one. Returns its id."""
    roomy(daemon, free=0)
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", data)
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()
    assert daemon.job(job_id).state == State.COMPLETED
    assert provider.persisted[job_id] and pulls_of(daemon, job_id)[-1]["files"] is None
    return job_id


def test_cloud_retention_defaults_to_three_days():
    assert Config().cloud_retention_days == 3


def test_a_finished_job_nobody_pulled_is_swept_once_past_retention(cloud, repo, clock):
    daemon, provider = cloud
    job_id = left_unpulled(daemon, provider, repo, b"w" * 4096)
    # Whatever is on local disk under the pull dir is the user's, and never the sweep's to touch.
    local = daemon.data_dir / "pulls" / str(job_id) / "notes.txt"
    local.parent.mkdir(parents=True)
    local.write_text("mine")
    clock.advance(retention(daemon))
    daemon.housekeep()

    assert provider.deleted_persist == [job_id] and job_id not in provider.persisted
    # Past the window, everything goes: the sweep alone deletes the dir recursively.
    assert provider.recursive_deletes == [job_id]
    assert daemon.store.cloud_swept(job_id) == {"job_id": job_id, "ts": clock.t, "files": 1,
                                                "bytes": 4096}
    [event] = events_of(daemon, "swept")
    assert f"job {job_id}" in event and "4096 bytes" in event
    assert local.read_text() == "mine"
    assert daemon.job(job_id).state == State.COMPLETED


def test_a_job_inside_the_retention_window_is_left_alone(cloud, repo, clock):
    daemon, provider = cloud
    job_id = left_unpulled(daemon, provider, repo)
    clock.advance(retention(daemon) - 1)
    daemon.housekeep()
    daemon.sweep(job_id)  # the sweep checks the age again itself: time passes in the queue

    assert provider.persisted[job_id] == {"checkpoint.pt": b"weights"}
    assert provider.deleted_persist == []
    assert daemon.store.cloud_swept(job_id) is None and events_of(daemon, "swept") == []


def test_retention_runs_from_when_the_job_finished_not_from_its_last_attempt(cloud, repo, clock):
    # Paused for days and then cancelled: its results are as old as the cancel, not the pause.
    daemon, provider = cloud
    job_id = pause_with_a_checkpoint(daemon, provider, repo)
    clock.advance(retention(daemon) + 86400)
    roomy(daemon, free=0)
    daemon.cancel(job_id)
    clock.advance(3600)
    daemon.housekeep()

    assert provider.deleted_persist == [] and daemon.store.cloud_swept(job_id) is None
    clock.advance(retention(daemon))
    daemon.housekeep()
    assert provider.deleted_persist == [job_id]


@pytest.mark.parametrize("how", ["paused", "running"])
def test_a_live_checkpoint_is_never_swept_however_old(cloud, repo, clock, how):
    # A paused job is waiting for a person to approve its next attempt, which resumes from
    # exactly this directory: sweeping it would make that attempt silently redo everything the
    # first one was paid for. Not even a stray "finished" stamp may change that.
    daemon, provider = cloud
    roomy(daemon)
    if how == "paused":
        job_id = pause_with_a_checkpoint(daemon, provider, repo)
        state = State.AWAITING
    else:
        job_id = start(daemon, repo)
        provider.persist(job_id, "checkpoint.pt", b"weights")
        state = State.RUNNING
    daemon.store.mark_cloud_finished(job_id, 0.0)
    for _ in range(3):
        clock.advance(365 * 86400)
        daemon.housekeep()
    daemon.sweep(job_id)  # nor may the sweep itself, even if something did queue it

    assert daemon.job(job_id).state == state
    assert provider.persisted[job_id] == {"checkpoint.pt": b"weights"}
    assert provider.deleted_persist == []
    assert daemon.store.cloud_swept(job_id) is None and events_of(daemon, "swept") == []


def test_a_job_whose_provider_is_gone_is_skipped_with_a_log_line(cloud, repo, clock, caplog):
    daemon, provider = cloud
    job_id = left_unpulled(daemon, provider, repo)
    executor = daemon.executors.pop("fake")
    clock.advance(retention(daemon))
    with caplog.at_level(logging.WARNING, logger="pasar.daemon"):
        daemon.housekeep()
        daemon.sweep(job_id)

    assert f"job {job_id}" in caplog.text and "fake" in caplog.text
    assert provider.deleted_persist == [] and daemon.store.cloud_swept(job_id) is None

    daemon.executors["fake"] = executor  # the provider comes back: the next housekeep sweeps it
    clock.advance(3600)
    daemon.housekeep()
    assert provider.deleted_persist == [job_id]


def test_a_job_nobody_ever_tried_to_pull_is_never_swept(cloud, repo, clock):
    # No automatic pull ever ran — its provider was down all through the window, say — so the
    # volume holds the only copy and nobody has had a chance to fetch it: past the window or
    # not, it is not the sweep's to delete.
    daemon, provider = cloud
    daemon.background = lambda work: None  # nothing queued ever runs
    job_id = finish(daemon, provider, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    clock.advance(retention(daemon) + 86400)
    daemon.housekeep()
    daemon.sweep(job_id)

    assert pulls_of(daemon, job_id) == []
    assert provider.persisted[job_id] == {"checkpoint.pt": b"weights"}
    assert provider.deleted_persist == [] and daemon.store.cloud_swept(job_id) is None


def test_a_job_whose_provider_was_down_past_the_window_is_pulled_before_any_sweep(
        make_cloud, repo, tmp_path, clock, executor, probe):
    # The reproduced loss: a paused job's provider failed to start, the job ended while it was
    # down, and by the time it came back the retention window had passed. It must be pulled
    # first — a first try is owed whatever the window says — and never swept unpulled.
    daemon, provider = make_cloud(approval_ttl=600)
    job_id = pause_with_a_checkpoint(daemon, provider, repo)
    down = without_its_provider(tmp_path, clock, executor, probe, approval_ttl=600)
    clock.advance(601)
    down.tick()  # nobody approved it in time
    assert down.job(job_id).state == State.CANCELLED
    clock.advance(retention(down) + 86400)
    down.housekeep()
    assert provider.persisted[job_id] == {"checkpoint.pt": b"weights"}

    back, _ = make_cloud(provider, approval_ttl=600)
    roomy(back)
    back.housekeep()

    pulled = back.data_dir / "pulls" / str(job_id) / "checkpoint.pt"
    assert pulled.read_bytes() == b"weights"
    assert back.store.last_pull(job_id, landed=True)["auto"]


def test_once_tried_a_pull_past_the_window_is_not_tried_again_and_the_sweep_goes_ahead(
        cloud, repo, clock):
    daemon, provider = cloud
    job_id = left_unpulled(daemon, provider, repo)  # one automatic try, skipped for room
    clock.advance(retention(daemon) + 1)
    daemon.housekeep()
    assert daemon.store.count_pulls(job_id, auto=True) == 1
    assert provider.deleted_persist == [job_id]


def test_a_job_already_pulled_is_marked_swept_without_deleting_again(cloud, repo, clock):
    daemon, provider = cloud
    roomy(daemon)
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()
    assert provider.deleted_persist == [job_id]  # the pull's own delete, after verifying

    clock.advance(retention(daemon))
    daemon.housekeep()

    assert provider.deleted_persist == [job_id]
    assert daemon.store.cloud_swept(job_id)["bytes"] == 0
    assert events_of(daemon, "swept") == []
    assert (daemon.data_dir / "pulls" / str(job_id) / "checkpoint.pt").read_bytes() == b"weights"


def test_a_swept_job_is_never_looked_at_again(cloud, repo, clock, monkeypatch):
    daemon, provider = cloud
    job_id = left_unpulled(daemon, provider, repo)
    clock.advance(retention(daemon))
    daemon.housekeep()
    assert provider.deleted_persist == [job_id]

    calls = []
    real = provider.persist_usage
    monkeypatch.setattr(provider, "persist_usage", lambda jid: calls.append(jid) or real(jid))
    for _ in range(3):
        clock.advance(86400)
        daemon.housekeep()

    assert calls == [] and provider.deleted_persist == [job_id]


def test_a_provider_error_on_one_job_does_not_stop_the_others(cloud, repo, clock, monkeypatch,
                                                              caplog):
    daemon, provider = cloud
    first = left_unpulled(daemon, provider, repo)
    second = left_unpulled(daemon, provider, repo, b"seven!!")
    real = provider.delete_persist

    def flaky(job_id):
        if job_id == first:
            raise RuntimeError("pretend the volume API is down")
        real(job_id)

    monkeypatch.setattr(provider, "delete_persist", flaky)
    clock.advance(retention(daemon))
    with caplog.at_level(logging.WARNING, logger="pasar.daemon"):
        daemon.housekeep()

    assert provider.deleted_persist == [second]
    assert daemon.store.cloud_swept(second)["bytes"] == 7
    assert daemon.store.cloud_swept(first) is None and first in provider.persisted
    assert "pretend the volume API is down" in caplog.text

    monkeypatch.setattr(provider, "delete_persist", real)
    clock.advance(3600)
    daemon.housekeep()
    assert provider.deleted_persist == [second, first]


def test_the_sweep_runs_off_the_tick_through_the_one_worker(cloud, repo, clock):
    daemon, provider = cloud
    job_id = left_unpulled(daemon, provider, repo)
    pending = []
    daemon.background = pending.append
    clock.advance(retention(daemon) + 1)
    daemon.housekeep()

    assert len(pending) == 1 and provider.deleted_persist == []  # decided, not yet done
    daemon.housekeep()
    assert len(pending) == 1  # and not queued twice

    pending.pop()()
    assert provider.deleted_persist == [job_id]
    assert daemon._pull_queued == set()


def test_a_job_whose_pull_is_still_queued_is_not_swept_under_it(cloud, repo, clock):
    # On the last second of the window the retry queues one more pull; the sweep waits for it.
    daemon, provider = cloud
    job_id = left_unpulled(daemon, provider, repo)
    pending = []
    daemon.background = pending.append
    roomy(daemon)
    clock.advance(retention(daemon))
    daemon.housekeep()

    pending.pop()()
    assert pending == []
    assert daemon.store.last_pull(job_id, landed=True)["files"] == 1
    assert daemon.store.cloud_swept(job_id) is None

    clock.advance(1)
    daemon.housekeep()
    pending.pop()()
    assert provider.deleted_persist == [job_id]  # the pull's, and nothing more
    assert daemon.store.cloud_swept(job_id)["bytes"] == 0


def test_a_sweep_waits_out_a_pull_already_in_flight(cloud, repo, clock, tmp_path):
    daemon, provider = cloud
    job_id = left_unpulled(daemon, provider, repo)
    roomy(daemon)
    clock.advance(retention(daemon) + 1)
    # A person pulls it by hand at the very moment the sweep comes round to it.
    provider.on_download = daemon.sweep
    daemon.pull(job_id, tmp_path / "out")

    assert (tmp_path / "out" / "checkpoint.pt").read_bytes() == b"weights"
    assert provider.deleted_persist == [job_id]  # once, by the pull that verified it
    assert daemon.store.cloud_swept(job_id) is None


def test_a_manual_pull_is_refused_while_the_sweep_has_the_job(cloud, repo, clock, tmp_path,
                                                             monkeypatch):
    daemon, provider = cloud
    job_id = left_unpulled(daemon, provider, repo)
    clock.advance(retention(daemon) + 1)
    refused = []
    real = provider.persist_usage

    def usage(jid):
        with pytest.raises(Conflict) as e:
            daemon.pull(jid, tmp_path / "out")
        refused.append(str(e.value))
        return real(jid)

    monkeypatch.setattr(provider, "persist_usage", usage)
    daemon.housekeep()

    assert len(refused) == 1 and "swe" in refused[0]
    assert provider.deleted_persist == [job_id]
    assert not (tmp_path / "out").exists()


def test_the_pull_retry_window_is_cloud_retention_days(make_cloud, repo, clock):
    daemon, provider = make_cloud()
    daemon.cfg.cloud_retention_days = 10
    job_id = left_unpulled(daemon, provider, repo)
    roomy(daemon)  # room again
    clock.advance(5 * 86400)
    daemon.housekeep()

    assert provider.deleted_persist == [job_id]
    assert daemon.store.last_pull(job_id, landed=True)["files"] == 1


# ---- what a job left behind, and when it goes

def deadline(daemon, job_id):
    """The sweep deadline as a message says it: local time, to the minute."""
    at = daemon.store.cloud_finished_at(job_id) + retention(daemon)
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(at))


def persist_of(daemon, job_id):
    return cloud_view(daemon, daemon.job(job_id))["persist"]


def test_a_skipped_pull_says_when_the_results_are_deleted_at_the_provider(cloud, repo):
    # "left at the provider; pull it by hand" with no date reads as if it stays there for good.
    daemon, provider = cloud
    job_id = left_unpulled(daemon, provider, repo)
    [event] = events_of(daemon, "pull_skipped")
    assert deadline(daemon, job_id) in event and "cloud_retention_days" in event
    assert deadline(daemon, job_id) in daemon.store.last_pull(job_id)["error"]


def test_giving_up_says_when_the_results_are_deleted_at_the_provider(cloud, repo, clock):
    daemon, provider = cloud
    roomy(daemon)
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    provider.fail_download.add(job_id)
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()
    for _ in range(2):
        clock.advance(3600)
        daemon.housekeep()
    [event] = events_of(daemon, "pull_gave_up")
    assert deadline(daemon, job_id) in event and "cloud_retention_days" in event


def test_a_pull_records_what_the_provider_held_even_when_it_pulls_nothing(cloud, repo):
    daemon, provider = cloud
    job_id = left_unpulled(daemon, provider, repo, b"w" * 4096)
    row = daemon.store.last_pull(job_id)
    assert row["files"] is None  # nothing landed...
    assert row["remote_files"] == 1 and row["remote_bytes"] == 4096  # ...but it was measured


def test_persist_is_only_on_a_finished_cloud_job(cloud, repo):
    daemon, _ = cloud
    job = daemon.submit(cloud_spec(repo))
    assert cloud_view(daemon, job)["persist"] is None  # awaiting: its checkpoint is live
    daemon.approve(job.id)
    daemon.tick()
    assert persist_of(daemon, job.id) is None  # running


def test_persist_says_nothing_is_at_the_provider_for_a_job_that_never_launched(cloud, repo):
    # Rejected straight from awaiting: no attempt, so no persist dir to pull or sweep.
    daemon, _ = cloud
    job = daemon.submit(cloud_spec(repo))
    daemon.reject(job.id)
    view = persist_of(daemon, job.id)
    assert view["remote_bytes"] == 0 and view["sweeps_at"] is None and view["files"] is None


def test_persist_still_waits_on_a_paused_job_rejected_at_its_reapproval(cloud, repo):
    # A paused job's checkpoint is still at the provider: rejected, it is results to pull.
    daemon, provider = cloud
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    provider.reclaim(handle_of(daemon, job_id))
    daemon.tick()
    assert daemon.job(job_id).state == State.AWAITING
    daemon.pull_settle = 3600  # its pull not due yet: the checkpoint is still at the provider
    daemon.reject(job_id)
    assert persist_of(daemon, job_id)["sweeps_at"] is not None


def test_persist_says_where_a_pulled_job_landed_and_that_nothing_is_left(cloud, repo, clock):
    daemon, provider = cloud
    roomy(daemon)
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    provider.persist(job_id, "metrics.json", b"{}")
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()

    assert persist_of(daemon, job_id) == {
        "files": 2, "bytes": len(b"weights{}"), "pulled_at": clock.t,
        "pulled_to": str(daemon.data_dir / "pulls" / str(job_id)), "remote_deleted": True,
        "remote_bytes": 0, "swept_at": None, "swept_bytes": None, "sweeps_at": None,
        "last_error": None,
    }


def test_persist_says_what_is_still_at_the_provider_and_when_it_goes(cloud, repo, clock):
    daemon, provider = cloud
    job_id = left_unpulled(daemon, provider, repo, b"w" * 4096)
    finished = daemon.store.cloud_finished_at(job_id)

    view = persist_of(daemon, job_id)
    assert view["files"] is None and view["pulled_to"] is None and not view["remote_deleted"]
    assert view["remote_bytes"] == 4096
    assert view["sweeps_at"] == finished + retention(daemon)
    assert "pull_min_free" in view["last_error"]

    clock.advance(retention(daemon))
    daemon.housekeep()  # nobody pulled it: swept
    view = persist_of(daemon, job_id)
    assert view["swept_at"] == clock.t and view["swept_bytes"] == 4096
    assert view["sweeps_at"] is None and view["remote_bytes"] == 0


def test_persist_of_a_kept_pull_still_counts_down_to_the_sweep(cloud, repo, tmp_path):
    daemon, provider = cloud
    job_id = finish(daemon, provider, repo)
    provider.persist(job_id, "checkpoint.pt", b"weights")
    daemon.pull(job_id, dest=tmp_path / "out", keep=True)

    view = persist_of(daemon, job_id)
    assert view["files"] == 1 and view["pulled_to"] == str(tmp_path / "out")
    assert not view["remote_deleted"] and view["remote_bytes"] == len(b"weights")
    assert view["sweeps_at"] == daemon.store.cloud_finished_at(job_id) + retention(daemon)


def test_the_cloud_status_counts_what_is_known_to_be_stored_per_target(cloud, repo, clock):
    daemon, provider = cloud
    left = left_unpulled(daemon, provider, repo, b"w" * 4096)
    left_unpulled(daemon, provider, repo, b"x" * 1000)
    roomy(daemon)
    job_id = start(daemon, repo)
    provider.persist(job_id, "checkpoint.pt", b"pulled")
    provider.finish(handle_of(daemon, job_id), 0)
    daemon.tick()  # pulled whole, remote copy deleted: holds nothing

    [target] = cloud_status_view(daemon, clock.t, {})["targets"]
    assert target["known_stored_bytes"] == 5096 and target["known_stored_jobs"] == 2

    daemon.store.mark_cloud_swept(left, clock.t, 1, 4096)
    [target] = cloud_status_view(daemon, clock.t, {})["targets"]
    assert target["known_stored_bytes"] == 1000 and target["known_stored_jobs"] == 1


def test_the_cloud_status_says_whose_account_pays_for_each_target(make_cloud, clock):
    daemon, _ = make_cloud(owner="First Owner")
    [target] = cloud_status_view(daemon, clock.t, {})["targets"]
    assert target["owner"] == "First Owner"


# ---- the per-job lifetime cap

H100_RATE = hourly_rate(FakeProvider().rates(), "h100", 1)  # $5.278/hour


def spend(daemon, provider, clock, job_id, dollars, attempt=1):
    """Run the job's live attempt for exactly `dollars` of H100 time, then let it pause."""
    clock.advance(dollars / H100_RATE * 3600)
    out_of_time(daemon, provider, job_id, attempt)
    daemon.tick()
    assert daemon.job(job_id).state == State.AWAITING


def test_submit_is_refused_when_the_estimate_is_past_the_jobs_cap(make_cloud, repo):
    # 3h of H100 at $5.28/hour is $15.83, past the $10 one job may spend. Pinned rates add an L4
    # ($2.13/hour with the sandbox, $6.38 for 3h, which fits) and an A100 ($3.83/hour, $11.48,
    # which does not); the refusal has to point at the one that fits.
    daemon, _ = make_cloud(rates={"gpu_hour_cost_l4": 0.80, "gpu_hour_cost_a100": 2.50})
    with pytest.raises(ValueError) as excinfo:
        daemon.submit(cloud_spec(repo, est_runtime=3 * 3600))
    msg = str(excinfo.value)
    assert "$15.83" in msg and "$10.00" in msg
    assert "L4 ($6.38)" in msg and "A100" not in msg
    assert "max_job_cost" in msg and "config" in msg and "not by an agent" in msg
    assert daemon.store.list_jobs() == []


def test_the_job_cap_refusal_suggests_each_gpu_once_even_under_two_spellings(make_cloud, repo):
    # The rate map carries this GPU under both spellings (see pasar.cloud.base.aliased); it must
    # be suggested once, by one name, not twice under two names for the same physical card.
    daemon, _ = make_cloud(rates={"gpu_hour_cost_a100_80gb": 0.80, "gpu_hour_cost_a100-80gb": 0.80})
    with pytest.raises(ValueError) as excinfo:
        daemon.submit(cloud_spec(repo, est_runtime=3 * 3600))
    msg = str(excinfo.value)
    assert msg.count("A100") == 1  # not "A100_80GB (...), A100-80GB (...)" -- the actual old bug
    assert "A100-80GB ($6.38)" in msg


def test_the_job_cap_refusal_names_each_gpu_the_way_pasar_cloud_does(make_cloud, repo, tmp_path):
    # A live Modal rate table carries A10 under `a10g` and RTX-PRO-6000 under `rtx6000` (plus the
    # aliases `aliased` adds for each — see modal_provider.GPU_NAMES); walking the raw rate keys
    # with no naming table suggests each of these twice, once per spelling. The refusal must name
    # each GPU the one way `pasar cloud`/`--gpu` know it by, exactly once.
    sdk = FakeSDK()
    sdk.rates_value = {"gpu_hour_cost_h100": 7.90, "gpu_hour_cost_a10g": 0.20,
                       "gpu_hour_cost_rtx6000": 0.40, "cpu_hour_cost_sandbox": 0.1419,
                       "mem_gib_hour_cost_sandbox": 0.024}
    modal_target = CloudTarget(name="fake", provider="modal", daily_budget=50.0,
                               monthly_budget=300.0)
    provider = ModalProvider(modal_target, tmp_path / "modal-state", sdk=sdk)
    try:
        daemon, _ = make_cloud(provider=provider)
        with pytest.raises(ValueError) as excinfo:
            daemon.submit(cloud_spec(repo, est_runtime=3 * 3600))
        msg = str(excinfo.value)
        # "A10G" and "RTX6000" (the raw billing spellings) both contain these substrings too, so
        # a count of 1 only holds once each GPU is named exactly one way.
        assert msg.count("A10") == 1
        assert msg.count("6000") == 1
        assert "A10 ($" in msg and "RTX-PRO-6000 ($" in msg
    finally:
        provider.close()


def modal_daemon(make_cloud, tmp_path, live, **target_kw):
    """A daemon over a real `ModalProvider` (on the fake SDK) whose live price list is `live`:
    the provider's own GPU spellings are what these rate tests are about."""
    sdk = FakeSDK()
    sdk.rates_value = live
    target = CloudTarget(name="fake", provider="modal", daily_budget=50.0, monthly_budget=300.0)
    provider = ModalProvider(target, tmp_path / "modal-state", sdk=sdk)
    daemon, _ = make_cloud(provider=provider, **target_kw)
    return daemon, provider


SANDBOX = {"cpu_hour_cost_sandbox": 0.1419, "mem_gib_hour_cost_sandbox": 0.024}


@pytest.mark.parametrize("key", ["gpu_hour_cost_a100_80gb", "gpu_hour_cost_a100-80gb"])
def test_a_config_rate_prices_every_spelling_of_its_gpu(make_cloud, tmp_path, key):
    # An override under Modal's own key must reach `--gpu A100-80GB` too, and one written the
    # way `--gpu` spells it must reach the row `pasar cloud` lists: one GPU, one price.
    daemon, provider = modal_daemon(make_cloud, tmp_path,
                                    {"gpu_hour_cost_a100_80gb": 2.5, **SANDBOX},
                                    rates={key: 1.0})
    try:
        target = daemon.cfg.clouds["fake"]
        rates = daemon.cloud_rates(target)
        assert rates["gpu_hour_cost_a100-80gb"] == rates["gpu_hour_cost_a100_80gb"] == 1.0
        [row] = daemon.cloud_gpus(target)
        assert row.name == "A100-80GB" and row.hourly_rate == 1.0
        assert daemon._hourly(target, "A100-80GB") == hourly_rate(rates, "a100-80gb", 1)
    finally:
        provider.close()


def test_a_config_rate_under_modals_billing_name_prices_the_name_gpu_accepts(make_cloud,
                                                                              tmp_path):
    daemon, provider = modal_daemon(make_cloud, tmp_path, {"gpu_hour_cost_a10g": 1.1, **SANDBOX},
                                    rates={"gpu_hour_cost_a10g": 0.5})
    try:
        target = daemon.cfg.clouds["fake"]
        assert daemon.cloud_rates(target)["gpu_hour_cost_a10"] == 0.5
        assert [(r.name, r.hourly_rate) for r in daemon.cloud_gpus(target)] == [("A10", 0.5)]
    finally:
        provider.close()


def test_config_rates_alone_price_every_gpu_they_list(make_cloud, tmp_path):
    # No live price list at all (the provider could not report one): what the config pins is
    # all there is, and each GPU it names must still price under the name `pasar cloud` shows.
    daemon, provider = modal_daemon(make_cloud, tmp_path, {},
                                    rates={"gpu_hour_cost_a100_80gb": 2.0,
                                           "gpu_hour_cost_rtx6000": 3.0, **SANDBOX})
    try:
        target = daemon.cfg.clouds["fake"]
        for row in daemon.cloud_gpus(target):
            assert daemon._hourly(target, row.name) > 0
        assert {r.name for r in daemon.cloud_gpus(target)} == {"A100-80GB", "RTX-PRO-6000"}
    finally:
        provider.close()


def test_the_padded_ceiling_is_not_what_submit_refuses(cloud, repo):
    # 1h30m estimates $7.92, under the cap; the 1.5x window around it would cost $11.88, over it.
    # That is not a reason to refuse: the cap turns into a shorter window, like --max-cost does.
    daemon, _ = cloud
    job = daemon.submit(cloud_spec(repo, est_runtime=5400))
    assert job.state == State.AWAITING
    est, top = daemon.cloud_estimate(job)
    assert est == pytest.approx(7.917, abs=1e-3) and top == pytest.approx(10.0)
    approved, full = daemon.cloud_window(job)
    assert full == 8100 and approved == int(10.0 / H100_RATE * 3600)


def test_a_second_attempt_may_only_spend_what_the_first_left_under_the_cap(cloud, repo, clock):
    daemon, provider = cloud
    job_id = start(daemon, repo)
    spend(daemon, provider, clock, job_id, 7.0)
    assert daemon.ledger.job_spent(job_id) == pytest.approx(7.0)

    job = daemon.approve(job_id)
    assert daemon.store.approvals(job_id)[1]["max_cost"] == pytest.approx(3.0)
    approved, full = daemon.cloud_window(job)
    assert full == 5400 and approved == int(3.0 / H100_RATE * 3600)  # 2046s, not 5400s
    daemon.tick()
    assert daemon.job(job_id).state == State.RUNNING
    row = next(r for r in daemon.store.cloud_spend("fake") if r["attempt"] == 2)
    assert row["estimated"] == pytest.approx(3.0)  # the ledger holds the capped figure too

    started = daemon.store.current_attempt(job_id).start_time
    clock.t = started + 3.0 / H100_RATE * 3600 - 1
    daemon.tick()
    assert daemon.job(job_id).state == State.RUNNING
    clock.advance(2)
    daemon.tick()
    assert daemon.job(job_id).state == State.STOPPING
    events = [e for e in daemon.store.machine_events() if e["kind"] == "job_cap"]
    assert len(events) == 1 and "$10.00" in events[0]["text"]
    assert not [e for e in daemon.store.machine_events() if e["kind"] == "max_cost"]


def test_a_job_that_has_spent_its_cap_stays_awaiting_until_the_cap_is_raised(cloud, repo, clock):
    daemon, provider = cloud
    job_id = start(daemon, repo)
    spend(daemon, provider, clock, job_id, 7.0)
    daemon.approve(job_id)
    daemon.tick()
    clock.advance(3.0 / H100_RATE * 3600 + 1)
    daemon.tick()  # paused at the cap
    provider.emit(handle_of(daemon, job_id),
                  ctl(daemon, job_id, {"t": "exit", "code": 143, "signal": None,
                                       "reason": "stopped"}, attempt=2))
    daemon.tick()

    job = daemon.job(job_id)
    assert job.state == State.AWAITING and job.reason == "job_cap"
    assert "$10.00" in job.summary and "max_job_cost" in job.summary
    # It does not wait for good: approval_ttl after it started waiting it is cancelled like
    # any job nobody approved, and the message says when. A resubmit is no way round that
    # either: it would be a new job, starting over, not resuming from this one's checkpoint.
    ttl = daemon.cfg.clouds["fake"].approval_ttl
    expires = time.strftime("%Y-%m-%d %H:%M", time.localtime(job.queue_time + ttl))
    assert expires in job.summary and "approval_ttl" in job.summary
    assert "starts over" in job.summary and "pasar pull" in job.summary
    with pytest.raises(Conflict, match="max_job_cost") as refused:
        daemon.approve(job_id)
    assert expires in str(refused.value)
    job = daemon.job(job_id)
    assert job.state == State.AWAITING  # not failed: a raised cap lets it carry on
    assert len(daemon.store.approvals(job_id)) == 2  # and the refusal wrote nothing

    daemon.cfg.clouds["fake"].max_job_cost = 20.0
    assert daemon.approve(job_id).state == State.QUEUED
    assert daemon.store.approvals(job_id)[2]["max_cost"] == pytest.approx(
        estimate(H100_RATE, 5400))  # $10 left now, so the window's own $7.92 binds again


def test_a_queued_job_whose_cap_was_lowered_is_sent_back_rather_than_launched(cloud, repo, clock):
    daemon, provider = cloud
    job_id = start(daemon, repo)
    spend(daemon, provider, clock, job_id, 7.0)
    daemon.approve(job_id)
    daemon.cfg.clouds["fake"].max_job_cost = 5.0  # below the $7 it has already spent
    daemon.tick()
    job = daemon.job(job_id)
    assert job.state == State.AWAITING and job.reason == "job_cap"
    assert len(provider.boxes) == 1  # only the first attempt ever launched


def test_max_cost_below_what_is_left_under_the_cap_still_wins(cloud, repo, clock):
    daemon, provider = cloud
    job_id = start(daemon, repo, max_cost=6.0)
    spend(daemon, provider, clock, job_id, 3.0)
    job = daemon.approve(job_id)  # $7 left under the cap, but --max-cost says $6
    assert daemon.store.approvals(job_id)[1]["max_cost"] == pytest.approx(6.0)
    assert daemon.cloud_window(job)[0] == int(6.0 / H100_RATE * 3600)


def test_the_job_view_carries_the_cap_and_what_the_job_has_spent(cloud, repo, clock):
    daemon, provider = cloud
    job = daemon.submit(cloud_spec(repo))
    view = cloud_view(daemon, job)
    assert view["job_cap"] == 10.0 and view["job_spent"] == 0.0
    daemon.approve(job.id)
    daemon.tick()
    # a live attempt counts at what it holds, since that is what it may still bill
    assert cloud_view(daemon, daemon.job(job.id))["job_spent"] == pytest.approx(
        estimate(H100_RATE, 5400))
    spend(daemon, provider, clock, job.id, 2.0)
    assert cloud_view(daemon, daemon.job(job.id))["job_spent"] == pytest.approx(2.0)


# ---- submitting to a group of accounts


@pytest.fixture
def make_group(tmp_path, clock, executor, probe, platform_check):
    """Two interchangeable accounts, modal-a (alice's) and modal-b (bob's), in group "modal",
    each with $30 a month. `reachable` names the ones that have a provider on this pasard."""
    data = tmp_path / "group-data"
    data.mkdir()

    def make(reachable=("modal-a", "modal-b"), a=None, b=None):
        base = {"provider": "modal", "group": "modal", "daily_budget": 30.0,
                "monthly_budget": 30.0, "max_running": 2}
        cfg = Config(clouds={
            "modal-a": CloudTarget(**{**base, "name": "modal-a", "owner": "alice", **(a or {})}),
            "modal-b": CloudTarget(**{**base, "name": "modal-b", "owner": "bob", **(b or {})}),
        })
        daemon = Daemon(cfg, Store(data / "pasar.db"), executor, probe, data, clock=clock,
                        providers={name: FakeProvider() for name in reachable},
                        platform_check=platform_check, background=run_now)
        daemon.pull_settle = 0
        return daemon

    return make


@pytest.fixture
def grouped(make_group):
    return make_group()


def spent(daemon, target, dollars):
    """A finished, billed attempt on `target` this month, so its headroom is `dollars` less."""
    job_id = daemon.store.insert_job(
        JobSpec(command="x", est_runtime=60, cwd="/tmp", target=target, gpu="H100"), 1000,
        daemon.clock(), None)
    daemon.store.update_job(job_id, state=State.COMPLETED)
    daemon.ledger.record(target, job_id, 1, estimated=dollars, billed=dollars)


def test_submitting_to_a_group_picks_a_member_and_records_both(grouped, repo):
    job = grouped.submit(cloud_spec(repo, target="modal", est_runtime=600))
    assert job.spec.target in ("modal-a", "modal-b")
    assert job.spec.group == "modal"
    assert job.state == State.AWAITING
    stored = grouped.job(job.id).spec
    assert (stored.target, stored.group) == (job.spec.target, "modal")


def test_a_group_submit_packs_onto_the_account_already_in_use(grouped, repo):
    # modal-b is second in config order, so only the packing rule can pick it
    spent(grouped, "modal-b", 25.0)
    job = grouped.submit(cloud_spec(repo, target="modal", est_runtime=600))
    assert job.spec.target == "modal-b"


def test_a_group_submit_prefers_an_account_with_a_free_slot(make_group, repo):
    daemon = make_group(a={"max_running": 1}, b={"max_running": 1})
    start(daemon, repo, target="modal-a")  # modal-a is fuller, but now has no slot
    job = daemon.submit(cloud_spec(repo, target="modal", est_runtime=600))
    assert job.spec.target == "modal-b"


def test_a_group_submit_is_refused_when_no_account_can_afford_it(grouped, repo):
    """Queueing for three weeks until credit resets is a job you forget you submitted."""
    for name in ("modal-a", "modal-b"):
        spent(grouped, name, 29.9)
    with pytest.raises(Conflict) as e:
        grouped.submit(cloud_spec(repo, target="modal", est_runtime=3600))
    msg = str(e.value)
    assert "modal-a" in msg and "modal-b" in msg
    assert "alice" in msg and "bob" in msg
    assert "$0.10" in msg and "$30.00" in msg
    assert f"${estimate(H100_RATE, 5400):.2f}" in msg  # what it needs: the padded ceiling
    assert "--on" in msg                                # the escape hatch is in the message
    assert grouped.store.list_jobs([State.AWAITING]) == []


def test_submitting_to_a_named_account_still_queues_when_it_cannot_afford_it(grouped, repo):
    """The explicit form is how you say 'I know, do it anyway'; the budget gate stops it later."""
    spent(grouped, "modal-a", 29.9)
    job = grouped.submit(cloud_spec(repo, target="modal-a", est_runtime=3600))
    assert job.spec.target == "modal-a" and job.spec.group == ""
    assert job.state == State.AWAITING


def test_a_name_that_is_neither_a_target_nor_a_group_is_refused_naming_both(grouped, repo):
    with pytest.raises(ValueError, match="nowhere") as e:
        grouped.submit(cloud_spec(repo, target="nowhere"))
    assert "modal-a" in str(e.value) and "groups: modal" in str(e.value)


def test_a_group_submit_skips_an_account_whose_job_cap_the_estimate_is_over(make_group, repo):
    # 1h of H100 is $5.28: over modal-a's $5 cap, so the fuller modal-a is not a candidate
    daemon = make_group(a={"max_job_cost": 5.0})
    spent(daemon, "modal-a", 20.0)
    job = daemon.submit(cloud_spec(repo, target="modal", est_runtime=3600))
    assert job.spec.target == "modal-b"


def test_a_group_submit_over_every_accounts_job_cap_gets_the_cap_refusal(grouped, repo):
    # 3h of H100 is $15.83, over both $10 caps: the refusal is the one a named account gives,
    # with its figures and its advice, rather than a claim that nobody has the money
    with pytest.raises(ValueError) as e:
        grouped.submit(cloud_spec(repo, target="modal", est_runtime=3 * 3600))
    msg = str(e.value)
    assert "$15.83" in msg and "max_job_cost" in msg and "modal-a" in msg
    assert grouped.store.list_jobs() == []


def test_a_group_submit_skips_an_account_without_a_provider(make_group, repo):
    daemon = make_group(reachable=("modal-b",))
    spent(daemon, "modal-a", 20.0)  # the fuller one, if only it could be reached
    job = daemon.submit(cloud_spec(repo, target="modal", est_runtime=600))
    assert job.spec.target == "modal-b"


def test_a_group_with_no_reachable_account_says_so_rather_than_blaming_money(make_group, repo):
    daemon = make_group(reachable=())
    with pytest.raises(ValueError, match="no provider") as e:
        daemon.submit(cloud_spec(repo, target="modal", est_runtime=600))
    msg = str(e.value)
    assert "modal-a" in msg and "modal-b" in msg
    assert "configured but its provider could not be set up" in msg


# ---- moving a group job that has not launched yet


def moves(daemon):
    return [e for e in daemon.store.machine_events(limit=100) if e["kind"] == "cloud_moved"]


def test_a_waiting_job_moves_to_an_account_that_can_still_afford_it(grouped, repo):
    job = grouped.submit(cloud_spec(repo, target="modal", est_runtime=3600))
    first = job.spec.target
    spent(grouped, first, 25.0)  # something else ate that account: $5 left, the job needs $7.92
    grouped.tick()
    moved = grouped.job(job.id)
    assert moved.spec.target != first and moved.spec.group == "modal"
    assert moved.state == State.AWAITING


def test_a_job_that_has_already_run_is_never_moved(grouped, repo):
    """Its checkpoint is on that account's volume; another account cannot read it, so moving the
    job would silently restart work that has already been paid for."""
    job_id = start(grouped, repo, target="modal", est_runtime=3600)
    first = grouped.job(job_id).spec.target
    grouped.executors[first].provider.reclaim(handle_of(grouped, job_id))
    grouped.tick()
    assert grouped.job(job_id).state == State.AWAITING  # paused after running, waiting again
    spent(grouped, first, 29.9)
    grouped.tick()
    assert grouped.job(job_id).spec.target == first
    assert moves(grouped) == []


def test_a_job_with_spend_on_its_account_is_never_moved(grouped, repo):
    """Spend is the other sign a job has used its account, even without an attempt row."""
    job = grouped.submit(cloud_spec(repo, target="modal", est_runtime=3600))
    first = job.spec.target
    grouped.ledger.record(first, job.id, 1, estimated=1.0, billed=1.0)
    spent(grouped, first, 28.0)
    grouped.tick()
    assert grouped.job(job.id).spec.target == first


def test_a_job_pinned_to_a_named_account_is_never_moved(grouped, repo):
    job = grouped.submit(cloud_spec(repo, target="modal-a", est_runtime=3600))
    spent(grouped, "modal-a", 29.9)
    grouped.tick()
    assert grouped.job(job.id).spec.target == "modal-a"
    assert moves(grouped) == []


def test_a_move_is_recorded_where_a_person_can_see_it(grouped, repo):
    job = grouped.submit(cloud_spec(repo, target="modal", est_runtime=3600))
    spent(grouped, "modal-a", 25.0)
    grouped.tick()
    [event] = moves(grouped)
    text = event["text"]
    assert f"job {job.id}" in text and "modal-a" in text and "modal-b (bob)" in text
    assert f"${estimate(H100_RATE, 5400):.2f}" in text  # what it needs


def test_a_job_that_cannot_move_anywhere_stays_where_it_is(grouped, repo):
    """Not an error: the budget gate blocks it with a reason, and credit resets eventually."""
    job = grouped.submit(cloud_spec(repo, target="modal", est_runtime=3600))
    first = job.spec.target
    for name in ("modal-a", "modal-b"):
        spent(grouped, name, 29.9)
    grouped.tick()
    assert grouped.job(job.id).spec.target == first
    assert grouped.job(job.id).state == State.AWAITING
    assert moves(grouped) == []


def test_a_job_does_not_move_to_an_account_whose_job_cap_its_estimate_is_over(make_group, repo):
    # 1h of H100 is $5.28, over modal-b's $5 cap: modal-b would have refused it at submit
    daemon = make_group(b={"max_job_cost": 5.0})
    job = daemon.submit(cloud_spec(repo, target="modal", est_runtime=3600))
    assert job.spec.target == "modal-a"
    spent(daemon, "modal-a", 25.0)
    daemon.tick()
    assert daemon.job(job.id).spec.target == "modal-a"


def test_a_job_does_not_move_to_an_account_without_a_provider(make_group, repo):
    daemon = make_group(reachable=("modal-a",))
    job = daemon.submit(cloud_spec(repo, target="modal", est_runtime=3600))
    spent(daemon, "modal-a", 25.0)
    daemon.tick()
    assert daemon.job(job.id).spec.target == "modal-a"


def test_jobs_submitted_together_are_spread_once_one_account_is_full(grouped, repo):
    """The submit-time choice cannot see jobs that are waiting, only money already spent or held,
    so several quick submits all land on the fullest account; the tick then moves the ones it
    cannot pay for all of, and leaves them there on the next."""
    spent(grouped, "modal-a", 15.0)  # $15 left: room for one $7.92 run, not two
    first = grouped.submit(cloud_spec(repo, target="modal", est_runtime=3600))
    second = grouped.submit(cloud_spec(repo, target="modal", est_runtime=3600))
    assert first.spec.target == second.spec.target == "modal-a"
    grouped.tick()
    assert grouped.job(first.id).spec.target == "modal-a"
    assert grouped.job(second.id).spec.target == "modal-b"
    grouped.tick()
    grouped.tick()
    assert len(moves(grouped)) == 1


def test_an_approved_job_keeps_its_place_ahead_of_one_still_awaiting(grouped, repo):
    spent(grouped, "modal-a", 15.0)
    earlier = grouped.submit(cloud_spec(repo, target="modal", est_runtime=3600))
    later = grouped.submit(cloud_spec(repo, target="modal", est_runtime=3600))
    grouped.approve(later.id)
    grouped.tick()  # the move comes before the lane, which then launches the approved one
    assert grouped.job(later.id).spec.target == "modal-a"
    assert grouped.job(earlier.id).spec.target == "modal-b"


def test_a_job_that_cannot_move_claims_its_account_first(grouped, repo):
    spent(grouped, "modal-a", 15.0)
    movable = grouped.submit(cloud_spec(repo, target="modal", est_runtime=3600))
    named = grouped.submit(cloud_spec(repo, target="modal-a", est_runtime=3600))
    grouped.tick()
    assert grouped.job(named.id).spec.target == "modal-a"
    assert grouped.job(movable.id).spec.target == "modal-b"


def test_a_moved_approved_job_must_be_approved_again(grouped, repo, clock):
    """An approval says whose credit is spent (the approve dialog names the owner), and the
    approvals row does not record an account, so a job moved to another one is asked again
    rather than spending bob's credit on a yes given for alice's."""
    job = grouped.submit(cloud_spec(repo, target="modal", est_runtime=3600))
    assert job.spec.target == "modal-a"
    grouped.approve(job.id)
    spent(grouped, "modal-a", 25.0)
    clock.advance(30)
    grouped.tick()
    moved = grouped.job(job.id)
    assert moved.spec.target == "modal-b"
    assert moved.state == State.AWAITING and moved.reason == "moved"
    assert moved.queue_time == clock.t  # a fresh approval_ttl to approve it again
    assert "approve" in moved.summary and "bob" in moved.summary
    assert grouped.store.approvals(job.id) == []
    for name in ("modal-a", "modal-b"):
        assert grouped.executors[name].provider.boxes == {}
    grouped.approve(job.id)
    grouped.tick()
    assert grouped.job(job.id).state == State.RUNNING
    assert parse_unit(grouped.store.current_attempt(job.id).unit)[0] == "modal-b"


def test_a_moved_job_that_was_never_approved_says_why_without_asking_again(grouped, repo):
    job = grouped.submit(cloud_spec(repo, target="modal", est_runtime=3600))
    spent(grouped, "modal-a", 25.0)
    grouped.tick()
    moved = grouped.job(job.id)
    assert moved.spec.target == "modal-b"
    assert moved.state == State.AWAITING and moved.reason == "moved"
    assert "modal-a" in moved.summary and "modal-b" in moved.summary
    assert "again" not in moved.summary  # nobody approved it, so nothing is asked again
    assert moved.queue_time == job.queue_time  # its approval_ttl runs from submit, as before


def test_an_approved_job_claims_its_account_before_an_unapproved_one_that_cannot_move(
        grouped, repo):
    """A job nobody has approved must not push out one somebody has, even if it is pinned."""
    spent(grouped, "modal-a", 15.0)  # room for one $7.92 run
    approved = grouped.submit(cloud_spec(repo, target="modal", est_runtime=3600))
    grouped.approve(approved.id)
    grouped.submit(cloud_spec(repo, target="modal-a", est_runtime=3600))  # named, awaiting
    grouped.tick()
    assert grouped.job(approved.id).spec.target == "modal-a"
    assert grouped.store.approvals(approved.id) != []
    assert moves(grouped) == []


def test_a_moved_job_bounced_back_to_awaiting_loses_its_stale_reason_and_approval(grouped, repo):
    """Approved, then sent back to awaiting (the price rose before launch): the approvals row is
    still there, and so are the reason and summary of a bounce that no longer describe it."""
    job = grouped.submit(cloud_spec(repo, target="modal", est_runtime=3600))
    grouped.approve(job.id)
    grouped.store.update_job(job.id, state=State.AWAITING, reason="price_rose",
                             summary="the price rose past what was approved")
    spent(grouped, "modal-a", 25.0)
    grouped.tick()
    moved = grouped.job(job.id)
    assert moved.spec.target == "modal-b" and moved.state == State.AWAITING
    assert moved.reason == "moved" and "price" not in moved.summary
    assert "approve it again" in moved.summary and "bob" in moved.summary
    assert grouped.store.approvals(job.id) == []


def test_a_job_that_never_ran_leaves_an_account_whose_provider_failed(grouped, repo):
    """Not stranded: another account in its group can take it, so it goes there, and nothing
    says it is waiting for the provider to come back."""
    job = grouped.submit(cloud_spec(repo, target="modal", est_runtime=3600))
    assert job.spec.target == "modal-a"
    del grouped.executors["modal-a"]  # its provider failed to start this time
    grouped.tick()
    assert grouped.job(job.id).spec.target == "modal-b"
    assert grouped.job(job.id).state == State.AWAITING
    events = grouped.store.machine_events(limit=100)
    assert [e for e in events if e["kind"] == "target_gone"] == []
    [event] = moves(grouped)
    assert "provider could not be set up" in event["text"]


def test_a_job_that_never_ran_and_fits_nowhere_else_is_still_said_to_be_stranded(grouped, repo):
    job = grouped.submit(cloud_spec(repo, target="modal", est_runtime=3600))
    del grouped.executors["modal-a"]
    spent(grouped, "modal-b", 29.9)
    grouped.tick()
    assert grouped.job(job.id).spec.target == "modal-a"
    events = grouped.store.machine_events(limit=100)
    assert any(e["kind"] == "target_gone" and f"job {job.id}" in e["text"] for e in events)


def test_a_job_that_cannot_be_priced_is_left_alone(grouped, repo, monkeypatch):
    job = grouped.submit(cloud_spec(repo, target="modal", est_runtime=3600))
    spent(grouped, "modal-a", 25.0)
    for name in ("modal-a", "modal-b"):
        monkeypatch.setattr(grouped.executors[name].provider, "rates", dict)
    grouped._rates.clear()
    grouped.tick()
    assert grouped.job(job.id).spec.target == "modal-a"


def test_a_job_that_fits_where_it_is_prices_no_other_account(grouped, repo, clock):
    """The rebalance runs every tick, so its common case must cost nothing the lane does not."""
    grouped.submit(cloud_spec(repo, target="modal", est_runtime=3600))
    clock.advance(CLOUD_RATE_TTL + 1)
    other = grouped.executors["modal-b"].provider
    other.rates_calls = 0
    grouped.tick()
    assert other.rates_calls == 0
