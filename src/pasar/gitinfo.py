"""Record the git state of a job's working directory at submit time."""

import subprocess


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=10, check=False)


def capture(cwd: str) -> tuple[str | None, str]:
    """(HEAD commit, uncommitted diff), or (None, "") outside a git repository."""
    try:
        head = _git(cwd, "rev-parse", "HEAD")
        if head.returncode != 0:
            return None, ""
        diff = _git(cwd, "diff", "HEAD")
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    return head.stdout.strip(), diff.stdout
