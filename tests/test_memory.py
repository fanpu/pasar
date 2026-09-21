from pasar.config import Config
from pasar.memory import pool_size, reservation
from pasar.units import GiB

cfg = Config()


def test_pool_subtracts_system_reserve():
    assert pool_size(121 * GiB, cfg) == 105 * GiB
    assert pool_size(8 * GiB, cfg) == 0


def test_reservation_margin():
    assert reservation(10 * GiB, 105 * GiB, cfg) == 12 * GiB  # 2 GiB floor
    assert reservation(24 * GiB, 105 * GiB, cfg) == 24 * GiB + int(24 * GiB * 0.10)
    assert reservation(None, 105 * GiB, cfg) == 105 * GiB  # whole GPU
