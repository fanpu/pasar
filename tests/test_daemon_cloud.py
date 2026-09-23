"""Cloud jobs through the daemon: approval, the cloud lane, and how a cloud attempt ends.

Local behaviour is covered by test_daemon_tick.py and test_daemon_preempt.py; what matters here
is that a cloud job never touches the local memory pool, never costs money without a person
saying so, and never leaves a sandbox running that nothing owns.
"""

import json
import subprocess
from dataclasses import replace

import pytest

from pasar.cloud.cost import estimate, hourly_rate
from pasar.cloud.executor import parse_unit
from pasar.config import CloudTarget, Config
from pasar.daemon import PAUSE_LIMIT, Conflict, Daemon
from pasar.db import Store
from pasar.models import EndKind, JobSpec, State
from pasar.units import GiB
from tests.fakes_cloud import FakeProvider, launch_request


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
def make_cloud(tmp_path, clock, executor, probe):
    data = tmp_path / "data"
    data.mkdir()

    def make(provider=None, metrics=None, **target_kw):
        provider = provider or FakeProvider()
        base = {"name": "fake", "provider": "fake", "daily_budget": 50.0,
                "monthly_budget": 300.0, "max_running": 2,
                "env_passthrough": ["WANDB_API_KEY"]}
        cfg = Config(clouds={"fake": CloudTarget(**{**base, **target_kw})})
        daemon = Daemon(cfg, Store(data / "pasar.db"), executor, probe, data, clock=clock,
                        metrics=metrics, providers={"fake": provider})
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
    assert env["PASAR_ATTEMPT"] == "2" and env["PASAR_RESUMING"] == "1"


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
