"""Tests for the agent-facing guide: the packaged agents.md, `pasar guide`, and /llms.txt, and
the cloud topic (agents-cloud.md, `pasar guide cloud`, /llms-cloud.txt).

The anti-rot tests below introspect cli.py's argparse definitions directly, so the guide is
forced to stay in sync with the CLI as flags and subcommands are added or renamed. Cloud-only
commands and flags are documented in the cloud topic rather than the main guide (which keeps
only the hard rules for cloud jobs), so those tests read both.
"""

import re

import pytest
from fastapi.testclient import TestClient

from pasar import cli
from pasar.api import create_app
from pasar.guide import TOPICS, load_guide

# Submit flags that only mean anything for a cloud job; the cloud topic must document each one.
CLOUD_SUBMIT_FLAGS = ("--on", "--gpu", "--env", "--data", "--max-cost")


def _all_guides() -> str:
    return "\n".join(load_guide(topic) for topic in (None, *TOPICS))


def _subparsers_action(parser):
    for action in parser._actions:
        if getattr(action, "choices", None):
            return action
    raise AssertionError("no subparsers action found")


def test_guide_loads_from_the_installed_package():
    text = load_guide()
    assert text.startswith("#")
    assert "pasar submit" in text
    assert 250 <= len(text.splitlines()) <= 400


def test_cloud_topic_loads_from_the_installed_package():
    text = load_guide("cloud")
    assert text.startswith("#")
    assert "pasar submit --on" in text
    assert text != load_guide()
    assert 100 <= len(text.splitlines()) <= 400


def test_unknown_topic_is_an_error_naming_the_topics():
    with pytest.raises(ValueError, match="cloud"):
        load_guide("nope")


def test_main_guide_points_at_the_cloud_topic_and_keeps_the_hard_rules():
    guide = load_guide()
    cloud = guide[guide.index("## Cloud jobs"):]
    cloud = cloud[:cloud.index("\n## ", 1)]
    assert "pasar guide cloud" in cloud
    assert "approve" in cloud and "max_job_cost" in cloud and "--max-cost" in cloud
    assert "explicit" in cloud


def test_pasar_guide_cloud_prints_the_cloud_topic_without_a_daemon(capsys, monkeypatch):
    monkeypatch.delenv("PASAR_URL", raising=False)
    code = cli.main(["guide", "cloud"])
    assert code == 0
    assert capsys.readouterr().out == load_guide("cloud")


def test_pasar_guide_rejects_an_unknown_topic(capsys):
    code = cli.main(["guide", "nope"])
    assert code == cli.EX_USAGE
    assert "cloud" in capsys.readouterr().err


def test_llms_cloud_txt_returns_the_cloud_topic(daemon):
    client = TestClient(create_app(daemon, allowed_hosts=["testserver"]))
    r = client.get("/llms-cloud.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text == load_guide("cloud")


def test_every_cloud_submit_flag_and_command_is_in_the_cloud_topic():
    cloud = load_guide("cloud")
    for opt in CLOUD_SUBMIT_FLAGS:
        assert opt in cloud, f"{opt} missing from the cloud topic"
    for name in ("pull", "cloud"):
        assert re.search(rf"\bpasar {name}\b", cloud), f"'pasar {name}' missing"


def test_pasar_guide_prints_the_guide_without_a_daemon(capsys, monkeypatch):
    # No client is passed and no daemon is running anywhere; `guide` must not need either.
    monkeypatch.delenv("PASAR_URL", raising=False)
    code = cli.main(["guide"])
    assert code == 0
    assert capsys.readouterr().out == load_guide()


def test_guide_is_in_cli_help(capsys):
    code = cli.main(["--help"])
    assert code == 0
    assert "guide" in capsys.readouterr().out


def test_llms_txt_returns_the_same_guide_text(daemon):
    client = TestClient(create_app(daemon, allowed_hosts=["testserver"]))
    r = client.get("/llms.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text == load_guide()


def test_llms_txt_is_not_swallowed_by_the_spa_catch_all(daemon):
    # If /llms.txt were registered after the `/{path:path}` SPA route, it would 404 or return
    # the unbuilt-webui placeholder instead of the guide.
    client = TestClient(create_app(daemon, allowed_hosts=["testserver"]))
    r = client.get("/llms.txt")
    assert "pasar web UI is not built" not in r.text


def test_every_cli_subcommand_is_documented():
    guide = _all_guides()
    action = _subparsers_action(cli.build_parser())
    for name in action.choices:
        assert re.search(rf"\bpasar {re.escape(name)}\b", guide), f"'pasar {name}' missing"


def test_every_submit_flag_is_documented():
    guide = _all_guides()
    action = _subparsers_action(cli.build_parser())
    submit = action.choices["submit"]
    for act in submit._actions:
        for opt in act.option_strings:
            if opt in ("-h", "--help"):
                continue
            assert opt in guide, f"{opt} missing from guide"


def test_wait_exit_codes_match_cli_constants():
    guide = load_guide()
    for code in (0, 1, 2, 3, cli.WAIT_TIMEOUT):
        assert re.search(rf"\b{code}\b", guide), f"exit code {code} missing from guide"
    for code in (cli.EX_USAGE, cli.EX_UNAVAILABLE, cli.EX_API):
        assert str(code) in guide, f"exit code {code} missing from guide"


def test_the_cloud_topic_compares_the_gb10s_measured_speed_like_for_like():
    # The GB10 row is what it achieved here; the other rows are vendor peaks. The guide has to
    # carry the measurement, not a placeholder, and say the two are not the same kind of figure.
    text = load_guide("cloud")
    assert "MEASURED_" not in text
    [row] = [line for line in text.splitlines() if line.startswith("| GB10")]
    assert "88" in row and "240" in row and "273" in row
    assert "achieved" in text and "peak" in text
