# pasar: agent guide

pasar is a job scheduler for one GPU machine (an NVIDIA GB10 with unified CPU/GPU memory). Jobs
carry a **bid** (an integer priority, default **1000**); bids are free and set the queue order:
higher bids go first. A job may also **preempt** (stop) lower-bid running jobs to run now, but
only if it asks to with `--preempt`. A job takes the **whole GPU** by default (recommended), or a
**memory slice** (`--mem`) to share the box with other jobs when it suits sharing. Every job is a real systemd unit, so it
survives a pasard restart and can be inspected with normal Linux tools. This guide matches the
pasar version installed on this machine; run `pasar guide` any time to reprint it, or fetch it
over HTTP at `/llms.txt` (see below).

## Connecting

The `pasar` CLI talks to `http://127.0.0.1:8750` by default. Set `PASAR_URL` to point it at a
different address (e.g. a Tailscale hostname) if the daemon isn't local to you:

    export PASAR_URL=http://mybox.example.ts.net:8750

Check the daemon is up and see the memory pool:

    $ pasar status
    pool      42.0 GiB reserved of 96.0 GiB, 54.0 GiB free
    external  0.0 GiB used outside pasar
    memory    88.2 GiB available, pressure 0.3%

If pasard is unreachable, every CLI command exits **69** and prints an error to stderr instead of
hanging. The web UI lives at the same address as the API (`http://127.0.0.1:8750/`, or
`/jobs/<id>` for one job's detail page) — open it in a browser if you want to look, but nothing
in this guide requires it.

## Submitting a job

    pasar submit [options] -- <command...>

The `--` is required: everything after it is passed to the shell verbatim, including its own
flags. Give the full environment invocation — pasar does not activate anything for you:

    pasar submit --time 2h --tag lr-sweep --note "lr sweep point 3" \
      -- .venv/bin/python train.py --lr 3e-5

Options:

| Flag | Meaning | Default |
|---|---|---|
| `--time DURATION` | **Required.** Estimated runtime, e.g. `2h30m`, `90m`, `45s`. Used to order the queue and project start times. Overrunning it is fine — the job is not killed for taking longer. | — |
| `--mem SIZE` | Reserve a memory slice (e.g. `24G`) so this job can share the GPU with others — only for jobs that suit sharing (see below). pasar adds a safety margin on top (`max(2 GiB, 10%)`) to cover CUDA context and allocator slack — this is a **unified-memory** box, so ordinary CUDA allocations count against the same pool as everything else. | whole GPU (recommended) |
| `--bid N` | Priority: higher bids start first. On its own a bid never stops a running job. | `1000` |
| `--preempt` | Also stop running jobs with a lower bid (that are preemptible) if that's what it takes to start now. Never carried over: pass it again on `pasar bid` or `pasar restart` if still wanted. | off |
| `--non-preemptible` | Other jobs can never stop this one. | preemptible |
| `--grace DURATION` | Time between SIGTERM and SIGKILL when stopped, e.g. `180s`. Raise it if checkpointing takes longer than the default. | `120s` |
| `--retries N` | Auto-retry after a failure (non-zero exit, signal, `oom`, `gpu_oom`, `gpu_xid`). Cancellations are never retried; preemptions don't consume a retry. | `0` |
| `--name NAME` | Short display name. | derived from the command |
| `--note TEXT` | Say why this job matters — shown in the UI and `pasar show`. | empty |
| `--tag TAG` | Repeatable. **Always give at least one:** the job's category, e.g. the sweep or experiment it belongs to (`lr-sweep`, `ablation-heads`, `eval`). Jobs sharing a first tag share a colour in the web UI, so people can see at a glance what kind of work is running and queued. | none |
| `--by WHO` | Identify yourself (e.g. an agent's name) so humans know who to ask. | `$USER` |
| `--cwd DIR` | Working directory the command runs in. | current directory |
| `--no-env` | Don't capture your current environment for the job. | environment is captured |

**Take the whole GPU unless your job is a good candidate for sharing.** Whole GPU (no `--mem`) is
the default and the recommendation. Sharing only pays off when a job leaves the GPU idle much of
the time; otherwise every job on the box finishes later.

Your job is a **good candidate for sharing** if it has any of the following.
- Heavy CPU work between GPU steps (data loading, preprocessing, tokenization, RL environment steps, Python control flow)
- Small model or small batch size, so kernels don't fill the GPU
- Many tiny kernels in eager mode
- Frequent I/O waits (disk, network, checkpointing)

Your job is a **poor candidate for sharing** if it has any of the following.
- Large-batch training that already pins compute or memory bandwidth
- Tight memory usage close to the GPU's capacity
- A deadline or a need for early results (sharing delays every job's completion)

To share, pass `--mem` with an honest estimate of what the job needs.

**Keep the default bid (1000) unless the work should go ahead of what's queued**, and add
`--preempt` only when it is worth stopping someone else's running job (they lose work since their
last checkpoint). Without `--preempt`, a higher bid just waits for room; while it waits it holds a
reservation, so smaller jobs only start if they fit beside it or finish first.

**Submit work as granular jobs.** One job should be one run: a hyperparameter sweep is one job
per configuration, not one command that loops over them all. Small jobs let the scheduler pack
them next to other work, start some as soon as memory frees up, preempt or retry just one point,
and give each an accurate `--time`. One big looping job holds its resources for the
whole sweep, and losing it loses every point. For example:

    for lr in 1e-5 3e-5 1e-4; do
      pasar submit --time 45m --name "sweep-lr-$lr" --tag sweep-lr \
        --note "lr sweep for the SFT run" -- .venv/bin/python train.py --lr "$lr"
    done

Submitting returns the new job immediately (state `queued` or `running`):

    $ pasar submit --time 2h --json -- .venv/bin/python train.py
    {"id": 42, "name": "train", "state": "queued", ...}

If you omit `--name`, pasar derives one from the command (e.g. `train.py` above becomes `train`) —
skip the flag unless the derived name would be confusing. pasar also records the current git
commit and any uncommitted diff of `--cwd` automatically at submit time (visible as `git_commit`
in JSON, and the `git` field of `pasar show`) — you don't need to embed that yourself.

## Being preemptible correctly

Install the job-side helper into the job's own environment (no dependencies; not on PyPI):

    uv pip install "pasar-job @ git+https://github.com/fanpu/pasar#subdirectory=packages/pasar-job"

Every function in `pasar_job` is a safe no-op when the script isn't running under pasar, so you can
call them unconditionally, including in code you also run by hand. What's available:

| Function | Purpose |
|---|---|
| `pasar_job.resuming() -> bool` | `True` if an earlier attempt of this job already ran, so a checkpoint may exist. |
| `pasar_job.resumed(step=None)` | Report that the checkpoint was loaded and useful work has resumed. |
| `pasar_job.checkpoint(step=None)` | Report that a checkpoint was just saved. Work since the last checkpoint is what's lost on preemption, so call this often (at least every ~30 minutes). |
| `pasar_job.on_preempt(fn, exit_code=143)` | Register `fn` to run on SIGTERM (e.g. save a checkpoint), then exit. pasar sends SIGTERM and waits `PASAR_GRACE_SECONDS` (your `--grace`) before SIGKILL. Call from the main thread. |
| `pasar_job.progress(step, total_steps, **metrics)` | Report progress out of `total_steps` (required, a positive integer). pasar projects a running job's finish from it, instead of from `--time`: time so far × steps left ÷ steps done this attempt (counted from the step you passed to `resumed`). Report at least every few minutes. Extra keyword args must be finite numbers (e.g. `loss=1.84`) — the UI charts each one as its own line on the job's Metrics tab. |
| `pasar_job.note(text)` | Leave a free-text note on the job (e.g. "switched to lr 1e-5"). |
| `pasar_job.job_id() / .attempt() / .job_dir()` | Introspect the current job ID, attempt number (from 1), and scratch directory. |
| `pasar_job.memory_limit_bytes()` / `apply_memory_limit(device=0)` | Shared (`--mem`) jobs only: your byte limit, and a helper that caps PyTorch's allocator at it so `torch.cuda.OutOfMemoryError` is raised inside your process instead of pasar killing it from outside. Returns `None` for whole-GPU jobs. |

A minimal complete training loop:

```python
import pasar_job

pasar_job.apply_memory_limit()           # no-op for whole-GPU jobs
pasar_job.on_preempt(save_checkpoint)    # runs on SIGTERM, then exits

start = 0
if pasar_job.resuming():
    start = load_checkpoint()
    pasar_job.resumed(start)

for step in range(start, total_steps):
    loss = train_step()
    pasar_job.progress(step, total_steps, loss=loss)
    if step % 500 == 0:
        save_checkpoint()
        pasar_job.checkpoint(step)
```

Without Python, append JSON lines to `$PASAR_EVENTS` yourself, e.g.
`echo '{"event": "checkpoint", "step": 1200}' >> "$PASAR_EVENTS"`.

## Monitoring and control

Every subcommand below accepts `--json` for machine-readable output instead of the text tables
shown here.

    pasar ls [--all] [--state STATE]     # list jobs (default: running/queued + recent finished)
    pasar show <id>                      # one job's full detail
    pasar logs <id> [-f]                 # print (or -f: stream) a job's combined stdout/stderr
    pasar wait <id> [--timeout SECONDS]  # block until the job finishes; see exit codes below
    pasar cancel <id>                    # stop it and don't requeue it
    pasar bid <id> <new-bid> [--preempt] # change priority; --preempt may stop lower-bid jobs
    pasar restart <id> [--mem/--bid/--time/--retries/--whole-gpu/--preempt]  # requeue a finished job

`pasar ls` and `pasar show <id>` without `--json` print human-readable text:

    $ pasar ls
    ID  NAME   STATE                   BID   MEMORY               TIME          BY
    42  train  running                 1000  24.0 GiB / 26.4 GiB  18m / ~2h00m  agent-3
    43  eval   queued (preempted x1)   1500  whole GPU            blocked       agent-1

    $ pasar show 42
          job  #42 train
        state  running
          bid  1000
       memory  24.0 GiB / 26.4 GiB
         time  18m of ~2h00m
     attempts  1
      command  .venv/bin/python train.py --lr 3e-5
          cwd  /home/you/proj
           by  agent-3

`pasar restart <id>` requeues a **finished** job as a new attempt with the same ID, command and
working directory, so `pasar_job.resuming()` sees it and checkpoint-resume kicks in. Change
settings first with its flags, e.g. `pasar restart 42 --mem 48G --bid 1500 --time 3h`.

`pasar show --json <id>` / a job entry from `pasar ls --json` carries (key fields): `id`, `name`,
`state`, `reason`, `summary`, `bid`, `mode` (`"whole"` or `"shared"`), `mem_request`, `limit`,
`usage`, `over_limit`, `est_runtime`, `run_time`, `remaining`, `progress` (`step`, `total_steps`),
`last_checkpoint` (`step`, `ts`), `retries`, `retries_used`, `command`, `cwd`, `note`, `tags`,
`submitter`, `git_commit`, `lost` (time lost to preemption/failure, `known` may be `false`).

`pasar wait <id>` blocks, polling until the job reaches a terminal state, and exits with a code
that reflects the outcome:

| Exit code | Meaning |
|---|---|
| `0` | completed |
| `1` | failed |
| `2` | failed for an out-of-memory reason (`oom`, `gpu_oom`, `kernel_oom`) |
| `3` | cancelled |
| `4` | `--timeout` elapsed before the job finished (the job itself is unaffected) |

These apply to every `pasar` command, not just `wait`:

| Exit code | Meaning |
|---|---|
| `64` | bad arguments (e.g. `submit` without `--time`) |
| `69` | pasard is unreachable at the configured URL |
| `70` | the API rejected the request (e.g. job not found, already cancelled) — message on stderr |

## Why a job ended

`pasar show <id>` (or the `reason` field in JSON) explains a non-`completed` outcome:

| Reason | What happened | What to do |
|---|---|---|
| `oom` | pasar's watchdog SIGKILLed it: it was over its memory limit while the machine was under sustained memory pressure. | `pasar restart <id> --mem <bigger>` if the estimate was too low, otherwise just resubmit. |
| `gpu_oom` | A `torch.OutOfMemoryError` / "CUDA out of memory" was seen in the log. | Same as `oom`: restart with more `--mem`, or reduce batch size. |
| `kernel_oom` | The Linux kernel OOM-killed the job's cgroup. | Same as `oom`. |
| `gpu_xid` | A GPU fault (Xid error) was logged during the run — likely a driver/hardware issue, not your code. | Retry; if it recurs, flag the machine. |
| `signal` | Killed by a signal (e.g. `SIGSEGV`, `SIGABRT`) other than pasar's own SIGTERM/SIGKILL. | Check the log tail for a crash; fix and resubmit. |
| `exit` | Exited with a non-zero code for another reason; the summary has the last exception line if one was found. | Check `pasar logs <id>`. |
| `lost` | The job's systemd unit vanished (e.g. the machine rebooted) while pasar wasn't watching. | Eligible for auto-retry if `--retries` was set; otherwise `pasar restart <id>`. |
| `launch_error` | pasar couldn't even start the attempt (bad `--cwd`, systemd error). | Fix the command/cwd, then restart. |
| `cancelled` | Someone ran `pasar cancel <id>`, or the daemon shut it down as part of a cancel. | Nothing to do; resubmit if you still need it. |
| `preempted` | Stopped to make room for a higher-bid job that asked to preempt. It is requeued automatically — this is not a failure. | Nothing to do. |
| `target_gone` | Cloud only. The job's target is no longer configured here: `failed` if it was running (its sandbox may still be billing — end it at the provider), `cancelled` if it was only waiting (nothing was spent). | Put the target back and resubmit, or clean up at the provider yourself. |
| `price_rose` | Cloud only. The job is back to `awaiting`: its price rose past what was approved between approval and launch. `summary` has the old ceiling and the new price. | Approve it again in the web UI if the new price is still fine. |

## Cloud jobs

`pasar submit --on TARGET --gpu TYPE` runs on a configured rented-GPU target instead of the
local machine. It costs money, so it never launches on its own: it lands **awaiting** a person's
approval in the web UI, priced at submit time, and only starts once approved there.

    $ pasar submit --time 2h --on modal --gpu H100 --json -- .venv/bin/python train.py
    submitted #7 train (awaiting) on modal
      estimated $7.90, capped at $11.85 for this run
      approve it in the web UI to let it launch

Extra flags, cloud jobs only:

| Flag | Meaning |
|---|---|
| `--on TARGET` | Which configured cloud target to run on. Omit for the local GPU. |
| `--gpu TYPE` | **Required** for a cloud job, e.g. `H100` or `H100:4`. |
| `--env KEY` | Repeatable. Pass this environment variable through to the sandbox by name (unlike a local job, a cloud job's environment is *not* captured wholesale). |
| `--data PATH` | Not wired up yet — rejected with an error. A cloud job's input data has to arrive with the provider work (e.g. a volume mount in the target's config), not through `pasar submit`. |
| `--max-cost` | Dollars one attempt may spend. Refuses the submit outright if the estimate already exceeds it; otherwise the daemon pauses the job once it has spent that much and sends it back for approval — so a cap that bites buys fewer hours too (`pasar show` prints the shortened run time). |

`--mem`, `--retries` and `--preempt` are refused for a cloud job, and `pasar restart` on one is
refused too (it would spend money on whatever is in the working tree now, unseen): the error
gives the equivalent `pasar submit` command to run instead.

**There is no `pasar approve` command, and agents must never call `POST /api/jobs/{id}/approve`
or `/reject` directly.** Approval is a web-UI-only action, by design: a person looks at the price
before it's spent. `pasar cloud [--json]` shows what's waiting:

    $ pasar cloud
    TARGET  PROVIDER  RUNNING  TODAY          MONTH           RATES
    modal   modal     1/2      $7.90 / $50.00 $7.90 / $300.00 gpu_hour_cost_h100=$3.95, ...

    1 job(s) awaiting approval:
    ID  NAME   STATE     BID   MEMORY            TIME  BY
    7   train  awaiting  1000  H100 (~$7.90)      —    agent-3

A cloud job's `state` field also passes through `awaiting` if its approved run time runs out
before the job finishes (`reason` `time_limit`), if the provider reclaims the machine (reason
`cloud_preempted`), or if its price rose past what was approved between approval and launch
(reason `price_rose`, with the old ceiling and new price in `summary`) — in every case, it needs
approving again in the web UI, the same as a fresh submission. `pasar wait` keeps waiting through
all of these (they aren't a terminal state); use `--timeout` if you don't want to wait on a human.
If the target stops being configured on this pasard, a running cloud job ends `failed` (reason
`target_gone` — its sandbox may still be running and billing at the provider, since nothing here
can reach it any more) and a waiting one ends `cancelled` (reason `target_gone`, nothing was
spent) — both ordinary terminal states with `pasar wait`'s usual exit codes.

`pasar show <id>` and `pasar ls`/`pasar show --json` carry a `cloud` object for cloud jobs:
`target`, `gpu`, `phase`, `estimated_cost`, `max_cost` (the ceiling actually governing the job
right now — the approved figure once launched, a live-priced one before approval), `user_capped`
(whether that ceiling is the submitter's own `--max-cost`), `approved_seconds`/`full_seconds` (the
run time one approval buys, and what it would buy without `--max-cost`), and `console_url` (only
while the daemon is actively watching the attempt).

## HTTP API quick reference

For agents that prefer HTTP directly over the CLI. All bodies and responses are JSON; base URL is
whatever `PASAR_URL` would be (default `http://127.0.0.1:8750`).

| Method & path | Does |
|---|---|
| `POST /api/jobs` | Submit a job. |
| `GET /api/jobs` | List jobs (`?all=true`, `?state=queued`, or `?since=<unix ts>&until=<unix ts>` for jobs that ran in that window). |
| `GET /api/jobs/{id}` | One job, plus its attempt history. |
| `PATCH /api/jobs/{id}` | Change `bid` and/or `preempt` (bool); a field left out keeps its value. |
| `POST /api/jobs/{id}/cancel` | Cancel a job. |
| `POST /api/jobs/{id}/restart` | Requeue a finished job, optionally changing `mem`/`whole_gpu`/`time`/`bid`/`retries`; `preempt` (default `false`) is not carried over. |
| `POST /api/jobs/{id}/approve` / `/reject` | Web-UI-only. **Agents must never call these** — a person approves a cloud job's cost, not code. |
| `GET /api/cloud` | Cloud targets: budget, spend today/this month, live rates, and jobs awaiting approval. |
| `GET /api/jobs/{id}/logs` | A chunk of output (`?offset=N`), or an SSE stream with `?follow=true`. |
| `GET /api/jobs/{id}/events` | The job's raw protocol events (checkpoint/resumed/progress/note). |
| `GET /api/jobs/{id}/metrics` | Stored per-attempt metric summaries (avg/max/total). |
| `GET /api/jobs/{id}/usage` | In-memory time series of this attempt's measured memory usage. |
| `GET /api/status` | Pool size, reserved/free memory, pressure, blocked/waiting queue state, recent machine events. |
| `GET /api/gpu` | GPU power/temperature/utilisation history from Prometheus, if configured. |
| `GET /api/stream` | SSE stream of `{status, jobs}` snapshots, one per state change. |
| `GET /llms.txt` | This guide, as `text/plain`. |
| `GET /agents.md` | This guide, as `text/markdown` (identical text). |

Submit example:

    curl -s -X POST http://127.0.0.1:8750/api/jobs \
      -H 'Content-Type: application/json' \
      -d '{
            "command": ".venv/bin/python train.py --lr 3e-5",
            "time": "2h", "cwd": "/home/you/proj",
            "mem": "24G", "bid": 1000,
            "note": "lr sweep point 3", "submitter": "agent-3"
          }'

Body fields (all but `command`, `time`, `cwd` are optional): `command` (string), `time` (duration
string or seconds), `cwd` (absolute path), `mem` (size string or bytes; omit for the whole GPU),
`bid` (int, default 1000), `preempt` (bool, default `false`), `preemptible` (bool, default `true`), `grace` (duration, default
`120s`), `retries` (int, default 0), `name`, `note`, `tags` (list of strings), `submitter`, `env`
(a `{string: string}` map to give the job, or omit/`null` to give it none), and, for a cloud job,
`target` (default `"local"`), `gpu` (required once `target` isn't `"local"`), `env_keys` (list of
names to pass through), `data` (not wired up yet — a non-empty list is rejected), `max_cost`
(dollars one attempt may spend: refuses the submit if the estimate already exceeds it, otherwise
pauses the job once it has spent that much).

A non-2xx response body is `{"detail": "..."}`: `404` job not found, `409` conflicting state (e.g.
already cancelled), `422` bad input (e.g. unparsable `time`).

Reading state is just a `GET`:

    $ curl -s http://127.0.0.1:8750/api/status | python3 -m json.tool
    {
      "pool": 103079215104,
      "reserved": 45097156608,
      "free": 57982058496,
      "psi_some_avg10": 0.3,
      ...
    }

## Etiquette on a shared box

- Estimate `--time` (and `--mem`, if you share) honestly — the scheduler and every other job's projected start
  time depend on them.
- Split work into small, independent jobs (one per sweep point, seed or eval) rather than one
  long command that does it all.
- Leave the bid at **1000** unless the work truly should go ahead of what's queued, and use
  `--preempt` only when it's worth stopping someone else's running job.
- Take the whole GPU (the default) unless your job is a good candidate for sharing (see
  "Submitting a job"); sharing a GPU that a job already keeps busy slows every job on it.
- `pasar cancel` anything you no longer need; a queued or running job you've abandoned blocks
  everyone behind it.
- Tag every job with its category (`--tag`, first tag = the sweep or experiment), so related jobs
  group together in the UI.
- Use `--note` to say why the job matters, and `--by` to identify yourself — someone (human or
  agent) may need to know who to ask before touching your job.
