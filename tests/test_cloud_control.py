from pasar_job import run as wrapper

from pasar.cloud.control import encode, split


def test_wrapper_and_daemon_agree_on_the_format(capsys):
    wrapper.emit("tok", {"t": "event", "e": {"event": "progress", "step": 1}})
    # .strip() would also eat the leading \x1e: it counts as whitespace under str.isspace().
    line = capsys.readouterr().out.rstrip("\n")
    assert split(line, "tok") == {"t": "event", "e": {"event": "progress", "step": 1}}
    assert encode("tok", {"t": "event", "e": {"event": "progress", "step": 1}}).decode() == line + "\n"


def test_job_output_is_not_mistaken_for_control(capsys):
    assert split("\x1epasar:other {\"t\":\"exit\"}", "tok") is None
    assert split("regular log line", "tok") is None
