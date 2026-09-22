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
    assert set(m) == set(mascot.STATES)


def test_manifest_missing_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(mascot, "BUILTIN_DIR", tmp_path / "nope")
    m = mascot.manifest(tmp_path / "also-nope")
    assert all(v == [] for v in m.values())


def test_resolve_rejects_traversal_and_unknown(tmp_path):
    touch(tmp_path, "happy.png", "secret.png")
    assert mascot.resolve(tmp_path, "happy.png") == tmp_path / "happy.png"
    for bad in ["../happy.png", "secret.png", "happy.exe", "happy-1234.png", "HAPPY.png",
                "happy.png/..", ""]:
        assert mascot.resolve(tmp_path, bad) is None
    assert mascot.resolve(tmp_path, "done.png") is None  # valid name, no file


def test_builtin_art_complete():
    for state in mascot.STATES:
        data = (mascot.BUILTIN_DIR / f"{state}.png").read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", state
        assert len(data) < 128 * 1024, state
