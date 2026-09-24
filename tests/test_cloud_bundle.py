import os
import shutil
import subprocess
import sys
import tarfile

import pytest

from pasar.cloud.bundle import BundleError, build_bundle


def git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0"\n')
    (tmp_path / "uv.lock").write_text("version = 1\n")
    (tmp_path / "train.py").write_text("print('hi')\n")
    (tmp_path / ".gitignore").write_text("data/\n")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "big.bin").write_bytes(b"0" * 1024)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


def names(bundle):
    with tarfile.open(bundle.path) as t:
        return sorted(m.name for m in t.getmembers() if m.isfile())


def test_bundle_has_tracked_and_untracked_but_not_ignored(tmp_path):
    repo = git_repo(tmp_path / "repo")
    (repo / "notes.md").write_text("scratch\n")          # untracked, not ignored
    b = build_bundle(str(repo), tmp_path / "b.tar", max_bytes=1 << 20)
    assert "train.py" in names(b) and "notes.md" in names(b)
    assert not any(n.startswith("data/") for n in names(b))


def test_bundle_records_cwd_relative_to_repo_root(tmp_path):
    repo = git_repo(tmp_path / "repo")
    (repo / "sub").mkdir()
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    b = build_bundle(str(repo / "sub"), tmp_path / "b.tar", max_bytes=1 << 20)
    assert b.rel_cwd == "sub"


def test_bundle_over_limit_names_the_biggest_files(tmp_path):
    repo = git_repo(tmp_path / "repo")
    (repo / "weights.pt").write_bytes(b"0" * 4096)
    with pytest.raises(BundleError, match="weights.pt"):
        build_bundle(str(repo), tmp_path / "b.tar", max_bytes=2048)


def test_bundle_needs_a_lockfile(tmp_path):
    repo = git_repo(tmp_path / "repo")
    (repo / "uv.lock").unlink()
    with pytest.raises(BundleError, match="uv.lock"):
        build_bundle(str(repo), tmp_path / "b.tar", max_bytes=1 << 20)


def test_bundle_outside_a_repo(tmp_path):
    (tmp_path / "loose").mkdir()
    with pytest.raises(BundleError, match="git repository"):
        build_bundle(str(tmp_path / "loose"), tmp_path / "b.tar", max_bytes=1 << 20)


def test_bundle_keeps_a_tracked_non_ascii_name(tmp_path):
    repo = git_repo(tmp_path / "repo")
    (repo / "café.txt").write_text("scratch\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    b = build_bundle(str(repo), tmp_path / "b.tar", max_bytes=1 << 20)
    assert "café.txt" in names(b)


def test_bundle_rejects_a_symlink_pointing_outside_the_repo(tmp_path):
    repo = git_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.bin").write_bytes(b"x")
    (repo / "link_out").symlink_to(outside / "secret.bin")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    with pytest.raises(BundleError, match="link_out"):
        build_bundle(str(repo), tmp_path / "b.tar", max_bytes=1 << 20)


def test_bundle_keeps_a_symlink_pointing_inside_the_repo(tmp_path):
    repo = git_repo(tmp_path / "repo")
    (repo / "real.txt").write_text("hi\n")
    (repo / "link_in").symlink_to(repo / "real.txt")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    b = build_bundle(str(repo), tmp_path / "b.tar", max_bytes=1 << 20)
    with tarfile.open(b.path) as t:
        assert "link_in" in [m.name for m in t.getmembers() if m.issym()]


def test_bundle_file_vanishing_mid_archive_raises_bundle_error(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    real_add = tarfile.TarFile.add

    def flaky_add(self, name, *a, arcname=None, **kw):
        if arcname == "train.py":
            raise FileNotFoundError(arcname)
        return real_add(self, name, *a, arcname=arcname, **kw)

    monkeypatch.setattr(tarfile.TarFile, "add", flaky_add)
    with pytest.raises(BundleError, match="train.py"):
        build_bundle(str(repo), tmp_path / "b.tar", max_bytes=1 << 20)


def test_env_key_is_stable_and_changes_with_lock_contents(tmp_path):
    repo_a = git_repo(tmp_path / "a")
    repo_b = git_repo(tmp_path / "b")
    key_a = build_bundle(str(repo_a), tmp_path / "a.tar", max_bytes=1 << 20).env.key
    key_b = build_bundle(str(repo_b), tmp_path / "b.tar", max_bytes=1 << 20).env.key
    assert key_a == key_b

    (repo_a / "uv.lock").write_text("version = 2\n")
    key_changed_lock = build_bundle(str(repo_a), tmp_path / "a2.tar", max_bytes=1 << 20).env.key
    assert key_changed_lock != key_a

    (repo_a / "uv.lock").write_text("version = 1\n")
    (repo_a / "pyproject.toml").write_text('[project]\nname = "y"\nversion = "0"\n')
    key_changed_pyproject = build_bundle(str(repo_a), tmp_path / "a3.tar", max_bytes=1 << 20).env.key
    assert key_changed_pyproject != key_a

    (repo_a / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0"\n')
    (repo_a / ".python-version").write_text("3.12\n")
    key_changed_pyversion = build_bundle(str(repo_a), tmp_path / "a4.tar", max_bytes=1 << 20).env.key
    assert key_changed_pyversion != key_a


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_platform_check_passes_for_this_repo():
    from pasar.cloud.bundle import check_platform
    check_platform(".")            # pasar's own lock resolves for x86_64


def _hide_uv(monkeypatch, tmp_path):
    """Make every way `_find_uv` might locate uv come up empty: no PASAR_UV, nothing on PATH,
    no install under HOME, and no `uv` beside the interpreter."""
    monkeypatch.delenv("PASAR_UV", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "bin" / "python"))


def test_check_platform_without_uv_anywhere_names_path_and_pasar_uv(tmp_path, monkeypatch):
    from pasar.cloud.bundle import check_platform
    _hide_uv(monkeypatch, tmp_path)
    with pytest.raises(BundleError, match="PATH") as exc:
        check_platform(str(tmp_path))
    assert "PASAR_UV" in str(exc.value)


def test_check_platform_honours_pasar_uv_override(tmp_path, monkeypatch):
    from pasar.cloud.bundle import check_platform
    _hide_uv(monkeypatch, tmp_path)
    override = str(tmp_path / "my-uv")
    monkeypatch.setenv("PASAR_UV", override)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")
    monkeypatch.setattr(subprocess, "run", fake_run)
    check_platform(str(tmp_path))
    assert calls and calls[0][0] == override


def test_find_uv_falls_back_to_local_bin(tmp_path, monkeypatch):
    from pasar.cloud.bundle import check_platform
    _hide_uv(monkeypatch, tmp_path)
    local_uv = tmp_path / ".local" / "bin" / "uv"
    local_uv.parent.mkdir(parents=True)
    local_uv.write_text("#!/bin/sh\n")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")
    monkeypatch.setattr(subprocess, "run", fake_run)
    check_platform(str(tmp_path))
    assert calls and calls[0][0] == str(local_uv)


def test_check_platform_reports_a_timeout_as_a_bundle_error(tmp_path, monkeypatch):
    from pasar.cloud.bundle import check_platform
    monkeypatch.setattr("pasar.cloud.bundle._find_uv", lambda: "uv")

    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 600))
    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(BundleError, match="uv"):
        check_platform(str(tmp_path))


def test_env_spec_includes_workspace_members(tmp_path):
    repo = git_repo(tmp_path / "repo")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n\n'
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n')
    member = repo / "packages" / "inner"
    member.mkdir(parents=True)
    (member / "pyproject.toml").write_text('[project]\nname = "inner"\nversion = "0"\n')
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)

    b = build_bundle(str(repo), tmp_path / "b.tar", 1 << 20)

    assert "packages/inner/pyproject.toml" in b.env.files
    assert b.env.files["packages/inner/pyproject.toml"].startswith(b'[project]')


def test_env_spec_includes_the_readme_pyproject_names(tmp_path):
    repo = git_repo(tmp_path / "repo")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\nreadme = "docs/INTRO.md"\n')
    (repo / "docs").mkdir()
    (repo / "docs" / "INTRO.md").write_text("hello")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)

    b = build_bundle(str(repo), tmp_path / "b.tar", 1 << 20)

    assert b.env.files["docs/INTRO.md"] == b"hello"


def test_env_key_ignores_files_uv_never_reads(tmp_path):
    """The image cache key must not move when unrelated code changes, or every edit pays for a
    fresh multi-minute environment build."""
    repo = git_repo(tmp_path / "repo")
    first = build_bundle(str(repo), tmp_path / "a.tar", 1 << 20).env.key
    (repo / "train.py").write_text("print('changed')")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    second = build_bundle(str(repo), tmp_path / "b.tar", 1 << 20).env.key
    assert first == second


def test_missing_workspace_member_fails_at_submit(tmp_path):
    """A lockfile that cannot resolve must fail here, where it costs nothing, not in a paid
    image build."""
    repo = git_repo(tmp_path / "repo")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n\n'
        '[tool.uv.workspace]\nmembers = ["packages/gone"]\n')
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    with pytest.raises(BundleError, match="packages/gone"):
        build_bundle(str(repo), tmp_path / "b.tar", 1 << 20)


def test_workspace_member_without_pyproject_fails_at_submit(tmp_path):
    repo = git_repo(tmp_path / "repo")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n\n'
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n')
    empty_member = repo / "packages" / "empty"
    empty_member.mkdir(parents=True)
    (empty_member / "keep.txt").write_text("nothing to see\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    with pytest.raises(BundleError, match="packages/empty"):
        build_bundle(str(repo), tmp_path / "b.tar", 1 << 20)


def test_absolute_readme_path_is_rejected(tmp_path):
    """EnvSpec.files is uploaded to a third-party cloud; an absolute path in the readme field
    must not be able to smuggle an arbitrary local file into it."""
    repo = git_repo(tmp_path / "repo")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\nreadme = "/etc/hostname"\n')
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    with pytest.raises(BundleError, match="/etc/hostname"):
        build_bundle(str(repo), tmp_path / "b.tar", 1 << 20)


def test_symlinked_readme_escaping_the_repo_is_rejected(tmp_path):
    repo = git_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside-secret\n")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\nreadme = "docs/INTRO.md"\n')
    (repo / "docs").mkdir()
    (repo / "docs" / "INTRO.md").symlink_to(outside / "secret.txt")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    with pytest.raises(BundleError, match="docs/INTRO.md"):
        build_bundle(str(repo), tmp_path / "b.tar", 1 << 20)


def test_symlinked_workspace_member_escaping_the_repo_is_rejected(tmp_path):
    repo = git_repo(tmp_path / "repo")
    outside = tmp_path / "outside_member"
    outside.mkdir()
    (outside / "pyproject.toml").write_text('[project]\nname = "evil"\nversion = "0"\n')
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n\n'
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n')
    (repo / "packages").mkdir()
    (repo / "packages" / "evil").symlink_to(outside)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    with pytest.raises(BundleError, match="packages/evil"):
        build_bundle(str(repo), tmp_path / "b.tar", 1 << 20)


def test_absolute_workspace_glob_is_rejected(tmp_path):
    repo = git_repo(tmp_path / "repo")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n\n'
        '[tool.uv.workspace]\nmembers = ["/etc/*"]\n')
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    with pytest.raises(BundleError, match="/etc/"):
        build_bundle(str(repo), tmp_path / "b.tar", 1 << 20)


def test_unreadable_referenced_file_fails_at_submit(tmp_path):
    repo = git_repo(tmp_path / "repo")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\nreadme = "README.md"\n')
    readme = repo / "README.md"
    readme.write_text("hi\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    os.chmod(readme, 0o000)
    try:
        with pytest.raises(BundleError, match="README.md"):
            build_bundle(str(repo), tmp_path / "b.tar", 1 << 20)
    finally:
        os.chmod(readme, 0o644)


def test_env_key_uses_an_unambiguous_separator(tmp_path):
    """Two distinct file sets that would collide under plain name+content concatenation (once
    keys can contain "/") must still produce different keys."""
    from pasar.cloud.bundle import _env_key
    key_a = _env_key({"ab": b"c"})
    key_b = _env_key({"a": b"bc"})
    assert key_a != key_b
