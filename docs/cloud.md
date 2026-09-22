# Cloud jobs (design proposal)

**Status: proposal, not implemented.** This extends [design.md](design.md) so that a job can run on
rented cloud GPUs instead of the local GPU when someone explicitly asks for burst compute. Modal is
the first provider. Everything provider-specific sits behind a small interface so that other GPU
clouds (RunPod, Lambda, a SkyPilot bridge, …) can be added later without touching the daemon,
scheduler, protocol or UI.

## Goals

- **Burst compute on request.** `pasar submit --on modal --gpu H100 …` runs a job in the cloud, with the
  same job record, logs, events, progress, checkpoints, cancel and `pasar wait` as a local job.
- **Never spend money by accident.** Cloud jobs only exist when a person asks for them: no automatic
  placement, no spillover, nothing enabled until it is configured with a budget.
- **The same command on both backends.** `.venv/bin/python train.py` works unchanged in the cloud.
- **Provider-neutral core.** A new provider is one module implementing a narrow interface. Packaging
  the job, streaming its output, events, GPU telemetry, the cost ledger, and the UI are shared.
- **Cloud-native stats**: cost (live estimate, then billed), startup breakdown, the exact GPUs you
  got, and per-GPU telemetry.

## Non-goals (for now)

- Automatic placement or spillover ("run in the cloud if the local wait is long").
- Moving a job between local and cloud, or between providers. A job's placement is fixed at submit.
- Restarting or retrying cloud jobs (see [No restarts](#no-restarts)). Running again is a new submit.
- Multi-node jobs. One job is one container, which may have several GPUs (`--gpu H100:8`).
- Arbitrary images or non-uv projects. The first version needs a uv project with a `uv.lock`
  (see [Environment](#environment)).
- The local memory model: pools, slices, the watchdog, cgroup and NVML accounting don't apply in the
  cloud, where a job gets its own GPUs.

## Guardrails

Cloud GPUs cost money, so the guardrails come before anything else. The risk they address is an
agent (or a person) starting paid runs nobody meant to pay for: one too many, a sweep ten times
bigger than intended, or the same failing run over and over. The rule behind all of them: **no paid
attempt starts without a person approving that attempt, at its estimated cost.**

pasar can't be a hard security boundary: agents run as the same user and can reach everything pasar
can. So the true ceiling lives at the provider (item 1), and pasar makes spending by accident hard.

1. **A spending limit at the provider.** Setup requires a workspace spending limit at the provider
   (on Modal, in the workspace's usage settings), set to the most you're willing to lose. It's the
   one limit nothing on this machine can raise. The docs say so, and `pasar cloud` warns if the
   provider exposes the limit and it isn't set.
2. **Off by default.** A cloud target exists only if `config.toml` defines it, and it can't be
   enabled without a `budget`.
3. **Human approval of every attempt.** A cloud job is submitted into **awaiting approval** and does
   nothing (no image build, no GPU) until a person approves it in the web UI, which shows the GPU,
   estimated runtime, estimated cost and the total for everything selected. A sweep can be approved
   in one go. Approval is per attempt: a job the provider interrupted comes back to awaiting approval,
   never straight back to the queue. There is no `pasar approve` command; the guide tells agents that
   approving is for the user alone and that they must never call the approve endpoint. Unapproved
   jobs are cancelled after `approval_ttl` (default 24 h).
4. **Explicit per job.** Only `--on <target>` (or `"target"` in the API) sends a job to the cloud.
   Nothing else (bid change, preemption) changes a job's placement, and cloud jobs can't be
   restarted or retried: every new paid run is a new, explicit submit.
5. **Budget gate.** Even an approved job starts only if the spend so far plus the committed cost of
   running jobs plus this job's estimated cost (`--time` × rate) fits within both `budget.daily` and
   `budget.monthly`. Otherwise it waits as `blocked: budget`. Approval can't override the budget.
6. **Every attempt has an approved maximum, and reaching it pauses the job instead of killing it.**
   An approval covers `--time × timeout_factor` (default 1.5) of run time, capped at `max_runtime`,
   shown as the "at most" cost. At that limit the job checkpoints and pauses (see
   [Run-time limits](#run-time-limits)), even with pasard down.
7. **Concurrency cap.** At most `max_running` cloud jobs per target.
8. **Documentation.** The README, `pasar guide` and `/llms.txt` say plainly: cloud jobs cost money,
   and agents must only use `--on` when the user explicitly asked for cloud compute for that work.
   `pasar submit` prints the estimated cost.
9. **No secret leakage.** The submitter's environment is **not** shipped to the cloud (unlike local
   jobs). Only variables listed in the target's `env_passthrough` or given with `--env KEY` are sent.

## Concepts

| Term | Meaning |
|---|---|
| **Target** | Where a job runs: `local` (the default) or the name of a configured cloud target, e.g. `modal`. Fixed at submit. |
| **Provider** | The code that talks to one cloud API (`modal`, later `runpod`, …). Several targets can use one provider, e.g. two Modal workspaces. |
| **GPU spec** | `--gpu H100`, `--gpu A100-80GB:4`. pasar parses `<type>[:<count>]`, and each provider maps types to its own names. Required for cloud jobs; there is no memory pool to size, so `--mem` is not allowed. |
| **Bundle** | A snapshot of the job's code, taken at submit: every file `git ls-files --cached --others --exclude-standard` lists, plus the uv environment spec. Only a requeue after the provider reclaims the GPU reuses it (see [No restarts](#no-restarts)). |
| **Environment spec** | `pyproject.toml`, `uv.lock`, `.python-version`. Its hash is the image cache key. |
| **Persist dir** | Per-job storage that survives attempts (`$PASAR_PERSIST_DIR`), for checkpoints and outputs. |
| **Wrapper** | `python -m pasar_job.run`, the entry point inside the container. It runs the command and talks to pasard through stdout. |

## Architecture

```
                         pasard
   ┌──────────────────────────────────────────────────────────────┐
   │ scheduler loop                                               │
   │   ├─ local lane: decide() over the memory pool (unchanged)   │
   │   └─ cloud lane per target: bid order, budget, concurrency   │
   │                                                              │
   │ executors: {"local": SystemdExecutor,                        │
   │             "modal": CloudExecutor(ModalProvider), …}        │
   │                                                              │
   │ CloudExecutor (shared, provider-neutral)                     │
   │   bundle builder · image cache · output pump · cost ledger   │
   └───────────────┬──────────────────────────────────────────────┘
                   │ Provider interface (one module per cloud)
                   ▼
        Modal sandbox / RunPod pod / Lambda VM / …
           └─ python -m pasar_job.run  ──stdout──▶ output pump ──▶ jobs/<id>/output.log
                                                              ├──▶ jobs/<id>/events.jsonl
                                                              └──▶ gpu samples (db)
```

The key idea: **a cloud attempt looks exactly like a local one to the rest of pasard.** The output
pump writes `output.log` and `events.jsonl` on local disk, so log following, the events reader,
progress and ETA, lost time, the log-tail diagnosis and the SSE streams all keep working unchanged.
The daemon only learns to pick an executor per job and to skip the local-only steps (memory
measurement, watchdog) for cloud jobs.

### Two layers

**`CloudExecutor`** implements the existing `Executor` protocol (`launch`, `status`, `stop`, `kill`,
`cleanup`, `list_units`) on top of any provider. It owns everything that is the same across clouds:

- building the bundle and the environment spec, and checking the size limit;
- the image cache (environment hash → provider image reference);
- the output pump: one background reader per running attempt that demultiplexes the wrapper's stdout
  into the log, events and telemetry;
- start-up phase tracking and timings;
- the cost ledger: a live estimate from rates, then billed cost when the provider reports it;
- graceful stop: send the wrapper a stop request, wait `grace`, then terminate.

**`Provider`** is the only per-cloud code, kept as narrow as possible:

```python
class Provider(Protocol):
    name: str
    caps: Capabilities          # which optional methods work

    def gpu_types(self) -> list[GpuOffer]
        """Types it can rent, allowed counts, and $/GPU-hour."""
    def prepare_image(self, env: EnvSpec) -> str
        """Build or look up an image for this environment; return a provider image ref."""
    def launch(self, req: CloudLaunch) -> str
        """Start the wrapper in a container with the bundle, volumes, env, GPUs, timeout and
        tags {pasar_job, pasar_attempt}; return the provider's handle."""
    def status(self, handle: str) -> CloudStatus
        """phase (pending | starting | running | exited | gone), exit code, whether the
        provider itself ended it (spot reclaim, host failure), and phase timestamps."""
    def read_output(self, handle: str, cursor: str | None) -> tuple[bytes, str | None]
        """New stdout+stderr bytes since cursor."""
    def request_stop(self, handle: str) -> None
        """Ask the wrapper to stop gracefully (exec, stdin, or a signal)."""
    def terminate(self, handle: str) -> None
    def list(self) -> list[tuple[str, dict[str, str]]]
        """Live handles and their tags, for reconciling after a pasard restart."""
    def persist_volume(self, job_id: int) -> VolumeRef
    def download(self, volume: VolumeRef, path: str, dest: Path) -> None
    def billed_cost(self, handles: list[str], since: float) -> dict[str, float] | None
        """Optional (caps.billing): actual cost per handle, possibly hours late."""
```

`Capabilities` lets the shared layer degrade cleanly. With no billing API, the estimate stays the
only cost. With no way to re-read output after a restart, the pump falls back to the copy the
wrapper tees to the persist volume. With no graceful stop, a stop is a terminate after the grace
period, and preemption loses more work.

### How other clouds would fit

| Provider | Launch | Output | Graceful stop | Billing | Notes |
|---|---|---|---|---|---|
| Modal | `Sandbox.create` with an image from `Image.uv_sync` | sandbox stdout stream | `Sandbox.exec` sends SIGTERM to the wrapper | workspace billing report, per tag | the reference provider |
| Container clouds (RunPod, …) | create a pod from a base CUDA image with the wrapper as its command | log API | API stop with grace, or exec | varies | the environment is built in the container at start, cached on a network volume |
| VM clouds (Lambda, …) | start an instance, then run the wrapper over SSH | the SSH session | SSH `kill -TERM` | usually none; estimate only | a VM has minutes of boot overhead, so a later version could reuse it across jobs |
| SkyPilot bridge | `sky launch` | `sky logs` | `sky cancel` | none | one provider that reaches many clouds, at the cost of a heavy dependency |

These are shapes to check the interface against, not commitments. The interface is shaped around the
lowest common denominator: something that runs one command with some GPUs, an image or base image,
environment variables and a volume, and returns its output. Everything richer is a capability.

## Environment

The bundle and environment spec are provider-neutral. How an image gets built from them is up to the
provider.

- **At submit**, for a cloud job pasar:
  1. finds the git repository containing `--cwd`;
  2. requires `pyproject.toml` and `uv.lock` in the uv project that contains `--cwd`, and checks
     that the lock is universal: it must contain wheels or sdists for `linux x86_64`, not just the
     local aarch64;
  3. writes `jobs/<id>/bundle.tar.zst`: the files git lists (tracked, plus untracked-but-not-ignored),
     with a size limit (`bundle_max`, default 256 MiB) and a clear error naming the largest files if
     it's exceeded. The git commit and diff are recorded as for local jobs.

  Submit fails fast on any of these, so a mistake costs nothing.
- **Image**: base image (configurable, default a CUDA runtime image with uv) + `uv sync --frozen`
  of the environment spec. On Modal this is `Image.uv_sync()`, which Modal caches by content, so the
  first job for a lockfile pays the build (minutes, mostly torch) and later jobs start fast. pasar
  records the image ref per environment hash and shows "building image" as its own start-up phase.
- **In the container**, the bundle is unpacked at `/pasar/work/<repo name>`, the working directory is
  the same path relative to the repository root as locally, and the project's `.venv` is a symlink to
  the image's environment. So `.venv/bin/python train.py` and `uv run train.py` both just work.
- **Frozen at submit.** A cloud job runs the code as it was when it was submitted, even if it waits
  in the queue while you edit. Local jobs, by contrast, run whatever the working tree holds when
  each attempt starts.

Packages that compile from source (flash-attn and the like) can build in the image step with `gpu=`
set. Cases that need more than uv (system packages) get an optional `[tool.pasar.cloud]` table in
`pyproject.toml` later (`apt = [...]`). Until then pasar rejects them rather than guessing.

## Data, checkpoints and outputs

- **`$PASAR_PERSIST_DIR`** is a per-job directory on a provider volume (Modal: a Volume
  `pasar-<target>` mounted at `/pasar/persist`, with a subdirectory per job). It survives attempts,
  so checkpoint-and-resume works exactly as it does locally. `pasar_job.persist_dir()` returns it,
  or a job-local directory when running locally, so one script can serve both.
- **Datasets**: targets can mount named volumes read-mostly (`volumes = {"/data" = "datasets"}`).
  Uploading them is out of pasar's scope; use the provider's CLI.
- **Getting results back**: `pasar pull <id> [path] [--to DIR]` downloads from the persist dir. The
  job detail view lists its files. Persist dirs are deleted with the job's files by the normal
  retention policy (`cloud_retention_days`, default 14), since stored data costs money too.

## The wrapper protocol

The wrapper (`python -m pasar_job.run -- <command>`, in the dependency-free `pasar-job` package that
the bundle's environment already has, or injected if not) is the only thing that runs in the cloud
besides the job. It:

- runs `bash -c <command>` as a child process, passing its stdout and stderr through line by line;
- sets `PASAR_EVENTS` to a FIFO it reads, so `pasar_job.progress()` and `echo … >> $PASAR_EVENTS`
  work unchanged, and re-emits each event as a control line;
- samples `nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu`
  every 5 s and emits a control line;
- on a stop request, or when the attempt reaches its approved run time (the wrapper enforces it
  itself), sends SIGTERM to the job's process group, then SIGKILL after `PASAR_GRACE_SECONDS`, and
  reports which of the two it was;
- tees everything to `$PASAR_PERSIST_DIR/.pasar/attempt-<n>.log`, so the output survives a pasard
  restart even on providers that can't replay it;
- ends with a control line holding the exact exit code or signal, which is more precise than what
  most providers report.

Control lines are single stdout lines prefixed with a per-attempt random token pasard generated
(`\x1epasar:<token> {"t":"event",…}`), so job output can't forge them by accident. Everything else
is job output and goes to `output.log`.

This puts all telemetry, events and exit status on one channel that every provider has, stdout.
Providers don't need an exec API or a way back into pasard, and pasard stays unexposed with no auth.

## Scheduling

Cloud jobs never enter `decide()`, which schedules the local memory pool. Each cloud target has its
own lane, evaluated every tick:

1. Order the target's queued jobs by bid, then queue time, the same as locally.
2. Launch in order while `running < max_running` and the budget check passes for the job at the head
   of the line.
3. A job that doesn't fit the budget blocks the ones behind it (no backfill), so a cheap job can't
   keep taking budget from a larger, higher-bid one. The blocked reason says why (`budget`,
   `concurrency`).

Bids still order each lane. `--preempt` isn't supported for cloud jobs: the lane has no shared
resource to take back, and stopping someone's paid-for run to start another one wastes money.

Clouds preempt too (spot reclaims, host failures). When a provider reports that it ended an attempt,
the attempt ends `preempted` (reason `cloud_preempted`) and the job goes back to **awaiting
approval**, showing how far it got and the estimated cost to finish. If approved, it resumes from its
persist-dir checkpoint using the same snapshot. This is the only way a cloud job gets a second
attempt, and it needs a person's approval and the budget gate like the first.

### Run-time limits

`--time` is usually a guess made on the local GPU, and cloud GPUs can be several times faster or
slower. Killing a job for overrunning a wrong guess would throw away paid work just before it
finishes, so the limit pauses instead, and pasar warns early, using the job's real pace:

1. **Early warning.** Once a running cloud job has reported enough `progress` (default: 5 minutes
   after its first report), pasar projects its total run time the same way the schedule does. If
   that is over the approved run time, the job appears in the approval tray as **needs more time**
   ("#52 is on pace for 2 h 40 m, approved 1 h 30 m: approve $9 more?"). Approving extends the
   attempt's limit; the job keeps running either way.
2. **Pause at the limit.** If no extension came in time, the wrapper sends SIGTERM at the limit and
   the job saves a checkpoint within its grace period. The attempt ends `paused` (reason
   `time_limit`), and the job goes back to **awaiting approval**, showing its progress and the
   estimated cost to finish. Approving resumes it from the checkpoint, like a reclaimed job. The
   only work lost is the restart time.
3. **Backstop.** The provider-side timeout is the approved limit plus the grace period plus a small
   margin, for a container that hangs so badly the wrapper can't act. Only that ends a job with
   `cloud_timeout`.
4. **Calibration.** pasar records each finished cloud job's measured pace against its `--time`, by
   first tag and GPU type (and local jobs' by tag). At submit and in the approval tray it shows a
   calibrated estimate next to the given one ("`lr-sweep` jobs ran 2.8× faster than their `--time`
   on H100"), and the approval's cost uses the calibrated one when there are at least three
   comparable jobs.

A job that never reports `checkpoint` loses its attempt at the limit. The approval tray flags cloud
jobs whose tag has no checkpoint events on record, so their limit can be set generously.

### No restarts

Cloud jobs can't be restarted, and `--retries` is rejected for them. A restart would start paying
for another run with whatever code is in the working tree now, often without anyone looking closely
at the cost again, which is too easy to do by accident. To run again, submit a new job: the working
tree is snapshotted fresh, the estimated cost is shown again, and the budget is checked again.

- `pasar restart` and `POST /api/jobs/{id}/restart` return an error for cloud jobs, which gives the
  equivalent `pasar submit` command.
- The UI hides **restart** and **restart…** on cloud jobs and shows **copy submit command** instead.
- A new submit gets a new persist dir. To continue from an earlier job's checkpoint, pass
  `--resume-from <id>`: the new job's persist dir starts as a copy of the old one (on Modal, copied
  within the Volume, so nothing is downloaded).

## Job lifecycle and records

Cloud jobs add one state, **awaiting**, before `queued`:

```
submit ──▶ awaiting ──(approved in the UI)──▶ queued ──▶ running ──▶ completed / failed
              │  ▲                                          │
              │  └── provider reclaimed the GPU, or paused ──┘
              │      at its approved run time
              └──(rejected, cancelled, or approval_ttl passed)──▶ cancelled
```

Local jobs never enter it. A running cloud attempt has a **phase**, shown in the UI and in
`pasar ls`: `packaging → building image → waiting for GPU → starting → running`. Each phase's
timestamp is stored, which gives the startup breakdown.

Additions:

- `JobSpec`: `target: str = "local"`, `gpu: str | None`, `env_keys: list[str]`.
- `attempts`: `unit` holds the provider handle for cloud attempts. A new `cloud` JSON column holds the
  phase timestamps, GPU type and count actually assigned, region, image ref, console URL, estimated
  cost and billed cost.
- New table `gpu_samples(job, attempt, ts, gpu, util, mem_used, mem_total, power, temp)`, downsampled
  after the attempt ends. `metric_summaries` gets the same summaries (avg and max power, energy, max
  temperature) as locally, computed from these instead of Prometheus.
- New table `cloud_spend(target, day, estimated, billed)` for the budget gate and the dashboard.
- New table `approvals(job, attempt, time, estimated_cost, max_cost)`: who approved which attempt at
  what price, for the record.

New end reasons: `image_build_error`, `no_capacity`, `time_limit` (paused at the approved run time;
the attempt's end kind is `paused`, which counts toward lost time like a preemption),
`cloud_timeout` (the provider's backstop),
`cloud_error` (the provider failed the container), `cloud_preempted`, `budget` (a job over its own
`--max-cost` is stopped). The existing `gpu_oom`, `signal` and `exit` reasons come from the log scan
and the wrapper's exit line as usual. `kernel_oom` and `gpu_xid` don't apply.

Reconciling after a pasard restart: `list_units()` returns every live handle tagged with a pasar job.
Handles no job owns are reported as `stray_unit`, and stray cloud units are **terminated** after a
warning, since unlike local ones they cost money.

## CLI and API

```
pasar submit --on modal --gpu H100[:N] --time 2h [--env KEY]… [--max-cost 20] -- <command>
pasar pull <id> [path] [--to DIR]
pasar submit --on modal … --resume-from <id> -- <command>   # continue an earlier cloud job's checkpoints
pasar cloud                         # targets, budget, spend today/this month, awaiting approval, rates
```

`pasar submit --on …` returns right away with the job awaiting approval and prints the estimated
and maximum cost plus the UI link. `pasar wait` keeps waiting through `awaiting`.

`--json` works everywhere, as before. `POST /api/jobs` accepts `target`, `gpu`, `env_keys` and
`max_cost`. `POST /api/jobs/{id}/approve` and `POST /api/jobs/{id}/reject` exist for the web UI
only; they are left out of the CLI and the agent guide says never to call them. `GET /api/cloud` returns the targets, spend and rates. Job views include a `cloud` object
for cloud jobs.

## Web UI

- **Cloud timeline**: its own chart under the local one, shown only when a target is configured. One
  row per cloud job, time on the horizontal axis, colours by first tag as elsewhere. Each bar is split
  by phase: a pale segment for building and waiting for a GPU, a solid one for running.
- **Approval tray**: when any job is awaiting approval, a banner at the top of the dashboard
  ("3 cloud jobs waiting for you · est. $14, at most $21"). It opens a list with checkboxes showing
  each job's submitter, note, command, GPU, time, estimated and maximum cost, and the job's git diff
  summary, with **approve selected** and **reject selected**. The selection total and the
  remaining budget are shown next to the button. The same tray lists running jobs that
  **need more time** and paused jobs waiting to resume, each with its progress and cost to finish,
  and shows calibrated estimates next to the given ones.
- **Tiles**: running cloud jobs, spend today and this month against the budget (small meters),
  current burn rate in $/h.
- **Cloud job detail** (specialised; the local memory and pool panels are hidden):
  - **Cost**: estimated so far and final projection (from rate × ETA), then billed when known, and
    `--max-cost` if set.
  - **Startup**: the phase breakdown as a small stacked bar (build, wait for GPU, start, until the
    job's first `resumed` or `progress` event).
  - **Hardware**: GPU type × count, region, image and environment hash, a link to the provider console.
  - **Per-GPU charts**: utilisation, memory, power and temperature per GPU. These are exact, because the
    job owns its GPUs, unlike the GPU-wide local charts.
  - **Files**: the persist dir listing, with the `pasar pull` command to copy.
  - Logs, events, metrics, progress, attempts and lost time as for local jobs.
- The mascot gets no new states. Cloud jobs reuse `start`, `done`, `failed`, `preempted`.

## Configuration

```toml
[clouds.modal]
provider = "modal"                 # the SDK reads credentials from ~/.modal.toml or env
budget = { daily = 50.0, monthly = 300.0 }   # USD; required to enable the target
approval_ttl = "24h"
max_running = 4
timeout_factor = 1.5
max_runtime = "24h"
env_passthrough = ["WANDB_API_KEY", "HF_TOKEN"]
volumes = { "/data" = "datasets" }
bundle_max = "256MiB"
# base_image = "nvidia/cuda:12.8.1-runtime-ubuntu24.04"
# rates = { H100 = 3.95 }          # override $/GPU-hour if the provider can't report rates
```

Provider SDKs are optional extras (`uv tool install 'pasar[modal]'`), imported only when a target
uses them, so a local-only install stays as it is.

## Testing

- **A fake provider** (in-process, running the wrapper as a subprocess) exercises all the shared
  machinery: bundle, image cache, output pump and control lines, graceful stop, phase timings, budget
  gate, hard timeout, provider preemption, reconcile and stray termination. The daemon and lane tests
  use it the way the local scheduler uses the fake executor.
- **Provider contract tests**: one parametrised suite every provider must pass, run against the fake
  in CI and against a real provider only when opted in (`PASAR_TEST_MODAL=1`), with a tiny CPU-only
  job so it costs cents.
- The wrapper and bundle builder are unit tested on their own (control-line escaping, FIFO events,
  `.gitignore` handling, size limit, lockfile platform check).

## Implementation order

1. Provider-neutral pieces with the fake provider: `JobSpec.target`, executor per target, the cloud
   lane and budget, the bundle builder, the wrapper and output pump.
2. `ModalProvider`, guardrails, and the docs (README, guide).
3. CLI (`--on`, `--gpu`, `pull`, `cloud`) and API fields.
4. UI: the cloud timeline, tiles, and the cloud job detail view.
5. Billing reconciliation and `--max-cost`.

## Open questions

- Per-submitter budgets?
- Notify on new approvals (phone push, email) so burst jobs don't sit waiting for someone to open
  the dashboard? This ties in with the planned Alertmanager notifications.
- Could approval get a real barrier (for example, the UI session holds a secret the CLI and
  agents never see)? It would only help if agents can't read the secret's file, which is hard when
  they run as the same user. The provider spending limit is the actual boundary.
- Modal: does a new stdout reader replay a sandbox's output from the start? If it does, the pump can
  resume after a pasard restart without the persist-dir tee. The design works either way.
- Is keeping VMs warm across jobs (for VM clouds) worth the complexity? Deferred until such a
  provider exists.
