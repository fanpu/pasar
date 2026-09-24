"""The interface a cloud provider implements, and the types it exchanges with pasar."""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pasar.cloud.bundle import Bundle, EnvSpec


@dataclass(frozen=True)
class GpuRow:
    """One GPU a target can be asked for: the name `--gpu` accepts, its live price, and its
    memory in GB. `memory_gb` is `None` when the provider's memory table has never heard of this
    GPU — it still lists, priced, rather than vanishing; a person or agent reading `pasar cloud`
    sees it exists even though nobody has looked up how much memory it carries yet."""
    name: str
    hourly_rate: float
    memory_gb: float | None = None


@dataclass(frozen=True)
class PersistFile:
    """One file in a job's persist dir as a listing saw it: its path relative to that dir
    (`/`-separated), its size, and its modification time. A pull's manifest is a list of these,
    and it is compared whole — a file whose size or mtime moved is a different file — because
    the manifest is exactly what the pull may delete: see `Provider.delete_persist_files`."""
    path: str
    size: int
    mtime: float


def gpu_rows(rates: dict[str, float], names: dict[str, str],
             memory_gb: dict[str, float]) -> list[GpuRow]:
    """Collapse `rates`'s `gpu_hour_cost_*` keys into one row per real GPU, cheapest first.

    `rates` is a provider's whole price list: `gpu_hour_cost_*` keys aliased both ways between
    dashes and underscores (see `aliased`), plus CPU/memory/volume/endpoint keys
    that are not GPUs at all and are skipped by their prefix alone. Two aliases of the same GPU
    — `a100_80gb` and `a100-80gb` — must not become two rows: everything not given an explicit
    name in `names` is folded onto Modal's own dash style (`replace("_", "-").upper()`), so both
    spellings land on the same key and the second write just overwrites the first with the same
    price. `names` exists only for the GPUs whose rate-key spelling does not already match that
    style — `a10g` -> `A10`, `rtx6000` -> `RTX-PRO-6000` — so the row's name is always one Modal's
    own `gpu=` accepts, not a guess built from the rate table's own habits.

    Every row's name must also round-trip back through `hourly_rate`'s `.lower()` to a key still
    present in `rates`; that only holds if every alias this produces was itself written into
    `rates` first, which is exactly what `aliased` (given the same `names` exceptions)
    guarantees.

    `memory_gb` is keyed by the row's display name, not the rate key's spelling, and is a static,
    hand-checked table — a GPU missing from it still gets a row, with `memory_gb=None`.
    """
    best: dict[str, GpuRow] = {}
    for key, rate in rates.items():
        if not key.startswith("gpu_hour_cost_"):
            continue
        basename = key[len("gpu_hour_cost_"):]
        display = names.get(basename, basename.replace("_", "-").upper())
        best[display] = GpuRow(display, rate, memory_gb.get(display))
    return sorted(best.values(), key=lambda r: r.hourly_rate)


GPU_RATE = "gpu_hour_cost_"


def gpu_spellings(name: str, names: dict[str, str]) -> set[str]:
    """Every spelling a GPU's rate key may need, given one of them (without `GPU_RATE`): its
    rate-key name with dashes and underscores both ways, plus — for a GPU in `names`, whose
    `--gpu` name is not just its rate key's own (`a10g` -> `A10`) — that name lowercased, both
    ways too. Works from either end: `a10` finds its way back to `a10g` as well."""
    shown = {key: display.lower() for key, display in names.items()}
    for key, display in shown.items():
        if name in (display, display.replace("-", "_")):
            name = key
    out = {name, name.replace("_", "-"), name.replace("-", "_")}
    if name in shown:
        out |= {shown[name], shown[name].replace("-", "_")}
    return out


def aliased(rates: dict[str, float], names: dict[str, str]) -> dict[str, float]:
    """`rates` with each GPU also priced under every spelling of its name (`gpu_spellings`).
    `hourly_rate` looks up `gpu_hour_cost_<--gpu lowercased>`, so `--gpu A100-80GB` asks for
    `…a100-80gb` while Modal lists `…a100_80gb`; an unpriced GPU refuses to estimate, which means
    the job never starts. A key `rates` has itself is never overwritten by another's alias."""
    out = dict(rates)
    for key, value in rates.items():
        if key.startswith(GPU_RATE):
            for spelling in gpu_spellings(key[len(GPU_RATE):], names):
                out.setdefault(GPU_RATE + spelling, value)
    return out


def price_list(live: dict[str, float], pinned: dict[str, float],
               names: dict[str, str]) -> dict[str, float]:
    """A target's effective prices: the provider's `live` list with the config's `pinned` rates
    on top, each aliased (`aliased`) before they meet. Aliasing the two separately is what lets
    a pinned rate win under every spelling of its GPU, whichever spelling it was written in —
    otherwise `gpu_hour_cost_a100_80gb` pinned in the config would price `--gpu A100-80GB` at
    the live rate still sitting under `…a100-80gb`, and `pasar cloud` would list whichever of the
    two happened to come last."""
    return {**aliased(live, names), **aliased(pinned, names)}


def parse_gpu(spec: str) -> tuple[str, int]:
    """'H100' or 'A100-80GB:4' -> (type, count)."""
    kind, _, count = spec.partition(":")
    if not kind or (count and not count.isdigit()):
        raise ValueError(f"bad --gpu {spec!r}: expected TYPE or TYPE:COUNT, e.g. H100 or H100:4")
    n = int(count) if count else 1
    if n < 1:
        raise ValueError(f"bad --gpu {spec!r}: count must be at least 1")
    return kind, n


@dataclass
class CloudLaunch:
    job_id: int
    attempt: int
    bundle: Bundle
    image_key: str
    command: str
    rel_cwd: str
    gpu: str
    gpu_count: int
    env: dict[str, str]
    volumes: dict[str, str]
    timeout: int
    tags: dict[str, str]


class Phase(StrEnum):
    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    GONE = "gone"


@dataclass
class CloudStatus:
    phase: Phase
    exit_code: int | None = None
    ended_by_provider: bool = False
    gpu_type: str = ""
    console_url: str = ""
    times: dict[str, float] = field(default_factory=dict)  # phase -> unix time


@dataclass(frozen=True)
class Credit:
    """What a provider's own books say one account has used this billing cycle.

    pasar's ledger only knows what pasar spent; this also counts what it could not see — an image
    build, a sandbox somebody started by hand, a collaborator's own work on the same account."""
    used: float            # spent this cycle, by anyone, from the provider's own books
    limit: float | None    # the provider's own allowance, None if it does not say
    exhausted: bool        # the provider says this account is past its free allowance
    cycle_start: float     # unix time this billing cycle began
    cycle_end: float


@dataclass(frozen=True)
class Capabilities:
    graceful_stop: bool = True
    replay_output: bool = True
    billing: bool = False
    credit: bool = False   # implements `Provider.credit`


class Provider(Protocol):
    """A rented-GPU backend. Handles are opaque strings the provider assigns at launch."""

    name: str
    caps: Capabilities
    # Rate-key names whose `--gpu` spelling differs from the key's own (Modal's `a10g` is
    # `--gpu A10`), for `aliased`/`price_list`; empty for a provider with none.
    gpu_names: dict[str, str]

    def prepare_image(self, env: EnvSpec) -> str:
        """Build or reuse an image for this environment; return an image key to pass to launch()."""

    def launch(self, req: CloudLaunch) -> str:
        """Start the attempt and return a handle; raises if the provider refuses to schedule it."""

    def status(self, handle: str) -> CloudStatus:
        """Report the attempt's current phase; Phase.GONE once the provider has forgotten it."""

    def read_output(self, handle: str, cursor: int) -> tuple[bytes, int]:
        """Return the attempt's combined stdout and stderr from byte offset `cursor` onward, and
        the offset the returned bytes end at.

        The bytes must begin at *exactly* `cursor`, counted from the first byte the attempt ever
        wrote, so `cursor` 0 replays the whole stream. `Pump.poll` leans on that and on nothing
        else: it counts the bytes it consumes itself and discards the cursor returned here, so a
        provider that serves a window starting anywhere else — clipped to a retention limit,
        rounded to a chunk boundary, skipped ahead after a gap — would have that output spliced
        into the job's log at the wrong offset, silently and with nothing to notice it by.

        Returning fewer bytes than are available is fine: the next poll asks again from where
        this one ended, and the pump only ever advances past whole lines, so it routinely
        re-requests the tail it has not consumed. A cursor at or past the end returns no bytes,
        not an error.
        """

    def request_stop(self, handle: str) -> None:
        """Ask the attempt to exit on its own; a no-op backend still needs terminate() to end it."""

    def terminate(self, handle: str) -> None:
        """End the attempt immediately, however the provider must, and mark it exited."""

    def list(self) -> list[tuple[str, dict[str, str]]]:
        """List handles the provider still knows about, with the tags each was launched with."""

    def rates(self) -> dict[str, float]:
        """Return this provider's current price list, keyed by billing dimension."""

    def gpus(self, rates: dict[str, float]) -> list[GpuRow]:
        """This target's GPUs as clean rows, built from `rates` — typically a caller's own
        already-fetched price list, not a fresh call to `rates()`: `Daemon.cloud_gpus` passes its
        cached `cloud_rates()` result so that listing a target's GPUs never costs a second
        provider round-trip on top of the one pricing already made. Pure: makes no calls of its
        own. Name as `--gpu` accepts it, live $/hour, and memory in GB (`None` if unknown). See
        `gpu_rows`."""

    def upload(self, paths: list[str], key: str) -> str:
        """Store data under key for later use as a launch volume; return a location to reference it."""

    def persist_usage(self, job_id: int) -> tuple[int, int]:
        """(file count, total bytes) under this job's persist dir; (0, 0) if there is none.

        Sized ahead of a pull, against the free-space guard, and shown on a job before anyone
        has fetched what it left behind — so this must answer without downloading anything.
        """

    def persist_manifest(self, job_id: int) -> list[PersistFile]:
        """Every file under this job's persist dir as it stands right now; `[]` if there is no
        dir. What a pull downloads, verifies against and — file by file — deletes, so a file
        written after this listing is never among what the pull deletes. Lists; never
        downloads."""

    def download_persist(self, job_id: int, dest: Path) -> tuple[int, int]:
        """Copy the job's persist dir into `dest`, returning what was written as (files, bytes).
        Creates `dest`. Raises rather than half-succeeding silently.

        A persist dir is the only copy of a job's data, so the caller verifies these counts
        against what the provider reported before it ever deletes the remote copy. Raising here
        rather than swallowing a partial failure is what makes that check meaningful: a `dest`
        this returned from must be exactly what it claims, or the caller has no way to tell a
        finished pull from one that stopped halfway and would delete the only copy regardless.
        """

    def delete_persist_files(self, job_id: int, files: list[PersistFile]) -> None:
        """Remove exactly `files` from the job's persist dir, one at a time and never
        recursively, then each directory they leave empty, the job's own dir last. A file not in
        `files` stays, and so does every directory above it: this is how a pull deletes only what
        it verified, whatever was written to the volume in the meantime — a mount's background
        commit can land a final checkpoint after the pull listed the dir. A file already gone is
        skipped, not an error.
        """

    def delete_persist(self, job_id: int) -> None:
        """Remove the job's persist dir at the provider, recursively, whatever is in it. A
        no-op if it is already gone. Only the retention sweep calls this: past the window,
        deleting everything is the policy. A pull deletes by manifest instead
        (`delete_persist_files`).

        Reachable only for a job whose checkpoint nothing will ever resume from again — never
        for `awaiting`, see `pasar.models.TERMINAL`. A no-op rather than an error because the
        sweep that calls this can race a person's own manual pull or delete.
        """

    def billed_cost(self, handles: list[str], since: float) -> dict[str, float] | None:
        """Return actual billed cost per handle since the given time, or None if unsupported."""

    def credit(self) -> Credit | None:
        """Optional (`caps.credit`): what the provider itself says this account has used, or
        None if it cannot say right now. A network call: `Daemon.cloud_credit` caches it and
        only ever calls it off the tick."""
