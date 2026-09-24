"""Snapshot a job's code and environment at submit time, so a cloud attempt is reproducible."""

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

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


def _contained(root: Path, path: Path) -> Path | None:
    """`path` with symlinks resolved, if that lands on root or inside it; otherwise None. Mirrors
    the check build_bundle's own code-snapshot loop already applies to a tracked symlink — a
    normalized path can climb out of the repository via "..", and a path can also lead outside
    through a symlink even when it never spells "..", so both are checked the same way: resolve,
    then compare."""
    real = Path(os.path.realpath(path))
    if real == root or real.is_relative_to(root):
        return real
    return None


def _glob(root: Path, pattern: str) -> list[Path]:
    """root.glob(pattern), turned into a BundleError for a pattern uv's own resolver would also
    reject: an absolute pattern makes pathlib raise NotImplementedError."""
    try:
        return list(root.glob(pattern))
    except NotImplementedError:
        raise BundleError(f"this project's uv workspace uses {pattern!r}, an absolute glob "
                          "pattern, which can't be resolved for a cloud job") from None


def _read(root: Path, rel: str) -> bytes:
    """Read a file this module has already decided belongs in the environment spec, turning a
    permissions problem into a submit-time BundleError instead of an unhandled exception."""
    try:
        return (root / rel).read_bytes()
    except OSError as e:
        raise BundleError(f"can't read {rel}, needed to build the cloud environment: {e}") from None


def _referenced(root: Path, rel: str, data: bytes) -> list[str]:
    """Paths a pyproject names that uv reads while it resolves: the readme (uv builds the root
    project's metadata even under --no-install-project) and a license file. Anything unparsable
    contributes nothing — a broken pyproject is uv's error to report, with uv's message. A named
    path that escapes the repository — an absolute path, or a relative one that resolves outside
    it, symlink or not — is a BundleError: EnvSpec.files is uploaded to a third-party cloud, so a
    file smuggled out this way would be exfiltrated to it, not just misfiled."""
    try:
        table = tomllib.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return []
    project = table.get("project") or {}
    named = []
    for key in ("readme", "license-files", "license"):
        value = project.get(key)
        if isinstance(value, str):
            named.append(value)
        elif isinstance(value, dict) and isinstance(value.get("file"), str):
            named.append(value["file"])
        elif isinstance(value, list):
            named.extend(v for v in value if isinstance(v, str))
    base = PurePosixPath(rel).parent
    out = []
    for name in named:
        # A license can be an SPDX expression ("MIT") rather than a path; those simply aren't
        # files, so is_file() below drops them without anything having to tell the two apart.
        candidate = PurePosixPath(name)
        if candidate.is_absolute():
            raise BundleError(f"{rel} names {name!r}, an absolute path, but a cloud job can only "
                              "reference files inside the repository")
        path = (base / candidate) if str(base) != "." else candidate
        real = _contained(root, root / path)
        if real is None:
            raise BundleError(f"{rel} names {name!r}, which resolves outside the repository, so "
                              "it can't be sent to the cloud")
        if real.is_file():
            out.append(str(path))
    return out


def _members(root: Path, data: bytes) -> list[str]:
    """Every workspace member's pyproject, resolved from the globs uv itself resolves. A member
    glob that matches nothing, a member directory with no pyproject.toml, or a member that
    resolves outside the repository (nominally via "..", or through a symlink) is uv's own error
    case — or worse, in the last case, a way to smuggle an outside file into the cloud image — so
    it is raised here rather than left for a paid image build to discover."""
    try:
        table = tomllib.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return []
    workspace = (table.get("tool") or {}).get("uv", {}).get("workspace") or {}
    excluded = set()
    for pattern in workspace.get("exclude") or []:
        excluded.update(str(p.relative_to(root)) for p in _glob(root, pattern) if p.is_relative_to(root))
    found = []
    for pattern in workspace.get("members") or []:
        matches = sorted(p for p in _glob(root, pattern) if p.is_dir())
        if not matches:
            raise BundleError(f"this project's uv workspace lists {pattern!r}, which matches "
                              "nothing, so its lockfile cannot be used in the cloud")
        for path in matches:
            if not path.is_relative_to(root):
                raise BundleError(f"this project's uv workspace lists {pattern!r}, which matches "
                                  f"{path}, outside the repository, so it can't be sent to the "
                                  "cloud")
            rel = str(path.relative_to(root))
            if rel in excluded:
                continue
            real = _contained(root, path)
            if real is None:
                raise BundleError(f"uv workspace member {rel} resolves outside the repository, "
                                  "so it can't be sent to the cloud")
            if not (real / "pyproject.toml").is_file():
                raise BundleError(f"uv workspace member {rel} has no pyproject.toml, so this "
                                  "project's lockfile cannot be used in the cloud")
            found.append(f"{rel}/pyproject.toml")
    return found


def _env_files(root: Path) -> dict[str, bytes]:
    """Everything a container-side `uv sync --frozen --no-install-project` reads, keyed by its
    path relative to the repository root. This set — and only this set — decides the image cache
    key, so a code change never invalidates a multi-minute environment build, and a dependency
    change always does."""
    files: dict[str, bytes] = {}
    for name in LOCK_FILES:
        path = root / name
        if path.is_file():
            files[name] = _read(root, name)
    if "uv.lock" not in files:
        raise BundleError(f"cloud jobs need a uv.lock next to pyproject.toml in {root}")
    pending = [name for name in files if name.endswith("pyproject.toml")]
    for rel in _members(root, files["pyproject.toml"]) if "pyproject.toml" in files else []:
        files[rel] = _read(root, rel)
        pending.append(rel)
    for rel in pending:
        for extra in _referenced(root, rel, files[rel]):
            if extra not in files:
                files[extra] = _read(root, extra)
    return files


def _env_key(files: dict[str, bytes]) -> str:
    """The image cache key: a digest of every environment file's name and content, in a form
    that can't collide across a name/content boundary. Each field is length-prefixed, so
    ("ab", b"c") and ("a", b"bc") — indistinguishable under plain concatenation once names can
    contain "/" — hash differently."""
    digest = hashlib.sha256()
    for name in sorted(files):
        encoded = name.encode()
        content = files[name]
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()[:16]


def build_bundle(cwd: str, dest: Path, max_bytes: int) -> Bundle:
    try:
        root = _git(cwd, "rev-parse", "--show-toplevel").strip()
    except BundleError as e:
        raise BundleError(f"cloud jobs must run inside a git repository: {e}") from None
    root_path = Path(root).resolve()
    rel_cwd = str(Path(cwd).resolve().relative_to(root_path))
    listed = _list_files(cwd)
    env_files = _env_files(root_path)
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
    return Bundle(dest, root, rel_cwd, EnvSpec(env_files, _env_key(env_files)), total)


def _find_uv() -> str | None:
    """Locate the uv executable, without trusting PATH: pasard runs as a systemd user service,
    and a service's PATH commonly lacks ~/.local/bin, where uv is usually installed.

    Tried in order: the PASAR_UV environment variable (an explicit override), PATH
    (`shutil.which`), uv's usual install locations (`~/.local/bin/uv`, `~/.cargo/bin/uv`), and
    the directory holding the running Python (a uv-managed tool environment keeps its `uv`
    there too). Returns None if none of these pan out."""
    override = os.environ.get("PASAR_UV")
    if override:
        return override
    found = shutil.which("uv")
    if found:
        return found
    home = Path.home()
    for candidate in (home / ".local" / "bin" / "uv", home / ".cargo" / "bin" / "uv"):
        if candidate.is_file():
            return str(candidate)
    sibling = Path(sys.executable).parent / "uv"
    if sibling.is_file():
        return str(sibling)
    return None


def check_platform(project_dir: str, platform: str = "x86_64-manylinux_2_28") -> None:
    """Fail at submit if the lockfile can't resolve for the cloud's architecture.

    Run inside the project so uv honours its own package indexes; a `uv export` to
    requirements.txt drops them and makes a custom torch index look unsatisfiable.

    Locates uv with `_find_uv` rather than a bare "uv", since pasard's PATH may not reach it;
    set PASAR_UV to uv's full path to override that search.
    """
    uv = _find_uv()
    if uv is None:
        raise BundleError(
            "pasard could not find uv, which it needs to check this job's lockfile before a "
            "cloud submit. Put uv on pasard's PATH (e.g. a systemd drop-in with "
            "`Environment=PATH=...`) or set PASAR_UV to uv's full path.")
    try:
        p = subprocess.run(
            [uv, "sync", "--frozen", "--dry-run", "--no-install-project",
             "--python-platform", platform],
            cwd=project_dir, capture_output=True, timeout=600, check=False)
    except subprocess.TimeoutExpired:
        raise BundleError(
            f"uv ({uv}) did not finish checking this project's lockfile for {platform} within "
            "600s; try again, or check whether the project's package indexes are reachable"
        ) from None
    except OSError as e:
        raise BundleError(f"could not run uv ({uv}) to check this job's lockfile: {e}") from None
    if p.returncode != 0:
        raise BundleError(
            f"this project's uv.lock does not resolve for {platform}, so it can't run in the "
            f"cloud:\n{p.stderr.decode(errors='replace')[-800:]}")
