"""The job -> pasar event protocol (JSON lines in $PASAR_EVENTS) and lost-time math."""

import json
from dataclasses import dataclass
from pathlib import Path

KINDS = {"checkpoint", "resumed", "progress", "note"}
_READ_LIMIT = 1 << 20


@dataclass
class ParsedEvent:
    kind: str
    step: int | None
    payload: dict


def parse_line(line: str) -> ParsedEvent | None:
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("event") not in KINDS:
        return None
    step = obj.get("step")
    if not isinstance(step, int) or isinstance(step, bool):
        step = None
    payload = {k: v for k, v in obj.items() if k != "event"}
    return ParsedEvent(obj["event"], step, payload)


def read_new(path: Path, offset: int) -> tuple[list[ParsedEvent], int]:
    """Parse complete lines after `offset`. A trailing partial line waits for the next read."""
    if not path.exists():
        return [], offset
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read(_READ_LIMIT)
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    lines = data[: end + 1].decode("utf-8", errors="replace").splitlines()
    return [e for e in map(parse_line, lines) if e], offset + end + 1


def wasted_work(end: float, start: float, last_checkpoint: float | None,
                reports_events: bool) -> float | None:
    """Work lost when an attempt is interrupted: time since its last checkpoint."""
    if not reports_events:
        return None
    since = last_checkpoint if last_checkpoint is not None and last_checkpoint >= start else start
    return max(0.0, end - since)


def restart_cost(start: float, resumed: float | None) -> float | None:
    """Time from relaunch until the job reports it resumed from its checkpoint."""
    return None if resumed is None else max(0.0, resumed - start)
