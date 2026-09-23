"""The control-line format shared with pasar_job.run (kept in sync by a test).

stdout is the only channel every cloud provider offers, so the wrapper multiplexes job
output with events, GPU samples and the exit status by prefixing control lines with a
byte (0x1e, ASCII record separator) that ordinary program output is vanishingly
unlikely to start a line with.
"""

import json

PREFIX = "\x1epasar:"


def encode(token: str, obj: dict) -> bytes:
    return (PREFIX + token + " " + json.dumps(obj, separators=(",", ":")) + "\n").encode()


def split(line: str, token: str) -> dict | None:
    """The control object in this line, or None if it is ordinary job output."""
    head = PREFIX + token + " "
    if not line.startswith(head):
        return None
    try:
        obj = json.loads(line[len(head):])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None
