"""Mascot images: which file to show for each machine state, custom images first."""

import re
from pathlib import Path

STATES = ("idle", "happy", "start", "busy", "waiting", "sweat", "hot", "oom",
          "failed", "preempted", "done", "thinking", "hmm", "confused")
BUILTIN_DIR = Path(__file__).parent / "mascot"
_NAME = re.compile(rf"^({'|'.join(STATES)})(?:-([0-9]{{1,3}}))?\.(png|svg|webp|gif)$")
_BUILTIN_NAME = re.compile(rf"^({'|'.join(STATES)})\.svg$")


def _files(d: Path) -> list[str]:
    try:
        return [p.name for p in d.iterdir() if p.is_file()]
    except OSError:
        return []


def manifest(custom_dir: Path) -> dict[str, list[str]]:
    custom: dict[str, list[tuple[int, str]]] = {}
    for name in _files(custom_dir):
        m = _NAME.match(name)
        if m:
            custom.setdefault(m.group(1), []).append((int(m.group(2) or 1), name))
    builtin = set(_files(BUILTIN_DIR))
    out = {}
    for state in STATES:
        if state in custom:
            out[state] = [f"/mascot/{n}" for _, n in sorted(custom[state])]
        elif f"{state}.svg" in builtin:
            out[state] = [f"/mascot/builtin/{state}.svg"]
        else:
            out[state] = []
    return out


def _resolve(d: Path, filename: str, pattern: re.Pattern) -> Path | None:
    if not pattern.match(filename):
        return None
    path = d / filename
    return path if path.is_file() else None


def resolve(custom_dir: Path, filename: str) -> Path | None:
    return _resolve(custom_dir, filename, _NAME)


def resolve_builtin(filename: str) -> Path | None:
    return _resolve(BUILTIN_DIR, filename, _BUILTIN_NAME)
