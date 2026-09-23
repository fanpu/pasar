"""Snapshot a job's code and environment at submit time, so a cloud attempt is reproducible."""

import hashlib
import os
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


def _run_git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, timeout=30, check=False)
    if p.returncode != 0:
        raise BundleError(f"git {' '.join(args)} failed: {p.stderr.decode(errors='replace')[:200]}")
    return p


def _git(cwd: str, *args: str) -> str:
    return _run_git(cwd, *args).stdout.decode("utf-8", errors="replace")


def _list_files(cwd: str) -> list[str]:
    # -z: NUL-delimited, unquoted paths. Without it, git quotes any path with non-ASCII or
    # non-UTF-8 bytes (e.g. "caf\303\251.txt"), which then doesn't exist on disk under that
    # literal name and silently drops out of the bundle.
    raw = _run_git(cwd, "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                    "--full-name", ":/").stdout
    names = []
    for chunk in raw.split(b"\0"):
        if not chunk:
            continue
        try:
            names.append(os.fsdecode(chunk))
        except UnicodeDecodeError:
            raise BundleError(f"file name isn't valid for this filesystem's encoding: {chunk!r}") from None
    return names


def build_bundle(cwd: str, dest: Path, max_bytes: int) -> Bundle:
    try:
        root = _git(cwd, "rev-parse", "--show-toplevel").strip()
    except BundleError as e:
        raise BundleError(f"cloud jobs must run inside a git repository: {e}") from None
    root_path = Path(root).resolve()
    rel_cwd = str(Path(cwd).resolve().relative_to(root_path))
    listed = _list_files(cwd)
    env_files = {}
    for name in LOCK_FILES:
        f = root_path / name
        if f.exists():
            env_files[name] = f.read_bytes()
    if "uv.lock" not in env_files:
        raise BundleError(f"cloud jobs need a uv.lock next to pyproject.toml in {root}")
    sizes = {}
    for name in listed:
        path = root_path / name
        if path.is_symlink():
            target = Path(os.path.realpath(path))
            if not (target == root_path or target.is_relative_to(root_path)):
                raise BundleError(f"symlink {name} points outside the repository, to {target}")
            sizes[name] = path.lstat().st_size
        elif path.is_file():
            sizes[name] = path.stat().st_size
    total = sum(sizes.values())
    if total > max_bytes:
        biggest = sorted(sizes.items(), key=lambda kv: -kv[1])[:5]
        listing = ", ".join(f"{n} ({s // 1024} KiB)" for n, s in biggest)
        raise BundleError(f"code snapshot is {total // (1 << 20)} MiB, over the "
                          f"{max_bytes // (1 << 20)} MiB limit. Biggest files: {listing}. "
                          "Move data out of the repository or gitignore it.")
    with tarfile.open(dest, "w") as tar:
        for name in sizes:
            try:
                tar.add(root_path / name, arcname=name)
            except FileNotFoundError:
                raise BundleError(f"{name} disappeared while the bundle was being built") from None
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
