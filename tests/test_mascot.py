import xml.etree.ElementTree as ET
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
    touch(builtin, *(f"{s}.svg" for s in mascot.STATES))
    monkeypatch.setattr(mascot, "BUILTIN_DIR", builtin)
    custom = tmp_path / "custom"
    touch(custom, "done-10.png", "done.png", "done-2.png", "happy.webp", "notes.txt", "Happy.png")
    m = mascot.manifest(custom)
    assert m["done"] == ["/mascot/done.png", "/mascot/done-2.png", "/mascot/done-10.png"]
    assert m["happy"] == ["/mascot/happy.webp"]
    assert m["idle"] == ["/mascot/builtin/idle.svg"]
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


def test_builtin_art_complete_and_safe():
    for state in mascot.STATES:
        path = mascot.BUILTIN_DIR / f"{state}.svg"
        text = path.read_text()
        assert len(text.encode()) < 6 * 1024, state
        root = ET.fromstring(text)
        assert root.tag.endswith("svg") and root.get("viewBox") == "0 0 128 128", state
        assert root.get("width") is None and root.get("height") is None, state
        lowered = text.lower()
        assert "<script" not in lowered and "http" not in lowered.replace("http://www.w3.org", ""), state
        assert "onload" not in lowered and "onclick" not in lowered, state
