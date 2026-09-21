"""Record the git state of a job's working directory at submit time."""

import subprocess

_MAX_DIFF = 1 << 20  # 1 MiB


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    # Bytes, not text=True: a working directory can contain non-UTF-8 file contents (e.g.
    # binary or otherwise-encoded files touched in the diff), and text mode decodes strictly.
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, timeout=10, check=False)


def capture(cwd: str) -> tuple[str | None, str]:
    """(HEAD commit, uncommitted diff), or (None, "") outside a git repository."""
    try:
        head = _git(cwd, "rev-parse", "HEAD")
        if head.returncode != 0:
            return None, ""
        diff = _git(cwd, "diff", "HEAD")
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    commit = head.stdout.decode("utf-8", errors="replace").strip()
    text = diff.stdout.decode("utf-8", errors="replace")
    if len(text) > _MAX_DIFF:
        text = text[:_MAX_DIFF] + "\n… (diff truncated)\n"
    return commit, text
