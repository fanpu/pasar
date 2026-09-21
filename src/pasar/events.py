"""The job -> pasar event protocol (JSON lines in $PASAR_EVENTS) and lost-time math."""

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

KINDS = {"checkpoint", "resumed", "progress", "note"}
_READ_LIMIT = 1 << 20
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1

log = logging.getLogger(__name__)


@dataclass
class ParsedEvent:
    kind: str
    step: int | None
    payload: dict


def _finite_float(text: str) -> float:
    """A json.loads `parse_float` hook that rejects non-finite results (e.g. from `1e999`)."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"non-finite number: {text}")
    return value


def _reject_constant(name: str) -> float:
    """A json.loads `parse_constant` hook: reject the literals NaN/Infinity/-Infinity."""
    raise ValueError(f"non-finite constant: {name}")


def parse_line(line: str) -> ParsedEvent | None:
    try:
        obj = json.loads(line, parse_float=_finite_float, parse_constant=_reject_constant)
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("event") not in KINDS:
        return None
    step = obj.get("step")
    if not isinstance(step, int) or isinstance(step, bool) or not (_INT64_MIN <= step <= _INT64_MAX):
        step = None
    payload = {k: v for k, v in obj.items() if k != "event"}
    return ParsedEvent(obj["event"], step, payload)


def read_new(path: Path, offset: int, job_id: int | None = None) -> tuple[list[ParsedEvent], int]:
    """Parse complete lines after `offset`. A trailing partial line waits for the next read.
    Malformed lines are dropped and logged as warnings (each line is only ever read once, since
    `offset` advances past it whether or not it parsed)."""
    if not path.exists():
        return [], offset
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read(_READ_LIMIT)
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    lines = data[: end + 1].decode("utf-8", errors="replace").splitlines()
    events = []
    for line in lines:
        e = parse_line(line)
        if e is not None:
            events.append(e)
        elif line.strip():
            log.warning("job %s: malformed event line: %r", job_id, line[:200])
    return events, offset + end + 1


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
