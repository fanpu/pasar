"""Mascot images: which file to show for each machine state, custom images first."""

import re
from pathlib import Path

STATES = ("idle", "happy", "start", "busy", "waiting", "sweat", "hot", "oom",
          "failed", "preempted", "done", "thinking", "hmm", "confused")
BUILTIN_DIR = Path(__file__).parent / "mascot"
_NAME = re.compile(rf"^({'|'.join(STATES)})(?:-([0-9]{{1,3}}))?\.(png|svg|webp|gif)$")


def _files(d: Path) -> list[str]:
    try:
        return [p.name for p in d.iterdir() if p.is_file()]
    except OSError:
        return []


def _scan(d: Path) -> dict[str, list[str]]:
    """Image names in `d` per state, variants in order (`done.png`, `done-2.png`, …)."""
    found: dict[str, list[tuple[int, str]]] = {}
    for name in _files(d):
        m = _NAME.match(name)
        if m:
            found.setdefault(m.group(1), []).append((int(m.group(2) or 1), name))
    return {state: [n for _, n in sorted(names)] for state, names in found.items()}


def manifest(custom_dir: Path) -> dict[str, list[str]]:
    custom = _scan(custom_dir)
    builtin = _scan(BUILTIN_DIR)
    out = {}
    for state in STATES:
        if state in custom:
            out[state] = [f"/mascot/{n}" for n in custom[state]]
        else:
            out[state] = [f"/mascot/builtin/{n}" for n in builtin.get(state, [])]
    return out


def _resolve(d: Path, filename: str) -> Path | None:
    if not _NAME.match(filename):
        return None
    path = d / filename
    return path if path.is_file() else None


def resolve(custom_dir: Path, filename: str) -> Path | None:
    return _resolve(custom_dir, filename)


def resolve_builtin(filename: str) -> Path | None:
    return _resolve(BUILTIN_DIR, filename)
