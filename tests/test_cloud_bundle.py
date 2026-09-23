import shutil
import subprocess
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


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_platform_check_passes_for_this_repo():
    from pasar.cloud.bundle import check_platform
    check_platform(".")            # pasar's own lock resolves for x86_64
