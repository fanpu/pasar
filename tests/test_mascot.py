from pathlib import Path

from pasar import mascot


def touch(d: Path, *names):
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_bytes(b"x")


def test_states():
    assert mascot.STATES == ("idle", "happy", "start", "busy", "waiting", "sweat", "hot", "oom",
                             "failed", "preempted", "done", "thinking", "hmm", "confused")


def test_manifest_prefers_custom_and_orders_variants(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    touch(builtin, *(f"{s}.png" for s in mascot.STATES), "done-2.png")
    monkeypatch.setattr(mascot, "BUILTIN_DIR", builtin)
    custom = tmp_path / "custom"
    assert mascot.manifest(tmp_path / "none")["done"] == ["/mascot/builtin/done.png", "/mascot/builtin/done-2.png"]
    touch(custom, "done-10.png", "done.png", "done-2.png", "happy.webp", "notes.txt", "Happy.png")
    m = mascot.manifest(custom)
    assert m["done"] == ["/mascot/done.png", "/mascot/done-2.png", "/mascot/done-10.png"]
    assert m["happy"] == ["/mascot/happy.webp"]
    assert m["idle"] == ["/mascot/builtin/idle.png"]
    assert set(m) == set(mascot.STATES) | {"peek", "jobs"}


def test_manifest_missing_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(mascot, "BUILTIN_DIR", tmp_path / "nope")
    m = mascot.manifest(tmp_path / "also-nope")
    assert all(v == [] for k, v in m.items() if k != "jobs")
    assert m["jobs"] == {"local": {}, "cloud": {}}


def test_manifest_peek_is_custom_only_and_picks_first_variant(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    touch(builtin, "peek.png")  # a built-in peek image must never surface
    monkeypatch.setattr(mascot, "BUILTIN_DIR", builtin)
    assert mascot.manifest(tmp_path / "none")["peek"] == []

    custom = tmp_path / "custom"
    touch(custom, "peek-2.webp", "peek.webp")
    assert mascot.manifest(custom)["peek"] == ["/mascot/peek.webp"]


def test_resolve_rejects_traversal_and_unknown(tmp_path):
    touch(tmp_path, "happy.png", "secret.png")
    assert mascot.resolve(tmp_path, "happy.png") == tmp_path / "happy.png"
    for bad in ["../happy.png", "secret.png", "happy.exe", "happy-1234.png", "HAPPY.png",
                "happy.png/..", ""]:
        assert mascot.resolve(tmp_path, bad) is None
    assert mascot.resolve(tmp_path, "done.png") is None  # valid name, no file


def test_resolve_accepts_peek_and_its_variants(tmp_path):
    touch(tmp_path, "peek.gif", "peek-2.gif")
    assert mascot.resolve(tmp_path, "peek.gif") == tmp_path / "peek.gif"
    assert mascot.resolve(tmp_path, "peek-2.gif") == tmp_path / "peek-2.gif"


def test_job_manifest_groups_by_kind_and_state_with_ordered_variants(tmp_path):
    jobs = tmp_path / "jobs"
    touch(jobs, "local-running.png", "local-running-2.png", "local-running-10.png",
          "cloud-awaiting.webp", "cloud-paused.gif", "notes.txt", "local-nonsense.png",
          "LOCAL-running.png")
    m = mascot.job_manifest(tmp_path)
    assert m["local"]["running"] == [
        "/mascot/jobs/local-running.png", "/mascot/jobs/local-running-2.png",
        "/mascot/jobs/local-running-10.png",
    ]
    assert m["cloud"]["awaiting"] == ["/mascot/jobs/cloud-awaiting.webp"]
    assert m["cloud"]["paused"] == ["/mascot/jobs/cloud-paused.gif"]
    assert "nonsense" not in m["local"]
    assert set(m) == {"local", "cloud"}


def test_job_manifest_missing_dir_is_empty_for_both_kinds(tmp_path):
    assert mascot.job_manifest(tmp_path / "no-mascot-dir") == {"local": {}, "cloud": {}}


def test_job_manifest_is_never_builtin(tmp_path, monkeypatch):
    # There is no such thing as a built-in job sprite: job_manifest never even looks at
    # BUILTIN_DIR, so pointing it somewhere with matching names must not leak through.
    builtin = tmp_path / "builtin" / "jobs"
    touch(builtin, "local-running.png")
    monkeypatch.setattr(mascot, "BUILTIN_DIR", tmp_path / "builtin")
    assert mascot.job_manifest(tmp_path / "custom-without-jobs") == {"local": {}, "cloud": {}}


def test_resolve_job_rejects_traversal_and_unknown(tmp_path):
    touch(tmp_path / "jobs", "local-running.png", "secret.png")
    touch(tmp_path, "local-running.png")  # only the `jobs/` subfolder counts
    assert mascot.resolve_job(tmp_path, "local-running.png") == tmp_path / "jobs" / "local-running.png"
    for bad in ["../local-running.png", "secret.png", "local-running.exe", "local-bogus.png",
                "bogus-running.png", "LOCAL-running.png", "local-running.png/..", ""]:
        assert mascot.resolve_job(tmp_path, bad) is None
    assert mascot.resolve_job(tmp_path, "cloud-idle.png") is None  # valid name, no file


def test_resolve_job_accepts_variants(tmp_path):
    touch(tmp_path / "jobs", "cloud-preempted.svg", "cloud-preempted-3.svg")
    assert mascot.resolve_job(tmp_path, "cloud-preempted.svg") == tmp_path / "jobs" / "cloud-preempted.svg"
    assert mascot.resolve_job(tmp_path, "cloud-preempted-3.svg") == tmp_path / "jobs" / "cloud-preempted-3.svg"


def test_manifest_includes_job_sprites(tmp_path, monkeypatch):
    monkeypatch.setattr(mascot, "BUILTIN_DIR", tmp_path / "nope")
    touch(tmp_path / "custom" / "jobs", "local-running.png")
    m = mascot.manifest(tmp_path / "custom")
    assert m["jobs"]["local"]["running"] == ["/mascot/jobs/local-running.png"]
    assert m["jobs"]["cloud"] == {}


def test_builtin_art_complete():
    for state in mascot.STATES:
        data = (mascot.BUILTIN_DIR / f"{state}.png").read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", state
        assert len(data) < 128 * 1024, state
