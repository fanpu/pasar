import pytest

from pasar.config import Config, load_config
from pasar.units import GiB


def test_defaults_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg = load_config()
    assert cfg.bind == []
    assert cfg.addresses() == ["127.0.0.1:8750"]
    assert cfg.system_reserve == 16 * GiB
    assert cfg.default_bid == 1000 and cfg.default_grace == 120
    assert cfg.mascot_dir == str(tmp_path / "cfg" / "pasar" / "mascot")
    assert cfg.data_dir == str(tmp_path / "data" / "pasar")


def test_parses_sizes_and_durations(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        'bind = ["127.0.0.1:9000", "100.1.2.3:9000"]\n'
        'system_reserve = "8G"\n'
        'default_grace = "5m"\n'
        'pressure_sustain = "45s"\n'
        'tick = "1s"\n'
        'prometheus_url = "http://127.0.0.1:9090"\n'
    )
    cfg = load_config(p)
    assert cfg.bind == ["127.0.0.1:9000", "100.1.2.3:9000"]
    assert cfg.addresses() == ["127.0.0.1:8750", "127.0.0.1:9000", "100.1.2.3:9000"]
    assert cfg.system_reserve == 8 * GiB
    assert cfg.default_grace == 300
    assert cfg.pressure_sustain == 45
    assert cfg.tick == 1.0
    assert cfg.prometheus_url == "http://127.0.0.1:9090"


def test_addresses_dedupes_and_preserves_order(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        'bind = ["127.0.0.1:8750", "127.0.0.1:9000", "127.0.0.1:8750"]\n'
    )
    cfg = load_config(p)
    assert cfg.bind == ["127.0.0.1:8750", "127.0.0.1:9000", "127.0.0.1:8750"]
    assert cfg.addresses() == ["127.0.0.1:8750", "127.0.0.1:9000"]


def test_unknown_key_is_an_error(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("bogus = 1\n")
    with pytest.raises(ValueError, match="bogus"):
        load_config(p)


def test_config_is_constructible_with_defaults():
    assert Config().mem_margin_frac == 0.10


def test_allowed_hosts_defaults_empty_and_parses_from_toml(tmp_path):
    assert Config().allowed_hosts == []
    p = tmp_path / "config.toml"
    p.write_text('allowed_hosts = ["mybox.example.ts.net"]\n')
    cfg = load_config(p)
    assert cfg.allowed_hosts == ["mybox.example.ts.net"]


def test_pasar_address_env_replaces_default(monkeypatch):
    monkeypatch.setenv("PASAR_ADDRESS", "127.0.0.1:18750")
    cfg = Config(bind=["100.64.0.1:8750"])
    assert cfg.addresses() == ["127.0.0.1:18750", "100.64.0.1:8750"]


def test_cloud_target_parsed(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("""
[clouds.modal]
provider = "modal"
budget = { daily = 50.0, monthly = 300.0 }
max_running = 4
max_runtime = "24h"
approval_ttl = "12h"
env_passthrough = ["WANDB_API_KEY"]
bundle_max = "128MiB"
""")
    cfg = load_config(p)
    t = cfg.clouds["modal"]
    assert (t.provider, t.daily_budget, t.monthly_budget) == ("modal", 50.0, 300.0)
    assert (t.max_running, t.max_runtime, t.approval_ttl) == (4, 86400, 43200)
    assert t.bundle_max == 128 * 1024 * 1024
    assert t.timeout_factor == 1.5           # default


def test_cloud_target_requires_budget(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[clouds.modal]\nprovider = "modal"\n')
    with pytest.raises(ValueError, match="budget"):
        load_config(p)


def test_no_clouds_by_default(tmp_path):
    assert load_config(tmp_path / "missing.toml").clouds == {}


def test_cloud_target_rejects_unknown_keys(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[clouds.modal]\nprovider = "modal"\nbudget = { daily = 50.0 }\nmax_runing = 4\n')
    with pytest.raises(ValueError, match="unknown config keys"):
        load_config(p)


def test_cloud_target_rejects_nonpositive_daily_budget(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[clouds.modal]\nprovider = "modal"\nbudget = { daily = 0 }\n')
    with pytest.raises(ValueError, match="budget.daily must be positive"):
        load_config(p)


def test_cloud_target_rejects_nonpositive_monthly_budget(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[clouds.modal]\nprovider = "modal"\nbudget = { daily = 50.0, monthly = -10.0 }\n')
    with pytest.raises(ValueError, match="budget.monthly must be positive"):
        load_config(p)
