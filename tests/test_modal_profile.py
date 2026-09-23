"""modal_profile.credentials: one account's token pulled out of ~/.modal.toml by name."""

import pytest

from pasar.cloud.modal_profile import credentials


def test_credentials_come_from_the_named_profile(tmp_path, monkeypatch):
    path = tmp_path / "modal.toml"
    path.write_text('[alice]\ntoken_id = "ak-1"\ntoken_secret = "as-1"\n'
                    '[bob]\ntoken_id = "ak-2"\ntoken_secret = "as-2"\nactive = true\n')
    monkeypatch.setenv("MODAL_CONFIG_PATH", str(path))
    assert credentials("alice") == ("ak-1", "as-1")


def test_an_unknown_profile_names_the_ones_that_exist(tmp_path, monkeypatch):
    path = tmp_path / "modal.toml"
    path.write_text('[alice]\ntoken_id = "ak-1"\ntoken_secret = "as-1"\n')
    monkeypatch.setenv("MODAL_CONFIG_PATH", str(path))
    with pytest.raises(KeyError, match="alice"):
        credentials("carol")


def test_a_profile_missing_a_token_is_an_error_not_a_silent_fallback(tmp_path, monkeypatch):
    """Falling back to the active profile would run one account's job on another's credit."""
    path = tmp_path / "modal.toml"
    path.write_text('[alice]\ntoken_id = "ak-1"\n')
    monkeypatch.setenv("MODAL_CONFIG_PATH", str(path))
    with pytest.raises(KeyError, match="token_secret"):
        credentials("alice")


def test_credentials_are_never_in_the_error_text(tmp_path, monkeypatch):
    path = tmp_path / "modal.toml"
    path.write_text('[alice]\ntoken_id = "ak-SECRET"\n')
    monkeypatch.setenv("MODAL_CONFIG_PATH", str(path))
    with pytest.raises(KeyError) as e:
        credentials("alice")
    assert "ak-SECRET" not in str(e.value)
