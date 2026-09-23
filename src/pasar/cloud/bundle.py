"""Snapshot a job's code and environment at submit time, so a cloud attempt is reproducible."""

import hashlib
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

LOCK_FILES = ("pyproject.toml", "uv.lock", ".python-version")


class BundleError(Exception):
    """The job can't be packaged; raised at submit, before anything is paid for."""


@dataclass
class EnvSpec:
    files: dict[str, bytes]      # name -> contents, for the image build
    key: str                     # hash of the above: the image cache key


@dataclass
class Bundle:
    path: Path
    root: str                    # repository root on the local machine
    rel_cwd: str                 # working directory relative to the root
    env: EnvSpec
    size: int


def _git(cwd: str, *args: str) -> str:
    p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, timeout=30, check=False)
    if p.returncode != 0:
        raise BundleError(f"git {' '.join(args)} failed: {p.stderr.decode(errors='replace')[:200]}")
    return p.stdout.decode("utf-8", errors="replace")


def build_bundle(cwd: str, dest: Path, max_bytes: int) -> Bundle:
    try:
        root = _git(cwd, "rev-parse", "--show-toplevel").strip()
    except BundleError as e:
        raise BundleError(f"cloud jobs must run inside a git repository: {e}") from None
    rel_cwd = str(Path(cwd).resolve().relative_to(Path(root).resolve()))
    listed = [f for f in _git(cwd, "ls-files", "--cached", "--others", "--exclude-standard",
                              "--full-name", ":/").splitlines() if f]
    env_files = {}
    for name in LOCK_FILES:
        f = Path(root) / name
        if f.exists():
            env_files[name] = f.read_bytes()
    if "uv.lock" not in env_files:
        raise BundleError(f"cloud jobs need a uv.lock next to pyproject.toml in {root}")
    sizes = {f: (Path(root) / f).stat().st_size for f in listed if (Path(root) / f).is_file()}
    total = sum(sizes.values())
    if total > max_bytes:
        biggest = sorted(sizes.items(), key=lambda kv: -kv[1])[:5]
        listing = ", ".join(f"{n} ({s // 1024} KiB)" for n, s in biggest)
        raise BundleError(f"code snapshot is {total // (1 << 20)} MiB, over the "
                          f"{max_bytes // (1 << 20)} MiB limit. Biggest files: {listing}. "
                          "Move data out of the repository or gitignore it.")
    with tarfile.open(dest, "w") as tar:
        for name in sizes:
            tar.add(Path(root) / name, arcname=name)
    digest = hashlib.sha256()
    for name in sorted(env_files):
        digest.update(name.encode())
        digest.update(env_files[name])
    return Bundle(dest, root, rel_cwd, EnvSpec(env_files, digest.hexdigest()[:16]), total)


def check_platform(project_dir: str, platform: str = "x86_64-manylinux_2_28") -> None:
    """Fail at submit if the lockfile can't resolve for the cloud's architecture.

    Run inside the project so uv honours its own package indexes; a `uv export` to
    requirements.txt drops them and makes a custom torch index look unsatisfiable.
    """
    p = subprocess.run(
        ["uv", "sync", "--frozen", "--dry-run", "--no-install-project",
         "--python-platform", platform],
        cwd=project_dir, capture_output=True, timeout=600, check=False)
    if p.returncode != 0:
        raise BundleError(
            f"this project's uv.lock does not resolve for {platform}, so it can't run in the "
            f"cloud:\n{p.stderr.decode(errors='replace')[-800:]}")
