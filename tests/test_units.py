import pytest

from pasar.units import GiB, fmt_duration, fmt_gib, parse_duration, parse_size


@pytest.mark.parametrize(
    "text,expected",
    [
        ("24G", 24 * GiB),
        ("24GiB", 24 * GiB),
        ("24gb", 24 * GiB),
        ("1.5g", int(1.5 * GiB)),
        ("512M", 512 * 1024**2),
        ("100", 100),
        (4096, 4096),
    ],
)
def test_parse_size(text, expected):
    assert parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "12X", "G"])
def test_parse_size_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_size(text)


@pytest.mark.parametrize(
    "text,expected",
    [("2h30m", 9000), ("90m", 5400), ("45s", 45), ("1.5h", 5400), ("3600", 3600), ("1h 30m", 5400),
     ("1d", 86400), (120, 120)],
)
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "soon", "5x", "h"])
def test_parse_duration_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_duration(text)


def test_fmt_duration():
    assert fmt_duration(45) == "45s"
    assert fmt_duration(540) == "9m"
    assert fmt_duration(8040) == "2h14m"


def test_fmt_gib():
    assert fmt_gib(int(31.2 * GiB)) == "31.2 GiB"
