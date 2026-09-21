"""Parsing and formatting of memory sizes and durations."""

import re

GiB = 1024**3

_SIZE_UNITS = {
    "": 1, "b": 1,
    "k": 1024, "kb": 1024, "kib": 1024,
    "m": 1024**2, "mb": 1024**2, "mib": 1024**2,
    "g": GiB, "gb": GiB, "gib": GiB,
    "t": 1024**4, "tb": 1024**4, "tib": 1024**4,
}
_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)\s*$")
_DUR_RE = re.compile(r"([0-9]*\.?[0-9]+)\s*([dhms])")
_DUR_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_size(text: str | int) -> int:
    """'24G', '24GiB', '512M', '1.5g' -> bytes. Units are binary; a bare number is bytes."""
    if isinstance(text, int):
        return text
    m = _SIZE_RE.match(text)
    if not m or m.group(2).lower() not in _SIZE_UNITS:
        raise ValueError(f"invalid size: {text!r}")
    return int(float(m.group(1)) * _SIZE_UNITS[m.group(2).lower()])


def parse_duration(text: str | float) -> int:
    """'2h30m', '90m', '45s', '1.5h', '3600' -> seconds."""
    if isinstance(text, (int, float)):
        return int(text)
    s = text.strip().lower()
    if s.isdigit():
        return int(s)
    parts = _DUR_RE.findall(s)
    if not parts or _DUR_RE.sub("", s).strip():
        raise ValueError(f"invalid duration: {text!r}")
    return int(sum(float(n) * _DUR_UNITS[u] for n, u in parts))


def fmt_duration(seconds: float) -> str:
    s = round(seconds)
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def fmt_gib(n: int) -> str:
    return f"{n / GiB:.1f} GiB"
