"""Where a cloud attempt's code, environment and checkpoints live inside the container.

Provider-neutral on purpose: every provider unpacks the same bundle into the same paths and runs
the same entry shell, so a job that runs on one runs on the next without knowing which it is.
"""

import shlex

from pasar.cloud.base import CloudLaunch

WORK = "/pasar/work"        # the repository, unpacked from the bundle: this is its root
ENV = "/env"                # the image's uv project, and so /env/.venv, the environment
LIB = "/pasar/lib"          # pasar_job, injected so the wrapper runs whatever the job depends on
PERSIST = "/pasar/persist"  # a provider volume: outlives the attempt, so checkpoints do too
BUNDLE = "/pasar/bundle.tar"


def persist_dir(job_id: int) -> str:
    """One directory per job, not per attempt: the whole point is that the next attempt finds
    the checkpoint the last one wrote."""
    return f"{PERSIST}/{job_id}"


def container_env(req: CloudLaunch) -> dict[str, str]:
    """The attempt's whole environment. pasar's own variables are applied last, so a --env the
    submitter asked for cannot point PYTHONPATH elsewhere and take the wrapper — the only thing
    that reports the real exit status — out of the picture."""
    env = dict(req.env)
    env.update({
        # The image's environment first, so `python` is the project's interpreter and the
        # wrapper's `python -m pasar_job.run` finds the packages the job's lockfile pinned.
        "PATH": f"{ENV}/.venv/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin",
        "VIRTUAL_ENV": f"{ENV}/.venv",
        "UV_PROJECT_ENVIRONMENT": f"{ENV}/.venv",
        # `uv run train.py` is a normal way to start a job, and without these it would try to
        # re-resolve the lockfile in a container with no write access to the image's environment.
        "UV_NO_SYNC": "1",
        "UV_FROZEN": "1",
        # The project is not installed into the environment (--no-install-project, so a code
        # change never invalidates the image), so its root goes on the path as an editable
        # install would put it there. /pasar/lib comes first: pasar_job must be the injected
        # copy the wrapper was built against, not whatever version the job pinned.
        "PYTHONPATH": f"{LIB}:{WORK}",
        # Without this the relay sees a job's output only when it exits, so a running job would
        # show no logs, no progress and no ETA — on hardware billed by the second.
        "PYTHONUNBUFFERED": "1",
        "PASAR_PERSIST_DIR": persist_dir(req.job_id),
    })
    return env


def entry_script(req: CloudLaunch) -> str:
    """The shell the container runs. It ends in `exec` so the wrapper replaces this shell and
    becomes PID 1: a graceful stop is delivered by signalling PID 1, and a shell left in front
    of the wrapper would swallow the signal, and with it the job's last checkpoint."""
    work = shlex.quote(WORK)
    # A plain join would carry a hostile rel_cwd's trailing "/" straight through, which
    # shlex.quote must preserve; only "." is special-cased away so the root doesn't end up
    # as "/pasar/work/.".
    joined = WORK if req.rel_cwd == "." else f"{WORK}/{req.rel_cwd}"
    cwd = shlex.quote(joined)
    persist = shlex.quote(persist_dir(req.job_id))
    return "\n".join([
        "set -e",
        f"mkdir -p {work} {persist}",
        f"tar -xf {BUNDLE} -C {work}",
        # -f alone would descend into a real .venv directory that came along in the bundle and
        # leave the link inside it, so the job would run against a half-built environment.
        f"rm -rf {work}/.venv",
        f"ln -sfn {ENV}/.venv {work}/.venv",
        f"cd {cwd}",
        # req.command is already one fully-quoted shell line (see wrapper_command), so it goes
        # in verbatim: quoting it again would hand the wrapper its own command line as one word.
        f"exec {req.command}",
    ]) + "\n"
