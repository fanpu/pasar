"""Tests for the agent-facing guide: the packaged agents.md, `pasar guide`, and /llms.txt.

The anti-rot tests below introspect cli.py's argparse definitions directly, so the guide is
forced to stay in sync with the CLI as flags and subcommands are added or renamed.
"""

import re

from fastapi.testclient import TestClient

from pasar import cli
from pasar.api import create_app
from pasar.guide import load_guide


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


def test_agents_md_alias_returns_the_same_guide_text(daemon):
    client = TestClient(create_app(daemon, allowed_hosts=["testserver"]))
    r = client.get("/agents.md")
    assert r.status_code == 200
    assert r.text == load_guide()


def test_llms_txt_is_not_swallowed_by_the_spa_catch_all(daemon):
    # If /llms.txt were registered after the `/{path:path}` SPA route, it would 404 or return
    # the unbuilt-webui placeholder instead of the guide.
    client = TestClient(create_app(daemon, allowed_hosts=["testserver"]))
    r = client.get("/llms.txt")
    assert "pasar web UI is not built" not in r.text


def test_every_cli_subcommand_is_documented():
    guide = load_guide()
    action = _subparsers_action(cli.build_parser())
    for name in action.choices:
        assert re.search(rf"\bpasar {re.escape(name)}\b", guide), f"'pasar {name}' missing"


def test_every_submit_flag_is_documented():
    guide = load_guide()
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
