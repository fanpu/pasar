"""A stand-in for the modal package: enough of its surface for the provider, none of its network.

The provider takes its SDK as an object precisely so this can exist — so the unit suite runs on a
machine with no modal installed, no credentials and no bill.
"""

import threading


class NotFoundError(Exception):
    pass


class _Exceptions:
    NotFoundError = NotFoundError


class FakeImage:
    def __init__(self, steps=None):
        self.steps = list(steps or [])

    def _then(self, step):
        return FakeImage(self.steps + [step])

    def debian_slim(self, **kw):
        return FakeImage([("base", "debian_slim")])

    def from_registry(self, ref, **kw):
        return FakeImage([("base", ref)])

    def apt_install(self, *pkgs):
        return self._then(("apt", pkgs))

    def pip_install(self, *pkgs):
        return self._then(("pip", pkgs))

    def add_local_file(self, local_path, remote_path, *, copy=False):
        return self._then(("file", str(local_path), remote_path, copy))

    def add_local_dir(self, local_path, remote_path, *, copy=False, ignore=()):
        return self._then(("dir", str(local_path), remote_path, copy))

    def workdir(self, path):
        return self._then(("workdir", path))

    def env(self, mapping):
        return self._then(("env", dict(mapping)))

    def run_commands(self, *cmds):
        return self._then(("run", cmds))


class FakeSandbox:
    def __init__(self, sdk, args, kwargs):
        self.sdk = sdk
        self.args = args
        self.kwargs = kwargs
        self.object_id = f"sb-{len(sdk.sandboxes) + 1}"
        self.tags = dict(kwargs.get("tags") or {})
        self.terminated = False
        self.execs = []
        self._code = None
        self._out = _FakeStream()
        self._err = _FakeStream()

    # --- the surface the provider uses
    @property
    def stdout(self):
        return self._out

    @property
    def stderr(self):
        return self._err

    def get_tags(self):
        return dict(self.tags)

    def get_dashboard_url(self):
        return f"https://modal.test/{self.object_id}"

    def poll(self):
        return self._code

    def terminate(self):
        self.terminated = True
        if self._code is None:
            self.finish(137)

    def exec(self, *args):
        self.execs.append(args)
        return self

    # --- test helpers
    def emit(self, text, stream="stdout"):
        (self._out if stream == "stdout" else self._err).push(text)

    def finish(self, code):
        self._code = code
        self._out.close()
        self._err.close()


class _FakeStream:
    """Blocks like modal's does, so the provider's reader threads are exercised for real."""

    def __init__(self):
        self.chunks = []
        self.closed = False
        self.cv = threading.Condition()

    def push(self, text):
        with self.cv:
            self.chunks.append(text)
            self.cv.notify_all()

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()

    def __iter__(self):
        while True:
            with self.cv:
                while not self.chunks and not self.closed:
                    self.cv.wait(0.05)
                if self.chunks:
                    yield self.chunks.pop(0)
                    continue
                if self.closed:
                    return


class FakeApp:
    app_id = "ap-1"


class FakeVolume:
    def __init__(self, name, create_if_missing):
        self.name = name
        self.create_if_missing = create_if_missing


class FakeSDK:
    """Assembled to look like the `modal` module: `sdk.Sandbox.create(...)` and friends."""

    def __init__(self):
        self.sandboxes = []
        self.volumes = []
        self.rates_value = {"gpu_hour_cost_t4": 0.59, "gpu_hour_cost_a100_80gb": 2.5,
                            "cpu_hour_cost_sandbox": 0.1419,
                            "mem_gib_hour_cost_sandbox": 0.024}
        self.create_error = None
        self.created = threading.Event()
        sdk = self

        class Sandbox:
            @staticmethod
            def create(*args, **kwargs):
                if sdk.create_error:
                    raise RuntimeError(sdk.create_error)
                box = FakeSandbox(sdk, args, kwargs)
                sdk.sandboxes.append(box)
                sdk.created.set()
                return box

            @staticmethod
            def list(*, app_id=None, tags=None):
                for box in sdk.sandboxes:
                    if tags and any(box.tags.get(k) != v for k, v in tags.items()):
                        continue
                    yield box

        class App:
            @staticmethod
            def lookup(name, create_if_missing=False):
                return FakeApp()

        class Volume:
            @staticmethod
            def from_name(name, *, create_if_missing=False, **kw):
                vol = FakeVolume(name, create_if_missing)
                sdk.volumes.append(vol)
                return vol

        class Workspace:
            @staticmethod
            def from_context():
                class _W:
                    billing = type("B", (), {"rates": staticmethod(lambda: dict(sdk.rates_value))})
                return _W()

        self.Sandbox = Sandbox
        self.App = App
        self.Volume = Volume
        self.Workspace = Workspace
        self.Image = FakeImage()
        self.exception = _Exceptions()

    def wait_for_sandbox(self, timeout=5.0):
        assert self.created.wait(timeout), "no sandbox was created"
        return self.sandboxes[-1]
