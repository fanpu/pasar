"""The shell and environment a cloud attempt runs under."""

import shlex

from pasar.cloud import container as c
from tests.fakes_cloud import launch_request


def test_entry_script_execs_the_wrapper_last(tmp_path):
    """The wrapper has to end up as PID 1: a stop is delivered by signalling PID 1, and a shell
    left in front of it would swallow the signal and the job would never save a checkpoint."""
    req = launch_request(tmp_path)
    req.command = "python -m pasar_job.run -- train.py"
    script = c.entry_script(req)
    assert script.rstrip().endswith("exec python -m pasar_job.run -- train.py")


def test_entry_script_unpacks_the_bundle_and_links_the_venv(tmp_path):
    req = launch_request(tmp_path)
    script = c.entry_script(req)
    assert f"tar -xf {c.BUNDLE} -C {shlex.quote(c.WORK)}" in script
    assert f"ln -sfn {c.ENV}/.venv" in script
    assert "set -e" in script.splitlines()[0]


def test_entry_script_enters_the_jobs_own_directory(tmp_path):
    req = launch_request(tmp_path)
    req.rel_cwd = "experiments/lr"
    assert "cd /pasar/work/experiments/lr" in c.entry_script(req)


def test_entry_script_quotes_a_hostile_directory(tmp_path):
    req = launch_request(tmp_path)
    req.rel_cwd = "a dir; rm -rf /"
    line = next(x for x in c.entry_script(req).splitlines() if x.startswith("cd "))
    assert line == "cd " + shlex.quote("/pasar/work/a dir; rm -rf /")


def test_entry_script_root_directory_is_the_repository_root(tmp_path):
    req = launch_request(tmp_path)
    req.rel_cwd = "."
    assert "cd /pasar/work\n" in c.entry_script(req) + "\n"


def test_container_env_points_python_at_the_images_environment(tmp_path):
    env = c.container_env(launch_request(tmp_path))
    assert env["PATH"].startswith("/env/.venv/bin:")
    assert env["VIRTUAL_ENV"] == "/env/.venv"
    assert env["UV_PROJECT_ENVIRONMENT"] == "/env/.venv"
    assert env["UV_NO_SYNC"] == "1"
    assert env["PYTHONPATH"] == "/pasar/lib:/pasar/work"
    assert env["PYTHONUNBUFFERED"] == "1"


def test_container_env_cannot_be_overridden_by_the_job(tmp_path):
    """A --env the submitter asked for must not be able to point PYTHONPATH somewhere else and
    take the wrapper — the thing that reports the exit status — out of the picture."""
    req = launch_request(tmp_path)
    req.env = {"PYTHONPATH": "/evil", "WANDB_API_KEY": "k"}
    env = c.container_env(req)
    assert env["PYTHONPATH"] == "/pasar/lib:/pasar/work"
    assert env["WANDB_API_KEY"] == "k"


def test_persist_dir_is_per_job(tmp_path):
    req = launch_request(tmp_path, job_id=52)
    assert c.persist_dir(52) == "/pasar/persist/52"
    assert c.container_env(req)["PASAR_PERSIST_DIR"] == "/pasar/persist/52"
    assert c.persist_dir(52) in c.entry_script(req)
