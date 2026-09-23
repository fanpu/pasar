"""Turning `[clouds.x]` into something that can actually launch a job."""

from pasar.cloud.providers import build_providers
from pasar.config import CloudTarget, Config


def _cfg(**targets):
    return Config(clouds={name: t for name, t in targets.items()})


def _target(name, provider):
    return CloudTarget(name=name, provider=provider, daily_budget=5.0, monthly_budget=50.0)


def _raising(target, state_dir):
    raise ImportError("No module named 'modal'")


def test_no_clouds_needs_nothing_installed(tmp_path):
    assert build_providers(Config(), tmp_path) == {}


def test_an_unknown_provider_is_skipped_not_fatal(tmp_path, caplog):
    """pasard has local jobs to run; a target it cannot build must not stop it starting. The
    daemon already settles jobs on a target with no provider, and says how to fix it."""
    providers = build_providers(_cfg(sky=_target("sky", "skypilot")), tmp_path)
    assert providers == {}
    assert "skypilot" in caplog.text


def test_a_provider_that_will_not_import_is_skipped_with_a_usable_message(tmp_path, caplog,
                                                                         monkeypatch):
    monkeypatch.setattr("pasar.cloud.providers._MODAL", _raising)
    providers = build_providers(_cfg(modal=_target("modal", "modal")), tmp_path)
    assert providers == {}
    assert "pip install" in caplog.text or "pasar[modal]" in caplog.text


def test_a_modal_target_gets_a_provider_with_its_own_state_directory(tmp_path, monkeypatch):
    built = {}

    def fake(target, state_dir):
        built["state_dir"] = state_dir
        return object()

    monkeypatch.setattr("pasar.cloud.providers._MODAL", fake)
    providers = build_providers(_cfg(modal=_target("modal", "modal")), tmp_path)
    assert set(providers) == {"modal"}
    assert built["state_dir"] == tmp_path / "cloud" / "modal"
