import pytest

from pasar.config import Config, load_config
from pasar.units import GiB


def _write(tmp_path, content):
    p = tmp_path / "config.toml"
    p.write_text(content)
    return load_config(p)


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
    assert cfg.pull_dir == str(tmp_path / "data" / "pasar" / "pulls")


def test_pull_dir_defaults_under_data_dir_but_can_be_overridden(tmp_path):
    cfg = _write(tmp_path, f'data_dir = "{tmp_path / "somewhere"}"\n')
    assert cfg.pull_dir == str(tmp_path / "somewhere" / "pulls")

    cfg = _write(tmp_path, f'data_dir = "{tmp_path / "somewhere"}"\n'
                           f'pull_dir = "{tmp_path / "elsewhere"}"\n')
    assert cfg.pull_dir == str(tmp_path / "elsewhere")


def test_pull_guards_default_to_a_20gib_margin_and_no_cap_and_parse_as_sizes(tmp_path):
    cfg = _write(tmp_path, "")
    assert cfg.pull_min_free == 20 * GiB
    assert cfg.pull_max == 0  # no limit

    cfg = _write(tmp_path, 'pull_min_free = "5G"\npull_max = "100G"\n')
    assert cfg.pull_min_free == 5 * GiB
    assert cfg.pull_max == 100 * GiB


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


def test_cloud_target_parses_profile_owner_and_group(tmp_path):
    cfg = _write(tmp_path, """
        [clouds.modal-a]
        provider = "modal"
        profile = "alice"
        owner = "Alice"
        group = "modal"
        budget = { monthly = 30.0 }
    """)
    t = cfg.clouds["modal-a"]
    assert (t.profile, t.owner, t.group) == ("alice", "Alice", "modal")


def test_owner_defaults_to_the_profile_name(tmp_path):
    """An approval has to be able to say whose credit it is about to spend, and a username says
    that; a second hand-written name would only drift out of date."""
    cfg = _write(tmp_path, """
        [clouds.modal-a]
        profile = "alice"
        budget = { monthly = 30.0 }
    """)
    assert cfg.clouds["modal-a"].owner == "alice"


def test_owner_falls_back_to_the_target_name_without_a_profile(tmp_path):
    cfg = _write(tmp_path, "[clouds.solo]\nbudget = { monthly = 5.0 }\n")
    assert cfg.clouds["solo"].owner == "solo"


def test_monthly_budget_alone_is_enough(tmp_path):
    """An account's monthly credit is the whole budget; a daily slice of it is meaningless."""
    cfg = _write(tmp_path, """
        [clouds.m]
        budget = { monthly = 30.0 }
    """)
    assert cfg.clouds["m"].monthly_budget == 30.0
    assert cfg.clouds["m"].daily_budget == 30.0


def test_a_budget_with_neither_figure_is_still_an_error(tmp_path):
    with pytest.raises(ValueError, match="budget"):
        _write(tmp_path, "[clouds.m]\nbudget = {}\n")


def test_one_job_may_spend_ten_dollars_unless_the_config_says_otherwise(tmp_path):
    """A job's lifetime cap is a safe default out of the box, and config rather than a constant,
    because it is meant to be raised as trust in the jobs grows."""
    cfg = _write(tmp_path, "[clouds.m]\nbudget = { monthly = 30.0 }\n")
    assert cfg.clouds["m"].max_job_cost == 10.0
    cfg = _write(tmp_path, "[clouds.m]\nbudget = { monthly = 30.0 }\nmax_job_cost = 25\n")
    assert cfg.clouds["m"].max_job_cost == 25.0


@pytest.mark.parametrize("value", ["0", "-5.0"])
def test_a_job_cap_must_be_positive(tmp_path, value):
    with pytest.raises(ValueError, match="max_job_cost must be positive"):
        _write(tmp_path, f"[clouds.m]\nbudget = {{ monthly = 30.0 }}\nmax_job_cost = {value}\n")


def test_groups_lists_members_in_config_order(tmp_path):
    # provider is explicit and identical on both members: it defaults to the target's own name,
    # and "b" and "a" differ, which would otherwise trip the mixed-provider check below.
    cfg = _write(tmp_path, """
        [clouds.b]
        provider = "modal"
        group = "modal"
        budget = { monthly = 30.0 }
        [clouds.a]
        provider = "modal"
        group = "modal"
        budget = { monthly = 30.0 }
        [clouds.solo]
        budget = { monthly = 5.0 }
    """)
    assert [t.name for t in cfg.groups()["modal"]] == ["b", "a"]
    assert "solo" not in cfg.groups()


def test_a_group_may_not_be_named_after_a_target(tmp_path):
    """`--on x` has to mean one thing. Catching this at load beats resolving it at submit."""
    with pytest.raises(ValueError, match="group 'a'"):
        _write(tmp_path, """
            [clouds.a]
            budget = { monthly = 30.0 }
            [clouds.b]
            group = "a"
            budget = { monthly = 30.0 }
        """)


def test_a_groups_members_must_share_a_provider(tmp_path):
    """Members are interchangeable by definition; two providers in one group are not."""
    with pytest.raises(ValueError, match="group 'mixed'"):
        _write(tmp_path, """
            [clouds.a]
            provider = "modal"
            group = "mixed"
            budget = { monthly = 30.0 }
            [clouds.b]
            provider = "runpod"
            group = "mixed"
            budget = { monthly = 30.0 }
        """)
