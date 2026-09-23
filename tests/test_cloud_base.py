import pytest

from pasar.cloud.base import parse_gpu


def test_parse_gpu_defaults_to_one():
    assert parse_gpu("H100") == ("H100", 1)


def test_parse_gpu_with_count():
    assert parse_gpu("A100-80GB:4") == ("A100-80GB", 4)


@pytest.mark.parametrize("bad", ["", "H100:0", "H100:x", ":2", "H100:2:3", "H100:-1"])
def test_parse_gpu_rejects_junk(bad):
    with pytest.raises(ValueError):
        parse_gpu(bad)
