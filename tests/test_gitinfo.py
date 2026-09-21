import subprocess

from pasar.gitinfo import capture


def test_capture_in_repo_with_dirty_file(tmp_path):
    def run(*a):
        return subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    run("init", "-q")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
    (tmp_path / "f.txt").write_text("hi\n")
    run("add", "f.txt")
    commit, diff = capture(str(tmp_path))
    assert len(commit) == 40 and "f.txt" in diff


def test_capture_outside_repo(tmp_path):
    assert capture(str(tmp_path)) == (None, "")
