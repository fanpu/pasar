"""pasar.cloud.check: the repeatable version of Task 0's survey -- `pasar cloud check`."""

import datetime

import pytest

from pasar.cloud.check import check_targets
from pasar.config import CloudTarget
from tests.fakes_modal import FakeSDK, billing_summary


def target(name="modal-a", profile="alice", owner="alice", group="modal", **kw):
    return CloudTarget(name=name, provider="modal", profile=profile, owner=owner, group=group,
                       daily_budget=30.0, monthly_budget=30.0, **kw)


@pytest.fixture
def modal_toml(tmp_path, monkeypatch):
    """Writes `~/.modal.toml` (here, a tmp file) with one section per (profile, token_id); the
    check module reads it through `modal_profile.credentials`, exactly like production does."""
    path = tmp_path / "modal.toml"
    monkeypatch.setenv("MODAL_CONFIG_PATH", str(path))

    def write(**profiles):
        lines = []
        for profile, token_id in profiles.items():
            lines.append(f'[{profile}]\ntoken_id = "{token_id}"\ntoken_secret = "secret-{token_id}"\n')
        path.write_text("\n".join(lines))

    return write


def test_no_targets_with_a_profile_has_nothing_to_check():
    result = check_targets([target(profile="")], sdk=FakeSDK())
    assert result.targets == [] and result.collisions == {} and result.ok is True


def test_a_healthy_target_authenticates_and_reads_its_workspace_and_credit(modal_toml):
    modal_toml(alice="ak-alice")
    sdk = FakeSDK()
    sdk.workspace_names["ak-alice"] = "alice-workspace"
    cycle_start = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)
    cycle_end = datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC)
    sdk.billing_summaries["ak-alice"] = billing_summary(metered=7.5)

    result = check_targets([target()], sdk=sdk)

    [check] = result.targets
    assert check.ok is True and check.error is None
    assert check.workspace == "alice-workspace"
    assert check.owner == "alice" and check.group == "modal"
    assert check.credit_used == pytest.approx(7.5)
    assert check.budget_left == pytest.approx(22.5)
    assert check.exhausted is False
    assert check.cycle_start == cycle_start.timestamp()
    assert check.cycle_end == cycle_end.timestamp()
    assert result.ok is True
    assert sdk.verify_calls == [("https://fake.modal.test", ("ak-alice", "secret-ak-alice"))]


def test_a_positive_billed_cost_means_the_account_is_exhausted(modal_toml):
    modal_toml(alice="ak-alice")
    sdk = FakeSDK()
    sdk.billing_summaries["ak-alice"] = billing_summary(metered=32.0, billed=2.0)

    [check] = check_targets([target()], sdk=sdk).targets
    assert check.ok is True  # still usable; it authenticated and reached its workspace
    assert check.exhausted is True


def test_credit_used_is_read_the_way_the_daemon_reads_it(modal_toml):
    """The larger of `metered_cost` and its own breakdown, exactly as `ModalProvider.credit`:
    one formula, so the check and the group choice never disagree about an account."""
    modal_toml(alice="ak-alice")
    sdk = FakeSDK()
    sdk.billing_summaries["ak-alice"] = billing_summary(metered=4.0,
                                                        breakdown={"gpu": 5.0, "cpu": 1.5})
    [check] = check_targets([target()], sdk=sdk).targets
    assert check.credit_used == pytest.approx(6.5)
    assert check.budget_left == pytest.approx(23.5)


def test_a_billing_failure_is_logged_by_its_type_alone(modal_toml, caplog):
    modal_toml(alice="ak-alice")
    sdk = FakeSDK()
    sdk.billing_errors["ak-alice"] = "token ak-alice rejected by upstream"
    check_targets([target()], sdk=sdk)
    assert "RuntimeError" in caplog.text
    assert "ak-alice rejected" not in caplog.text and "Traceback" not in caplog.text


def test_an_unknown_profile_is_unusable_and_never_prints_a_token(modal_toml):
    modal_toml(bob="ak-bob")  # "alice" is not in the file at all
    sdk = FakeSDK()

    [check] = check_targets([target(profile="alice")], sdk=sdk).targets
    assert check.ok is False
    assert "alice" in check.error
    assert "ak-bob" not in check.error and "secret" not in check.error.lower()


def test_a_token_that_fails_to_verify_is_reported_unusable(modal_toml):
    modal_toml(alice="ak-alice")
    sdk = FakeSDK()
    sdk.verify_errors["ak-alice"] = "revoked"

    [check] = check_targets([target()], sdk=sdk).targets
    assert check.ok is False and check.workspace is None
    assert "revoked" not in check.error  # the provider's own exception text is not trusted


def test_two_targets_reaching_the_same_workspace_are_a_collision(modal_toml):
    modal_toml(alice="ak-alice", bob="ak-bob")
    sdk = FakeSDK()
    sdk.workspace_names["ak-alice"] = "shared-workspace"
    sdk.workspace_names["ak-bob"] = "shared-workspace"

    result = check_targets([
        target(name="modal-a", profile="alice", owner="alice"),
        target(name="modal-b", profile="bob", owner="bob"),
    ], sdk=sdk)

    assert result.collisions == {"shared-workspace": ["modal-a", "modal-b"]}
    assert result.ok is False  # both targets are individually fine; the collision alone fails it
    assert all(t.ok for t in result.targets)


def test_distinct_workspaces_are_not_a_collision(modal_toml):
    modal_toml(alice="ak-alice", bob="ak-bob")
    sdk = FakeSDK()
    sdk.workspace_names["ak-alice"] = "alice-workspace"
    sdk.workspace_names["ak-bob"] = "bob-workspace"

    result = check_targets([
        target(name="modal-a", profile="alice"),
        target(name="modal-b", profile="bob"),
    ], sdk=sdk)
    assert result.collisions == {} and result.ok is True


def test_a_billing_summary_that_cannot_be_read_leaves_the_target_ok_with_no_credit_figures(modal_toml):
    modal_toml(alice="ak-alice")
    sdk = FakeSDK()
    sdk.billing_errors["ak-alice"] = "503"

    [check] = check_targets([target()], sdk=sdk).targets
    # A billing outage is not a reason to call the account unusable: the workspace was reached,
    # which is already enough to catch a collision, and the ledger's own figures still gate
    # every launch.
    assert check.ok is True
    assert check.credit_used is None and check.budget_left is None and check.exhausted is False


def test_works_when_modal_is_not_installed(monkeypatch):
    def fail_import(name, *a, **kw):
        raise ImportError(f"No module named {name!r}")
    monkeypatch.setattr("pasar.cloud.check.importlib.import_module", fail_import)

    result = check_targets([target()])  # no sdk= override: takes the lazy-import path

    [check] = result.targets
    assert check.ok is False and check.error == "modal is not installed"
    assert result.ok is False
