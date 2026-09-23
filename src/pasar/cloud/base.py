"""The interface a cloud provider implements, and the types it exchanges with pasar."""


def parse_gpu(spec: str) -> tuple[str, int]:
    """'H100' or 'A100-80GB:4' -> (type, count)."""
    kind, _, count = spec.partition(":")
    if not kind or (count and not count.isdigit()):
        raise ValueError(f"bad --gpu {spec!r}: expected TYPE or TYPE:COUNT, e.g. H100 or H100:4")
    n = int(count) if count else 1
    if n < 1:
        raise ValueError(f"bad --gpu {spec!r}: count must be at least 1")
    return kind, n
