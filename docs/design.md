# pasar design

pasar is a GPU job scheduler for a single machine. Jobs carry a **bid** that sets their place in the queue, jobs that ask to can **preempt** lower-bid ones, jobs can take the **whole GPU or a slice of its memory**, and a web UI shows what is running, what is waiting, and why.

It is built for a small, cooperative group (people and the agents they steer) sharing one GPU box. People are expected to follow pasar's conventions: estimate memory and runtime, checkpoint regularly, report checkpoint events.

## Goals

- Make "what's running, what's next, and why" obvious at a glance, on desktop and phone.
- Let important work jump the queue by bidding higher, allow preemption only when asked for, and make it cheap by standardising checkpoint and resume.
- Share one GPU between several memory-bounded jobs safely.
- Be easy for agents to drive: every command has JSON output and stable exit codes.
- Record enough about each job (command, env, git state, logs, metrics, lost time) to understand it later.

## Non-goals (for now)

- Multiple GPUs or multiple nodes, gang scheduling. (Explicit, opt-in burst jobs on rented cloud GPUs are proposed in [cloud.md](cloud.md).)
- Authentication, users, quotas, accounting. Everyone on the box is trusted.
- Slurm compatibility. Ideas are borrowed where they fit.
- Containers. The command itself picks its environment (e.g. `.venv/bin/python train.py`).

## Concepts

| Term | Meaning |
|---|---|
| **Job** | A command with scheduling metadata. Identified by an integer ID (`42`) for its whole life. |
| **Attempt** | One launch of a job. Preemptions, retries and restarts create new attempts of the same job. |
| **Bid** | An integer priority, default **1000**. Bids are free and only order the queue: higher bids go first. |
| **Preempt** | A job submitted with `--preempt` may stop running jobs with a lower bid to start now. Never carried over: it is asked for on each submit, bid change or restart. |
| **Whole-GPU job** | The default. Takes the entire memory pool and runs alone. |
| **Shared job** | Requests an estimated amount of memory (`--mem 24G`) and can run alongside other shared jobs. |
| **Preemptible** | Default yes. A preemptible job can be stopped (SIGTERM, grace period, SIGKILL) to make room for a higher-bid job that asked to preempt, then requeued. |
| **Estimated runtime** | Required on submit. Used for ordering decisions and projected start/finish times. Jobs that overrun are not killed. |

## Architecture

```
 agents / people                     browser
   │  pasar CLI (--json)               │  web UI (static assets served by pasard)
   └──────────────┬────────────────────┘
                  ▼  HTTP (loopback by default, optionally a tailnet address)
        ┌──────────────────────────────┐
        │ pasard (one Python process)  │
        │  • FastAPI: REST + SSE       │───▶ Prometheus: GPU-wide metrics
        │  • scheduler loop            │───▶ NVML: per-process GPU memory
        │  • memory watchdog           │
        │  • SQLite                    │
        └──────────────┬───────────────┘
                       ▼  Executor interface (systemd backend)
      transient systemd user units: pasar-job-<id>-<attempt>
                       │
     job process ──appends──▶ jobs/<id>/events.jsonl
                 ──stdout/err──▶ jobs/<id>/output.log
```

- **pasard** is the single daemon: HTTP API, scheduler loop, watchdog, and storage. It runs as a systemd user service (`pasard.service`) with lingering enabled so it starts on boot.
- **Executor** is a small interface (`launch`, `status`, `stop`, `kill`, `cleanup`, `list_units`). The first backend uses transient systemd user units. The scheduler never talks to systemd directly, so it can be unit-tested with a fake executor and ported later.
- **Jobs survive pasard restarts.** Each attempt is its own systemd unit that writes its log straight to a file. On startup pasard lists `pasar-job-*` units and reconciles them with the database.
- **pasar CLI** is a thin HTTP client.
- **pasar-job** is a separate, dependency-free Python package installed into job venvs. It provides the job-side protocol helpers.
- **Web UI** is a Svelte + Vite app built to static files and served by pasard.

### Stack

Python ≥ 3.11 (FastAPI, uvicorn, pydantic, nvidia-ml-py, httpx), SQLite (WAL mode), Svelte 5 + Vite, managed with `uv`.

### Launching an attempt

pasard writes `jobs/<id>/launch.json` (mode 0600: command, working directory, full environment
including `PASAR_*`) and runs roughly:

```
systemd-run --user --quiet --unit=pasar-job-42-3 \
  -p StandardOutput=append:<jobdir>/output.log -p StandardError=append:<jobdir>/output.log \
  -p RemainAfterExit=yes -p KillMode=control-group -p KillSignal=SIGTERM \
  -p TimeoutStopSec=<grace> -p MemorySwapMax=0 -p MemoryMax=<pool> \
  <python> -m pasar.launch <jobdir>
```

`pasar.launch` changes to the working directory and execs `bash -c '<command>'` with exactly
that environment, so no quoting rules of systemd environment files apply.

- Stopping a unit sends SIGTERM to every process in its cgroup, waits for the grace period, then sends SIGKILL, which covers dataloader workers and other children.
- Before each launch pasard appends a separator line to `output.log` (`──── attempt 3 · 2026-09-21 14:02 · resumed after preemption ────`).

## Memory model

The target host is an NVIDIA GB10 with **unified memory**: CPU and GPU share one ~121 GiB pool, and NVIDIA tools report no separate GPU memory total. We measured on this box that:

- CUDA allocations are **not** charged to the process's cgroup. A 4 GiB `torch` allocation left `memory.current` at 0.36 GiB, and `MemoryMax=3G` did not stop it.
- NVML **does** report per-process GPU memory (4292 MiB for that process).

So pasar accounts for memory itself.

- **Pool** = total RAM − `system_reserve` (default **16 GiB**, for the OS, monitoring, and interactive use).
- **External usage.** GPU memory used by processes that pasar did not launch (NVML) is subtracted from the free pool, so pasar never launches into memory that isn't really free.
- **Whole-GPU job**: for scheduling and the watchdog, it is charged the pool **minus external GPU usage**, so a foreign GPU process pasar didn't launch can't block it forever. Its cgroup `MemoryMax`, set once at launch, is the full pool regardless, since shrinking or growing that cap as foreign usage comes and goes would be unstable.
- **Shared job** requesting *R*: reserves **R + max(2 GiB, 10% of R)** to cover the CUDA context, fragmentation and caching-allocator slack. This reservation is the job's **limit**.
- **Job usage** = cgroup `memory.current` + the NVML GPU memory of every PID in the job's cgroup. Peak usage is tracked per attempt.
- For scheduling, each running job counts as `max(reservation, actual usage)`.
- The limit is not a hard cap. Every local job, shared or whole-GPU, gets the same cgroup `MemoryMax`: the full pool, set once at launch, a machine-safety backstop for CPU-side memory only. A shared job that runs past its limit while the machine has room keeps running; only the watchdog below stops it, and only under sustained pressure.

### Watchdog

Every 2 s pasard measures every job and the machine.

- **Over limit, no pressure**: the job keeps running and is flagged `over limit` in the UI and the job record.
- **Machine under memory pressure**: `/proc/pressure/memory` `some avg10 > 10%` **or** `MemAvailable < 5%` of total, sustained for **30 s**.
- **Over limit and under sustained pressure**: SIGKILL the job that is furthest over its limit in GiB, with reason `oom` (e.g. "exceeded 22 GiB limit (peak 31 GiB) during memory pressure"). Wait another 30 s before choosing another victim. Preemptible or not does not matter here.
- **Pressure with no job over its limit**: pasar takes no action (the kernel handles it) and records a machine event that the UI shows.

Jobs can opt into a softer in-process guard. pasard exports `PASAR_MEM_LIMIT_BYTES`, and `pasar_job.apply_memory_limit()` calls `torch.cuda.set_per_process_memory_fraction` accordingly, so PyTorch raises a catchable `OutOfMemoryError` instead of the job being killed from outside.

## Scheduling

The scheduler runs every 2 s and immediately after any submit, cancel, bid change, job exit, or watchdog kill.

1. **Order** queued jobs by bid (highest first), then by queue time (earliest first). Preempted and auto-retried jobs keep their original queue time. A manual restart gets a new queue time.
2. For each queued job in order:
   - **Fits in free memory** → launch it.
   - **Doesn't fit, and the job asked to `--preempt`** → collect running jobs that are preemptible **and** have a strictly lower bid. Choose victims from the lowest bid up (among equal bids, most recently started first, since it has the least work to lose) until enough memory would be freed. If that is enough, send SIGTERM to the victims. Their memory still counts as used until they exit. The job launches on a later pass once the memory is actually free.
   - **Still doesn't fit** → the job is **blocked**. The first blocked job gets a **reservation**: the time enough running jobs are projected to have ended for it to fit (from their progress, else their `--time`), and how much memory will be spare then.
3. **Backfill**: jobs further down the queue still start in free memory, but only if that can't delay the reservation: they fit in the spare memory beside the reserved job, or they are projected to finish before its reserved start. A job with no usable estimate only takes the first route. This keeps a large high-bid job from being starved by a stream of small ones without preempting anything.

Consequences:

- Nothing is preempted unless a queued job asked to. Equal bids never preempt each other.
- Raising a job's bid takes effect on the next pass and moves it up the queue; with `--preempt` it can also stop lower-bid jobs. Lowering a running job's bid can make it a victim of a job that asked to preempt.
- A job that overruns its estimate can delay a reservation it was projected to finish before; the reserved job still starts as soon as the memory is free.
- Time run before a preemption counts toward the job's estimated runtime. Remaining time = estimate − total run time so far.

### Projected schedule

For the UI and `pasar ls`, pasard simulates the same rules forward in time, using each job's remaining time (at least 1 minute for jobs that have overrun). For a running job that reports `progress` with `total_steps`, that is extrapolated from its pace: its attempt took `elapsed` (from the attempt's start to the latest report, so startup is amortised) for `done` steps (since the step it resumed from: the `resumed` step, else the last earlier checkpoint, else its first report), so the remaining `total − step` steps take `elapsed × (total − step) ÷ done`, counted down from the latest report. Otherwise it is `--time` minus the time already run. Job views say which (`eta_source`), and `expected_runtime` is run time plus remaining. The result is a projected start and finish time per job and the data for the timeline chart. Projections are labelled as estimates.

## Job lifecycle

```
queued ──▶ running ──▶ completed
  ▲          │  └────▶ failed ──(auto-retry / manual restart)──▶ queued
  │          ▼
  └──── stopping ────▶ cancelled
     (preempted: stopping → queued)
```

`stopping` covers the grace period of a preemption or a cancellation. Preempted, oom and similar outcomes are **reasons** attached to the state and to the attempt, not separate states.

### Why a job ended

Determined in this order:

1. **pasar's own actions** are known exactly and short-circuit everything else: `cancelled`, `preempted`, `oom` (watchdog).
2. Otherwise, a specific cause found in the logs or the GPU beats a generic signal or exit code:
   1. **`oom-kill` by the kernel** within the cgroup (from systemd's `Result`) → `kernel_oom`.
   2. **Log scan** of the last lines of `output.log`, most recent match first: `torch.OutOfMemoryError` / `CUDA out of memory` → `gpu_oom`.
   3. **GPU faults**: Xid errors in the kernel log during the attempt → `gpu_xid`.
   4. **Signal** (`SIGSEGV`, `SIGABRT`, …) → `signal`.
   5. Otherwise → `exit`, with the log scanned again for the final Python exception line or an NCCL error to use as the summary; failing that, "exited with code N".

**Planned, not yet implemented**: a log scan for a bare `Killed` line (printed when the kernel OOM-kills a process from *outside* the job's own cgroup, e.g. under whole-machine memory pressure — distinct from the `oom-kill` `Result` systemd reports for the cgroup itself) is planned but not wired up yet. Until it lands, such a case surfaces as `signal` or a generic `exit`, not as an OOM reason.

Each attempt stores a **reason code**, a one-line **summary**, and the **last 50 log lines**.

### Retries and restarts

- `--retries N` (default **0**) retries automatically after failures: non-zero exit, signal, `oom`, `gpu_oom`, `gpu_xid`. Cancellations are never retried. Preemptions do not use up retries.
- **Restart** (`pasar restart <id>`, UI button) requeues a finished job as a new attempt with the same ID, command and working directory, so checkpoint resume works. `pasar restart <id> --mem 48G --bid 1500 --time 3h` changes settings first; `--preempt` must be given again if wanted.

## Job protocol

### Environment given to every attempt

| Variable | Meaning |
|---|---|
| `PASAR_JOB_ID` | Job ID |
| `PASAR_ATTEMPT` | Attempt number, starting at 1 |
| `PASAR_RESUMING` | `1` if any earlier attempt ran |
| `PASAR_EVENTS` | Path to append protocol events to |
| `PASAR_JOB_DIR` | The job's directory (scratch space is fine) |
| `PASAR_MEM_LIMIT_BYTES` | Shared jobs only: the job's limit |
| `PASAR_GRACE_SECONDS` | Time between SIGTERM and SIGKILL |

On top of this the job gets the submitter's environment at submit time (unless `--no-env`), stored in `jobs/<id>/env.json` with mode 0600.

### Events

Jobs append one JSON object per line to `$PASAR_EVENTS`. pasard adds timestamps when it reads them, so job clocks don't matter.

```json
{"event": "checkpoint", "step": 1200}
{"event": "resumed", "step": 1200}
{"event": "progress", "step": 1350, "total_steps": 10000, "loss": 1.84}
{"event": "note", "text": "switched to lr 1e-5"}
```

A `progress` event may carry any extra numeric fields beyond `step`/`total_steps` (like `loss` above); the UI charts each one as its own line on the job's Metrics tab.

Any language works (`echo '{"event":"checkpoint"}' >> "$PASAR_EVENTS"`). The `pasar_job` helper wraps this:

```python
import pasar_job

pasar_job.apply_memory_limit()          # optional, shared jobs
pasar_job.on_preempt(save_checkpoint)   # run on SIGTERM, within the grace period
if pasar_job.resuming():
    step = load_checkpoint()
    pasar_job.resumed(step)
for step in range(start, total):
    ...
    pasar_job.progress(step, total, loss=loss)
    if step % 500 == 0:
        save_checkpoint()
        pasar_job.checkpoint(step)
```

**Conventions for job authors** (these go in the user docs): make commands resumable (detect an existing checkpoint and continue), checkpoint at least every ~30 minutes, save on SIGTERM when possible, report `checkpoint` and `resumed`.

### Lost time

For each interrupted attempt (preempted or failed), and the attempt that follows it:

- **Wasted work** = interruption time − last `checkpoint` time in that attempt. With no checkpoint event, the whole attempt.
- **Restart cost** = time of `resumed` in the next attempt − that attempt's start.
- Lost time = wasted work + restart cost, reported separately for preemptions and failures, per job and machine-wide. Queue wait is shown separately and is not counted as lost compute.
- Jobs that never send events show lost time as unknown.

## CLI

```
pasar submit [opts] -- <command…>    pasar logs <id> [-f] [--attempt N]
pasar ls [--all] [--state S]         pasar cancel <id>
pasar show <id>                      pasar bid <id> <n> [--preempt]
pasar wait <id> [--timeout T]        pasar restart <id> [--mem/--bid/--time …]
pasar status
```

`logs --attempt N` (viewing a single attempt's slice of the log, rather than the whole file with
its separator lines between attempts) is **planned, not yet implemented**.

Submit options:

| Option | Default |
|---|---|
| `--time 2h30m` (estimated runtime) | required |
| `--mem 24G` (makes it a shared job) | whole GPU |
| `--bid N` | 1000 |
| `--preempt` (may stop lower-bid jobs to start now) | only queue |
| `--non-preemptible` | preemptible |
| `--grace 120s` | 120 s |
| `--retries N` | 0 |
| `--name`, `--note`, `--tag` (repeatable), `--by` | name derived from the command, `--by` defaults to `$USER` |
| `--cwd DIR` | current directory |
| `--no-env` | environment captured |

Git commit and uncommitted diff of the working directory are recorded automatically.

Every command accepts `--json`. `pasar wait` exit codes: `0` completed, `1` failed, `2` oom, `3` cancelled, `4` timeout. Codes 64 and above are CLI errors (bad arguments, daemon unreachable).

`pasar guide` prints a self-contained agent-facing guide (packaged as `src/pasar/agents.md`, shipped in the wheel) — it works without a daemon running. `pasar guide cloud` prints the cloud GPU topic (`src/pasar/agents-cloud.md`), also served at `/llms-cloud.txt`.

## HTTP API

REST under `/api`, JSON in and out:

- `POST /api/jobs`, `GET /api/jobs` (`?since=&until=` lists jobs with an attempt in that window, for browsing history), `GET /api/jobs/{id}`
- `POST /api/jobs/{id}/cancel`, `PATCH /api/jobs/{id}` (`bid` and/or `preempt`; patching other queued-job settings is **planned, not yet implemented**), `POST /api/jobs/{id}/restart`
- `GET /api/jobs/{id}/logs` (range, or SSE with `?follow=1`)
- `GET /api/jobs/{id}/events`, `GET /api/jobs/{id}/metrics`
- `GET /api/jobs/{id}/usage`: memory history (cgroup + NVML) of the job's current attempt, in memory only (not persisted, empty after a pasard restart)
- `GET /api/status` (pool, reserved/free memory, pressure, blocked/waiting queue state, recent machine events)
- `GET /api/gpu` (power, temperature, utilisation time series from Prometheus, for the dashboard)
- `GET /api/mascot` (a manifest mapping each state to the URL(s) to use for it: custom images from `mascot_dir` if any exist for that state, else the built-in one, else none), `GET /mascot/<file>` (serves a custom image from `mascot_dir` only, 404 otherwise), `GET /mascot/builtin/<file>` (serves a built-in image only)
- `GET /api/stream`: a single SSE stream of state changes that keeps the UI live
- `GET /llms.txt`: the same agent guide as `pasar guide`, as plain text

Job views (in `GET /api/jobs`, `GET /api/jobs/{id}` and the `stream` SSE payload) carry a `spans` list, one `[start_time, end_time, end_kind]` triple per attempt, so the UI can draw each attempt's timeline bar without a separate request.

pasard binds to `127.0.0.1:8750` by default. `bind` in the config can add more addresses, e.g. a Tailscale IP. There is no authentication, so it should never bind to a public interface. pasard also checks the request's `Host` header against an allowlist (bound addresses, localhost, plus `allowed_hosts` in the config) to guard against DNS rebinding.

## Storage

```
~/.config/pasar/config.toml         settings
~/.config/pasar/mascot/             optional custom mascot images
~/.local/share/pasar/pasar.db       SQLite
~/.local/share/pasar/jobs/<id>/
    spec.json   env.json   launch.json   git.diff   output.log   events.jsonl
```

Tables:

- `jobs`: spec (command, cwd, name, note, tags, submitter, bid, mode, mem request, reservation, estimated runtime, preempt, preemptible, grace, retries), state, reason, queue time, git commit.
- `attempts`: job, number, unit name, start and end time, end kind, exit code or signal, reason and summary, log tail, peak memory, wasted work, restart cost.
- `events`: job, attempt, time, kind, step, payload.
- `metric_summaries`: job, attempt, metric, avg, max, total (e.g. energy).
- `machine_events`: pressure episodes, watchdog kills; external GPU usage changes as a machine event is **planned, not yet implemented**.

Old job directories are cleaned up by a size and age policy (default: keep 30 days or 20 GiB of logs).

## Metrics

- **Per job**, from pasar itself: memory (cgroup + NVML) current and peak, progress, checkpoints.
- **GPU-wide**, from Prometheus (`prometheus_url`, e.g. `http://127.0.0.1:9090`), via dcgm-exporter: power, temperature, utilisation, energy. While a job runs the UI queries these live. When an attempt ends, pasard stores summaries (average and max power, energy used, max temperature) so they outlive Prometheus retention. The job page links to the matching time range in Grafana if `grafana_url` is set. **Planned, not yet implemented**: SM clock (also dcgm-exporter), and memory pressure / host stats from node-exporter.
- GPU-wide metrics cannot be split between jobs that share the GPU. The UI labels them as GPU-wide and shows which jobs were co-running.
- Without Prometheus configured, these panels are hidden and everything else works.

## Web UI

- Built from a Svelte + Vite app in `web/` (`npm ci && npm run build`) into static assets in `src/pasar/webui/`, served by pasard. It's a single-page app with two client-side routes, `/` (dashboard) and `/jobs/<id>` (job detail); pasard serves `index.html` for both so deep links and reloads work.
- **Dashboard**: a header with the mascot and a one-line status ("2 jobs running, 2 waiting · next up #47 ~16:40"), stat tiles (memory pool, power, temperature, utilisation), then a **timeline** (memory on the vertical axis, time on the horizontal) of running jobs and the projected schedule of queued jobs; drag it, press A/D, or use its range buttons (6h, 1d, 1w, 30d) to look back at older jobs, and W/S or ctrl + scroll to zoom, then the **job table** (running, stopping, queued, recently finished).
- **Job colours**: a job's colour comes from its first tag, so a sweep or experiment reads as one group; tags on screen together get distinct colours (oldest tag first) until there are more tags than the ten colours. Untagged jobs are coloured by ID. Finished jobs keep a muted tint of their colour.
- **Job detail**: on desktop, a slide-over panel on the right with the dashboard dimmed behind it. On phones, a full page with tabs (Overview, Logs, Metrics, Events). The URL is the same (`/jobs/42`) either way. Contents: state and reason, bid (editable), cancel/restart, elapsed vs estimated time, progress, last checkpoint, memory (current, peak, limit), lost time, an attempts bar (run, lost, waiting), metric charts, live log, event timeline, command, environment and git info.
- **Failed jobs** show the reason prominently (category, summary, the last log lines) with **restart** and **restart…** (a form pre-filled with the job's settings).
- **Phone layout**: the table becomes a list of cards, the timeline is shorter, and actions sit in a bottom bar.
- **Look**: light theme by default, soft rounded type, pastel colours, rounded cards with soft shadows, big numbers for key stats, subtle motion (a bounce when a job starts, a small celebration when one finishes). Motion respects `prefers-reduced-motion`. Status colours stay consistent (green running, lavender queued, amber stopping or over limit, red failed).

### Mascot

A mascot in the header reflects the machine's state, and small versions appear in toasts and empty states. States and image names:

| Image | Shown when |
|---|---|
| `idle` | nothing running or queued |
| `happy` | jobs running normally |
| `start` | a job just started (toast) |
| `busy` | pool full and jobs waiting |
| `waiting` | viewing a queued job |
| `sweat` | memory pressure or a job over its limit |
| `hot` | GPU temperature above `hot_temp_c` (default 85 °C) |
| `oom` | a job was killed for memory |
| `failed` | a job crashed |
| `preempted` | a job was preempted |
| `done` | a job finished |
| `thinking` | loading |
| `hmm` | empty search results, empty filters |
| `confused` | a job ended with an unknown or `lost` reason |

pasar ships a built-in set of PNG sprites (`src/pasar/mascot/`). Users can point `mascot_dir` (default `~/.config/pasar/mascot/`) at their own PNGs using these names. Variants are allowed: `done.png`, `done-2.png`, … and the UI picks one at random each time the state is shown. Missing images fall back to the built-in set.

## Configuration

`~/.config/pasar/config.toml`, all optional:

| Key | Default |
|---|---|
| `bind` | `["127.0.0.1:8750"]` |
| `allowed_hosts` | `[]` (extra Host-header names to accept, e.g. `["mybox.example.ts.net"]`; localhost, 127.0.0.1, `::1` and the bound addresses are always accepted) |
| `system_reserve` | `16GiB` |
| `mem_margin_min` / `mem_margin_frac` | `2GiB` / `0.10` |
| `default_bid` | `1000` |
| `default_grace` | `120s` |
| `tick` | `2s` |
| `pressure_some_avg10` / `pressure_mem_available_frac` / `pressure_sustain` | `0.10` / `0.05` / `30s` |
| `prometheus_url`, `grafana_url` | unset |
| `hot_temp_c` | `85` |
| `log_retention_days` / `log_retention_size` | `30` / `20GiB` (pasar's own `jobs/<id>/` files only; pulled cloud results are never counted or deleted) |
| `pull_dir` / `pull_min_free` / `pull_max` / `cloud_retention_days` | `<data_dir>/pulls` / `20GiB` / `0` (no limit) / `3` — where finished cloud jobs' results land and how long a provider keeps them; see [cloud.md](cloud.md#getting-results-back-and-when-they-are-deleted) |
| `mascot_dir` | `~/.config/pasar/mascot/` |

The `PASAR_ADDRESS` environment variable, when set, replaces the default `127.0.0.1:8750` as the
first address pasard binds to (`bind` entries still follow), so a throwaway daemon can run next to
a real pasard on the default port; the CLI's `PASAR_URL` is unaffected and still selects which
server it talks to.

## Failure handling

- **pasard restarts or crashes**: jobs keep running in their units. On startup pasard reconciles: units that finished while it was down get their results read and recorded. Jobs whose unit vanished (e.g. reboot) are marked failed with reason `lost` and are eligible for retry.
- **Reboot**: queued jobs remain queued. Running jobs become `lost` as above.
- **Launch failure** (bad cwd, systemd error): the attempt fails with reason `launch_error` and the error text.
- **Malformed events** are ignored and logged as warnings. They never crash pasard.
- **NVML or Prometheus unavailable**: pasard keeps scheduling on reservations alone, and the UI shows a warning.

## Testing

- **Scheduler and watchdog**: pure logic, tested against a fake executor, fake clock, and fake memory readings. Table-driven cases for ordering, preemption victim choice, blocking, reservations and backfill, pressure kills, retries, and lost-time accounting.
- **Protocol**: `pasar_job` helpers and event parsing, including malformed input.
- **systemd backend**: integration tests using real transient units running `sleep` and small scripts (launch, SIGTERM grace, SIGKILL, exit codes, recovery after a pasard restart). Marked so they only run on a systemd host.
- **GPU**: an opt-in test that allocates GPU memory to check NVML accounting and the watchdog.
- **API and CLI**: FastAPI test client, and CLI tests against a running test daemon.
- **Web UI**: component tests (`npm test`) plus a Playwright smoke test (`npm run e2e`) of the dashboard, job panel and phone layout, run against a throwaway `pasard` with its own port and XDG dirs. Needs a systemd user session, so it isn't part of CI.
- CI runs everything that doesn't need systemd or a GPU.

## Later

- Cloud jobs on rented GPUs (Modal first, other providers behind the same interface): see [cloud.md](cloud.md).
- Multiple GPUs and nodes.
- Job dependencies (run the eval after the training job).
- Notifications through Alertmanager, pausing launches while `GpuHot` fires.
- A dark theme.
- Head-and-shoulders mascot crops for very small sizes.
