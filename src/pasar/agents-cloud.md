# pasar: cloud GPUs (agent guide topic)

This is the cloud topic of the pasar agent guide: `pasar guide cloud` prints it, and pasard serves
it at `/llms-cloud.txt`. The main guide (`pasar guide`) covers everything else. Read this before
you submit any cloud work.

`pasar submit --on TARGET --gpu TYPE` runs a job on a rented GPU at a configured cloud target
(today, Modal) instead of the local GB10. The job keeps the same record, logs, progress,
checkpoints, `pasar wait` and `pasar cancel` as a local one. It also costs real money.

## The rules

- **Submit to the cloud only with the user's explicit go-ahead for this work.** Never pass
  `--on` as your own call, and never to dodge a busy local GPU.
- **Never approve.** Every cloud attempt waits in `awaiting` until a person approves it. There is
  no `pasar approve` command. Never call `POST /api/jobs/{id}/approve` (with or without
  `?extend=1`) or `/reject`, on the user's behalf or otherwise. The approval UI isn't built yet,
  so today the user approves by calling the endpoint themselves. That is still never an agent's
  call.
- **Each job has a lifetime spending cap:** the target's `max_job_cost`, shown as JOB CAP in
  `pasar cloud` (default $10). It covers every attempt, re-approval and extension of the job. A
  submit whose estimate is already over it is refused. Only the user raises it, in pasard's
  config. Ask them; don't work around it (for example by splitting one run into several jobs).
- **Always pass `--max-cost`**, the dollars one attempt may spend.

## Submitting

Once the user has said yes:

    $ pasar submit --time 1h30m --max-cost 8 --on modal --gpu H100 --tag sft \
        --note "sft on the full set" -- .venv/bin/python train.py
    submitted #7 train (awaiting) on modal
      estimated $6.00, capped at $8.00 (your --max-cost) for this run
      it will be paused after 2h00m instead of 2h15m, to stay under that cap
      a person has to approve it before it launches: POST /api/jobs/7/approve (no web UI for it yet)

(Figures illustrative.) The last line is for the user, not for you.

Extra flags, cloud jobs only:

| Flag | Meaning |
|---|---|
| `--on TARGET` | Which configured cloud target to run on. Omit for the local GPU. |
| `--gpu TYPE` | **Required** for a cloud job, e.g. `H100` or `H100:4`. |
| `--env KEY` | Repeatable. Pass this environment variable through to the sandbox by name (unlike a local job, a cloud job's environment is *not* captured wholesale). |
| `--data PATH` | Not wired up yet: rejected with an error. A cloud job's input data has to arrive with the provider work (e.g. a volume mount in the target's config), not through `pasar submit`. |
| `--max-cost` | Dollars one attempt may spend. Refuses the submit outright if the estimate already exceeds it; otherwise the daemon pauses the job once it has spent that much and sends it back for approval. So a cap that bites buys fewer hours too (`pasar show` prints the shortened run time). |

`--mem`, `--retries` and `--preempt` are refused for a cloud job. `pasar restart` on one is
refused too (it would spend money on whatever is in the working tree now, unseen): the error
gives the equivalent `pasar submit` command to run instead.

A submit is also refused when:

- the target has no provider wired up on this pasard (`pasar cloud` shows it as
  `(no provider)`);
- the estimate (`--time` × the rate) is over the job cap. The error names the GPUs on that target
  whose estimate for the same run would fit. Don't shorten `--time` to squeeze under it: the
  estimate should be honest, and the cap covers every later attempt anyway.

## Approval, and what's waiting

A cloud job never launches on its own: it lands `awaiting`, priced at submit time, and starts
only once a person approves it. `pasar cloud [--json]` shows each target, its budgets, the job
cap, its GPUs and what's waiting (prices elided here):

    $ pasar cloud
    TARGET  PROVIDER  RUNNING  TODAY          MONTH           JOB CAP  STORED              RATES
    modal   modal     1/2      $… / $50.00    $… / $300.00    $10.00   0.0 GiB (0 job(s))  cpu_hour_cost_sandbox=$…, ...

    modal GPUs (what --gpu accepts):
      GPU        $/HOUR  MEMORY
      T4         $…      16GB
      ...
      H100       $…      80GB

    1 job(s) awaiting approval:
    ID  NAME   STATE     BID   MEMORY        TIME  BY
    7   train  awaiting  1000  H100 (~$…)    —     agent-3

Even an approved job starts only if it fits the target's daily and monthly budgets (TODAY and
MONTH); otherwise it waits in the queue, blocked on `budget`. At most RUNNING's second number
run at once.

## Pauses, and why a cloud job ended

A cloud job goes back to `awaiting` rather than ending when:

| Reason | What happened | What to do |
|---|---|---|
| `time_limit` | Its approved run time ran out before it finished. | Tell the user; they approve it again to carry on. |
| `job_cap` | It has spent all its lifetime cap (`max_job_cost`). It waits until the user raises the cap. | Tell the user. Never work around it. |
| `cloud_preempted` | The provider reclaimed the machine (a spot interruption, a host failure). Not a failure. | Tell the user; they approve it again to run again. |
| `price_rose` | Its price rose past what was approved, between approval and launch. `summary` has the old ceiling and the new price. | Tell the user; they approve again if the new price is fine. |

An approved job starts a fresh attempt from the same code snapshot, with `PASAR_RESUMING=1`. It
resumes from its last checkpoint if it wrote one under `pasar_job.persist_dir()`, or starts over
if it never checkpointed. `pasar wait` keeps waiting through `awaiting` (it isn't a terminal
state); use `--timeout` if you don't want to wait on a human.

Cloud-only terminal reasons:

| Reason | What happened | What to do |
|---|---|---|
| `pause_limit` | It ran out of its approved time 5 times running without finishing. Each pause on its own is not a failure, but 5 with nothing to show for them means something is wrong. | Check that it checkpoints and resumes, then ask about resubmitting with a longer `--time`. |
| `target_gone` | Its target is no longer configured here: `failed` if it was running (its sandbox may still be billing: tell the user to end it at the provider), `cancelled` if it was only waiting (nothing was spent). | Tell the user. |

## Getting results back

What a cloud job wrote under `pasar_job.persist_dir()` is pulled into `<pull_dir>/<id>/` when it
finishes, then deleted at the provider. A pull skipped for disk space, or given up on, leaves it
there only until `cloud.persist.sweeps_at`, when it is deleted for good. For a job nobody pulled,
that is the only copy.

`pasar pull <id> [--to DIR] [--keep]` fetches it by hand. It verifies the file count and byte
total against what the provider reports, and only then deletes the remote copy (`--keep` leaves
it, for the sweep). It is refused for a job that hasn't finished yet (its checkpoint is still
live and the next attempt may resume from it), a local job (there is nothing on a provider to
pull), or a destination that already has something in it. A job that never wrote anything
reports that and exits `0`.

## The `cloud` object

`pasar show <id>`, and `pasar ls --json`/`pasar show --json`, carry a `cloud` object for cloud
jobs:

- `target`, `gpu`.
- `phase`: where the attempt the daemon is watching has got to (`pending`, `starting`, `running`,
  `success`, `exit-code`, `stopped`, `reclaimed`, `time_limit` or `signal`), or `null` when there
  is no live attempt. Where the *job* has got to is its own `state` field.
- `estimated_cost`, and `max_cost`: the ceiling governing this attempt right now (the approved
  figure once launched, a live-priced one before approval).
- `user_capped`: whether that ceiling is the submitter's own `--max-cost`.
- `approved_seconds`/`full_seconds`: the run time one approval buys, and what it would buy
  without the dollar caps.
- `job_cap`/`job_spent`: the job's lifetime cap and what its attempts have spent so far.
- `console_url`: only while the daemon is watching the attempt.
- `needs_more_time`: extra seconds a running attempt's own pace projects past its approved run
  time, or `null` when it's on pace or there isn't enough progress yet to judge (that takes 5
  minutes and at least 3 progress reports since the attempt started). A flagged job keeps
  running; the user can raise its ceiling (`POST /api/jobs/{id}/approve?extend=1`, never for an
  agent to call) or let it pause at the limit and approve it again. Tell them.
- `persist` (`null` until the job finishes): what it left behind. `files`/`bytes`/`pulled_at`/
  `pulled_to` of the last pull that landed, `remote_deleted`, `remote_bytes` (still at the
  provider, as last measured), `sweeps_at`, `swept_at`/`swept_bytes` and `last_error`.
