import pytest

from pasar.db import Store
from pasar.models import Attempt, EndKind, JobSpec, State


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "pasar.db")


def spec(**kw):
    return JobSpec(**{"command": "python a.py", "est_runtime": 60, "cwd": "/tmp", **kw})


def test_insert_and_get_job(store):
    job_id = store.insert_job(spec(name="a", tags=["x"]), bid=1200, now=100.0, git_commit="abc")
    job = store.get_job(job_id)
    assert job.id == 1 and job.state == State.QUEUED and job.bid == 1200
    assert job.queue_time == job.submit_time == 100.0
    assert job.spec.tags == ["x"] and job.git_commit == "abc"
    assert store.get_job(99) is None


def test_list_filters_by_state_and_update(store):
    a = store.insert_job(spec(), 1000, 1.0, None)
    b = store.insert_job(spec(), 1000, 2.0, None)
    store.update_job(b, state=State.RUNNING, reason="x", spec=spec(note="changed"))
    assert [j.id for j in store.list_jobs([State.QUEUED])] == [a]
    assert [j.id for j in store.list_jobs([State.RUNNING])] == [b]
    assert store.get_job(b).spec.note == "changed"
    assert len(store.list_jobs()) == 2
    with pytest.raises(ValueError):
        store.update_job(a, nonsense=1)


def test_attempts_round_trip(store):
    j = store.insert_job(spec(), 1000, 1.0, None)
    store.insert_attempt(Attempt(j, 1, "pasar-job-1-1", 10.0))
    store.update_attempt(j, 1, end_time=20.0, end_kind=EndKind.PREEMPTED, wasted_work=3.5, peak_mem=7)
    store.insert_attempt(Attempt(j, 2, "pasar-job-1-2", 30.0))
    atts = store.attempts(j)
    assert [a.n for a in atts] == [1, 2]
    assert atts[0].end_kind == EndKind.PREEMPTED and atts[0].wasted_work == 3.5 and atts[0].peak_mem == 7
    assert store.current_attempt(j).n == 2


def test_events(store):
    j = store.insert_job(spec(), 1000, 1.0, None)
    assert not store.has_events(j)
    store.add_event(j, 1, 5.0, "checkpoint", 10, {"step": 10})
    store.add_event(j, 1, 6.0, "progress", 12, {"step": 12, "total_steps": 100})
    store.add_event(j, 2, 7.0, "checkpoint", 20, {"step": 20})
    assert store.has_events(j)
    assert store.last_event(j, "checkpoint")["ts"] == 7.0
    assert store.last_event(j, "checkpoint", attempt=1)["ts"] == 5.0
    assert store.last_event(j, "resumed") is None
    assert store.events(j)[1]["payload"] == {"step": 12, "total_steps": 100}


def test_machine_events_and_metrics(store):
    store.add_machine_event(1.0, "pressure", "started")
    store.add_machine_event(2.0, "oom_kill", "killed #3")
    assert [e["kind"] for e in store.machine_events()] == ["oom_kill", "pressure"]
    j = store.insert_job(spec(), 1000, 1.0, None)
    store.set_metric_summary(j, 1, "power_w", 60.0, 80.0, None)
    store.set_metric_summary(j, 1, "power_w", 61.0, 81.0, None)
    assert store.metric_summaries(j) == [
        {"attempt": 1, "metric": "power_w", "avg": 61.0, "max": 81.0, "total": None}
    ]


def test_progress_events_for_many_jobs_in_one_query(store):
    a = store.insert_job(spec(), 1000, 1.0, None)
    b = store.insert_job(spec(), 1000, 1.0, None)
    c = store.insert_job(spec(), 1000, 1.0, None)
    store.add_event(a, 1, 5.0, "progress", 1, {"step": 1, "loss": 0.9})
    store.add_event(a, 1, 6.0, "checkpoint", 1, {"step": 1})
    store.add_event(a, 1, 7.0, "progress", 2, {"step": 2, "loss": 0.5})
    store.add_event(b, 1, 8.0, "progress", 1, {"step": 1, "acc": 0.3})
    store.add_event(c, 1, 9.0, "progress", 1, {"step": 1, "loss": 0.1})

    got = store.progress_events([a, b])
    assert set(got) == {a, b}
    assert [(e["ts"], e["payload"]["loss"]) for e in got[a]] == [(5.0, 0.9), (7.0, 0.5)]
    assert [e["payload"]["acc"] for e in got[b]] == [0.3]
    assert store.progress_events([]) == {}


def test_gpu_samples_roundtrip(store):
    store.add_gpu_samples(1, 1, 100.0, [[0, 55.0, 1024.0, 81920.0, 240.5, 61.0]])
    rows = store.gpu_samples(1, 1)
    assert rows[0][1] == 0 and rows[0][2] == 55.0


def test_cloud_spend_roundtrip_and_scoping(store):
    j = store.insert_job(spec(), 1000, 1.0, None)
    store.record_cloud_spend("modal", j, 1, "2026-01-01", 5.0, None)
    store.record_cloud_spend("other", j, 1, "2026-01-01", 100.0, None)
    rows = store.cloud_spend("modal")
    assert len(rows) == 1
    assert rows[0]["job_id"] == j and rows[0]["estimated"] == 5.0 and rows[0]["billed"] is None
    assert store.cloud_spend_on("modal", "2026-01-01")[0]["estimated"] == 5.0
    assert store.cloud_spend_on("modal", "2026-01-02") == []
    assert len(store.cloud_spend_in_month("modal", "2026-01")) == 1
    assert store.cloud_spend_in_month("modal", "2026-02") == []


def test_cloud_spend_upsert_keeps_the_original_day(store):
    j = store.insert_job(spec(), 1000, 1.0, None)
    store.record_cloud_spend("modal", j, 1, "2026-01-01", 5.0, None)
    store.record_cloud_spend("modal", j, 1, "2026-01-02", 5.0, 7.25)
    rows = store.cloud_spend("modal")
    assert len(rows) == 1
    assert rows[0]["day"] == "2026-01-01"
    assert rows[0]["billed"] == 7.25


def test_cloud_spend_upsert_keeps_billed_once_set(store):
    """A later call made with billed=None (bookkeeping re-run after a restart, say) must not
    erase a real billed figure already on the row."""
    j = store.insert_job(spec(), 1000, 1.0, None)
    store.record_cloud_spend("modal", j, 1, "2026-01-01", 5.0, 7.25)
    store.record_cloud_spend("modal", j, 1, "2026-01-01", 5.0, None)
    rows = store.cloud_spend("modal")
    assert len(rows) == 1
    assert rows[0]["billed"] == 7.25


def test_cloud_spend_is_empty_for_a_target_with_no_rows(store):
    assert store.cloud_spend("modal") == []
    assert store.cloud_spend_on("modal", "2026-01-01") == []
    assert store.cloud_spend_in_month("modal", "2026-01") == []


def test_pulls_round_trip_and_the_latest_is_what_is_read(store):
    assert store.last_pull(1) is None and store.pulls(1) == []
    store.add_pull(1, 10.0, "/p/1", None, None, False, "no room")
    store.add_pull(1, 20.0, "/p/1", 2, 99, True, None)
    store.add_pull(1, 30.0, "/elsewhere", None, None, False, "not empty")
    store.add_pull(2, 15.0, None, 0, 0, False, None)

    assert [p["ts"] for p in store.pulls(1)] == [10.0, 20.0, 30.0]
    assert store.last_pull(1)["error"] == "not empty"
    # What landed is asked for separately: a later refusal (a bad --to, say) must not hide it.
    landed = store.last_pull(1, landed=True)
    assert landed == {"job_id": 1, "ts": 20.0, "dest": "/p/1", "files": 2, "bytes": 99,
                      "remote_deleted": True, "error": None}
    assert store.last_pull(2, landed=True)["files"] == 0
