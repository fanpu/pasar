"""One real Modal sandbox, end to end. Opt-in, CPU-only, and it costs a few cents.

Everything the fake-SDK tests in test_modal_provider.py prove is about this code's own logic;
none of it proves anything about Modal itself. This test launches one real, tiny, CPU-only
sandbox and checks the three behaviours the provider's design leans on but has only ever had
measured by throwaway probe scripts: a fresh reader replays a sandbox's stdout from the start,
a signal to PID 1 reaches the wrapper, and the job's persist dir round-trips through a real
Modal volume. (The fourth — that a non-copy `add_local_file` for the code bundle never rebuilds
the environment image — is true by construction: `_start` never passes `copy=True` for the
bundle mount, only `_describe_image` does for the lockfile. Confirming that empirically would
need a second image build to compare against, which this test does not pay for.)

Run with:
    PASAR_TEST_MODAL=1 PASAR_TEST_MODAL_PROFILE=<a ~/.modal.toml profile> \\
        uv run --with modal pytest tests/test_modal_live.py -m modal -s

PASAR_TEST_MODAL_PROFILE names whichever account pays for this; nothing here defaults it or
hard-codes a real profile, target, or workspace name — see modal_profile.credentials.

Everything this run creates lives under TARGET_NAME, never one of this account's own targets
(config.toml's `[clouds.*]` sections), so it gets its own `pasar-<name>` volume and app rather
than touching a real one, and JOB_ID is chosen far past anything pasar's own autoincrement job
ids will reach. Both are cleaned up in `finally`, alongside the sandbox itself.
"""

import os
import secrets
import shlex
import time

import pytest

from pasar.cloud.base import CloudLaunch, Phase
from pasar.cloud.bundle import build_bundle
from pasar.cloud.executor import wrapper_command
from pasar.cloud.modal_provider import ModalProvider
from pasar.cloud.pump import Pump
from pasar.config import CloudTarget

pytestmark = [
    pytest.mark.modal,
    pytest.mark.skipif(os.environ.get("PASAR_TEST_MODAL") != "1",
                       reason="set PASAR_TEST_MODAL=1 to run against the real Modal (costs money)"),
]

# Never a name this account's own config.toml gives a real target (`modal-a`..`modal-e` at the
# time this was written) — this is what keeps the volume and app the test creates ("pasar-" +
# this name) from ever being mistaken for, or colliding with, production's own.
TARGET_NAME = "modal-live-test"
# Comfortably past anything pasar's own `job_id INTEGER PRIMARY KEY AUTOINCREMENT` will reach,
# so a persist dir this test writes and deletes can never be a real job's.
JOB_ID = 99001


def _profile() -> str:
    profile = os.environ.get("PASAR_TEST_MODAL_PROFILE")
    if not profile:
        pytest.fail("set PASAR_TEST_MODAL_PROFILE to a ~/.modal.toml profile "
                    "(PASAR_TEST_MODAL=1 alone is not enough: this must never silently spend "
                    "from whichever profile happens to be active in ~/.modal.toml)")
    return profile


def _cleanup(provider: ModalProvider) -> None:
    """Remove everything this run created, best-effort: a cleanup step that raises must not stop
    the next one from running, and must not turn a passing test into a failure over tidiness.

    The sandbox itself is terminated by the caller before this runs (see the test body). What's
    left: the persist dir this job wrote (through the same API a real pull would use), then the
    volume and app themselves — safe to remove outright, not just the one job's directory,
    because TARGET_NAME is never a real target's name and nothing else was ever going to use
    either object.
    """
    try:
        provider.delete_persist(JOB_ID)
    except Exception as e:  # noqa: BLE001 - best-effort cleanup, see the docstring
        print(f"cleanup: could not delete job {JOB_ID}'s persist dir: {e}")
    volume_name = f"pasar-{TARGET_NAME}"
    try:
        provider.sdk.Volume.objects.delete(volume_name, client=provider.client,
                                           allow_missing=True)
        print(f"cleanup: deleted volume {volume_name}")
    except Exception as e:  # noqa: BLE001 - best-effort cleanup, see the docstring
        print(f"cleanup: could not delete volume {volume_name}: {e}")
    # There is no public SDK method to remove an App (`modal.App` has no `delete`/`stop`); the
    # CLI's own `modal app stop` reaches for the same internal AppStop RPC this does. That RPC
    # has to run inside modal's own synchronicity task context — called bare, off `client.stub`
    # directly, it silently returns an unawaited coroutine and does nothing (confirmed against
    # a real orphaned test App before this was fixed) — so it is wrapped the same way the CLI
    # wraps it: `synchronizer.create_blocking`, modal's own primitive for exactly this, run on
    # its own background loop rather than a fresh one this function starts. Best-effort even so:
    # an App left behind costs nothing and runs nothing once its one sandbox is terminated, so a
    # failure here is noted, not raised.
    try:
        from modal._utils.async_utils import synchronizer
        from modal_proto import api_pb2

        @synchronizer.create_blocking
        async def _app_stop(client, app_id: str) -> None:
            await client.stub.AppStop(api_pb2.AppStopRequest(
                app_id=app_id, source=api_pb2.APP_STOP_SOURCE_PYTHON_CLIENT))

        app = provider._modal_app()
        _app_stop(provider.client, app.app_id)
        print(f"cleanup: stopped app pasar-{TARGET_NAME} ({app.app_id})")
    except Exception as e:  # noqa: BLE001 - best-effort cleanup, see the docstring
        print(f"cleanup: could not stop app pasar-{TARGET_NAME}: {e}")


def test_a_real_attempt_runs_reports_and_stops(tmp_path):
    token = secrets.token_hex(8)
    bundle = build_bundle(".", tmp_path / "b.tar", 512 << 20)
    target = CloudTarget(name=TARGET_NAME, provider="modal", daily_budget=1.0, monthly_budget=1.0,
                         profile=_profile())
    provider = ModalProvider(target, tmp_path / "state")
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    # Writes one small file into its persist dir so the round trip through a real Modal volume
    # (write from inside the sandbox, then Provider.download_persist from outside) is exercised
    # by this same run, not a separate one.
    inner = (
        "import os, pasar_job, time; pasar_job.progress(1, 2); "
        "open(os.path.join(pasar_job.persist_dir(), 'hello.txt'), 'w').write('hi from modal'); "
        "print('hello from modal', flush=True); time.sleep(120)"
    )
    req = CloudLaunch(
        job_id=JOB_ID, attempt=1, bundle=bundle,
        image_key=provider.prepare_image(bundle.env),
        command=wrapper_command(f"python -c {shlex.quote(inner)}", token, limit=300, grace=20),
        rel_cwd=bundle.rel_cwd, gpu="", gpu_count=0, env={}, volumes={}, timeout=420,
        tags={"pasar_job": str(JOB_ID), "pasar_attempt": "1", "pasar_target": TARGET_NAME})

    started = time.time()
    handle = provider.launch(req)
    pump = Pump(provider, handle, token, job_dir, on_sample=lambda rows: None,
               on_exit=lambda obj: None)
    try:
        # --- stdout replay from the start: read_output(handle, 0) onward must carry this
        # sandbox's very first line, proving a fresh reader (a restarted pasard re-attaching by
        # tag) is never missing output that arrived before it started following.
        deadline = time.time() + 900
        while time.time() < deadline:
            pump.poll()
            log = (job_dir / "output.log").read_text() if (job_dir / "output.log").exists() else ""
            if "hello from modal" in log:
                break
            assert provider.status(handle).phase is not Phase.EXITED, log
            time.sleep(2)
        else:
            pytest.fail("the job's output never arrived")
        build_and_start = time.time() - started
        print(f"first output after {build_and_start:.0f}s (includes any image build)")

        events = (job_dir / "events.jsonl").read_text()
        assert '"event": "progress"' in events  # the control-line channel works over Modal stdout

        # --- signal to PID 1 reaches the wrapper: request_stop signals the sandbox, and only the
        # wrapper (running as PID 1, per entry_script's `exec`) can be what turns that into the
        # "stopped" exit_info the pump reads back over the same stdout channel.
        stop_started = time.time()
        provider.request_stop(handle)
        deadline = time.time() + 180
        while time.time() < deadline and pump.exit_info is None:
            pump.poll()
            time.sleep(2)
        assert pump.exit_info is not None, "the wrapper never reported how it ended"
        assert pump.exit_info.get("reason") == "stopped"
        print(f"stop acknowledged after {time.time() - stop_started:.0f}s")

        # Ends the sandbox for real (rather than leaving it to its own 420s timeout), which is
        # also what forces Modal's volume mount to commit whatever the job wrote — the download
        # below is not meant to race that.
        provider.terminate(handle)

        # --- persist dir round trip: what the job wrote under $PASAR_PERSIST_DIR while it ran
        # must come back through Provider.download_persist, byte for byte, from a real volume —
        # not the in-memory fake test_modal_provider.py exercises this same method against.
        persist_started = time.time()
        dest = tmp_path / "pulled"
        deadline = time.time() + 120
        files = count = 0
        while time.time() < deadline:
            count, _ = provider.persist_usage(JOB_ID)
            if count:
                files, _ = provider.download_persist(JOB_ID, dest)
                break
            time.sleep(3)
        assert count and files, "the job's persist dir never showed up on the volume"
        assert (dest / "hello.txt").read_text() == "hi from modal"
        print(f"persist dir round-tripped after {time.time() - persist_started:.0f}s")
    finally:
        provider.terminate(handle)  # idempotent; the no-op path if the block above already did
        _cleanup(provider)
        provider.close()
    # A hard bound on the whole test, not just its pieces: the sandbox's own 420s timeout and
    # the wrapper's 300s --limit already guarantee nothing here can outlive them even if this
    # process dies, and the bounded loops above (900 + 180 + 120s) cap what a live process can
    # spend waiting; this is the belt-and-suspenders check that they held.
    assert time.time() - started < 1300
