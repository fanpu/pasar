# pasar: cloud GPUs (agent guide topic)

This is the cloud topic of the pasar agent guide: `pasar guide cloud` prints it, and pasard serves
it at `/llms-cloud.txt`. The main guide (`pasar guide`) covers everything else. Read this before
you propose cloud work to the user, and again before you submit any.

`pasar submit --on TARGET --gpu TYPE` runs a job on a rented GPU at a configured cloud target
(today, Modal) instead of the local GB10. The job keeps the same record, logs, progress,
checkpoints, `pasar wait` and `pasar cancel` as a local one. It also costs real money.

## The rules

- **Submit to the cloud only with the user's explicit go-ahead for this work.** Proposing is
  yours to do; deciding is theirs. Never pass `--on` as your own call, and never submit to dodge
  a busy GPU without asking.
- **Never approve.** Every cloud attempt waits in `awaiting` until a person approves it. There is
  no `pasar approve` command. Never call `POST /api/jobs/{id}/approve` (with or without
  `?extend=1`) or `/reject`, on the user's behalf or otherwise. The approval UI isn't built yet,
  so today the user approves by calling the endpoint themselves. That is still never an agent's
  call.
- **`--on <group>` (e.g. `--on modal`) is the normal way to submit** when a target name covers
  several accounts: pasar picks which account pays, at submit, and may move a job that has not
  launched yet to another account of the group (`moved`); `--on <account>` pins a specific one
  instead. Either way the same rule above still applies: proposing and picking a target is
  yours, submitting still needs the user's explicit go-ahead.
- **Each job has a lifetime spending cap:** the target's `max_job_cost`, shown as JOB CAP in
  `pasar cloud` (default $10). It covers every attempt, re-approval and extension of the job. A
  submit whose estimate is already over it is refused. Only the user raises it, in pasard's
  config. Ask them; don't work around it (for example by splitting one run into several jobs).
- **Always pass `--max-cost`**, the dollars one attempt may spend.

## Don't hold back an idea because it is slow here

An experiment worth doing is worth proposing, even if it would take days on the GB10. Don't
silently drop it. Don't shrink it until it fits in an afternoon, and don't quietly swap in a
weaker version. Propose it as it should be run, with the cloud route and its napkin math as the
option, and let the user decide. Say what you would run locally instead if they say no.

Proposing is free. Only running costs money, and running needs the user's yes.

## What a cloud GPU buys

Speed. The GB10 has a lot of memory (128 GB, shared with the CPU) but a fraction of a datacenter
GPU's compute and memory bandwidth. Compute-bound training runs several times faster on an
H100-class card, so a run that takes a day here can finish in hours.

What it doesn't buy: memory. Only the H200, B200 and B300 have more than the GB10. A job that
only fits here because of the unified memory needs one of those, or sharding across several GPUs.

Figures for napkin math, not benchmarks. The GB10 row is what it achieved here. Every other row
is the vendor's listed peak, of which real work reaches only part (see the napkin math):

| GPU (`--gpu`) | Memory (GB) | Dense BF16 TFLOPS | Bandwidth (GB/s) |
|---|---|---|---|
| GB10 (local) | 128 unified | ~88 achieved here on large BF16 matmuls (NVIDIA publishes none) | ~240 achieved here (vendor: 273) |
| `T4` | 16 | 65 (FP16: T4 has no BF16) | 300 |
| `L4` | 24 | 121 | 300 |
| `A10` | 24 | 125 | 600 |
| `L40S` | 48 | 362 | 864 |
| `A100-40GB` | 40 | 312 | 1,555 |
| `A100-80GB` | 80 | 312 | 1,935-2,039 |
| `RTX-PRO-6000` | 96 | ~504 | 1,597-1,792 |
| `H100` | 80 | 989 | 3,350 |
| `H200` | 141 | 989 | 4,800 |
| `B200` | 180 | 2,250 | 7,700 |
| `B300` | 270 | 2,250 | 7,700 |

- Dense means without sparsity. NVIDIA's sheets lead with the sparse figure, which is twice
  this. Sources: NVIDIA's data sheet for each card (for the RTX PRO 6000, its architecture
  whitepaper), and Modal's GPU docs for which variant it rents, checked September 2026. The H100
  and H200 are the SXM parts (Modal says so).
- Approximate: the T4 figure is FP16 standing in for BF16. Modal doesn't say whether its A100s
  are PCIe or SXM, hence the bandwidth range. It doesn't say which RTX PRO 6000 edition it rents
  either: ~504 is the Workstation Edition's figure in NVIDIA's architecture whitepaper (the data
  sheets give none), and the Server Edition has the lower bandwidth. For the B300, NVIDIA's sheet says 270 GB per GPU while Modal's own figures imply 288.
- `A100` on its own means the 40 GB card. `pasar cloud` lists what a target actually accepts,
  with each GPU's memory. Prices are never in this guide: read them from `pasar cloud`.

## When to ask

Ask the user about the cloud when the first two hold. The third decides what you say, not
whether you ask:

1. **The local wait is long, roughly over 2 hours.** Count the time until the result: the run
   itself plus the local queue ahead of it. Get the run time from a measured rate (time a few
   hundred steps, then multiply out) or, once it runs under pasar, from its progress reports
   (`remaining`, and `pasar ls` marks a projected time with `*`). Get the queue wait from pasar:
   `pasar ls` shows a queued job's `starts ~HH:MM`, and in `--json` the last span in `projected`
   ends when the job is expected to finish. For work not yet submitted, judge from what
   `pasar ls` shows ahead of it.
2. **A cloud GPU would plausibly more than halve it.** Check the table: is the job limited by
   compute or bandwidth that a bigger card has more of? A job limited by data loading, CPU work,
   Python overhead or I/O gains little from a faster GPU.
3. **Check the estimate against the job cap.** If it doesn't fit, still propose it and say so:
   the user may raise the cap, or pick a cheaper card or a short first run.

Ask before you start, not after the local run is half done. One ask covers one piece of work: a
sweep is one ask, with the per-job and the total cost.

## Choosing a GPU

1. **Memory first.** Weights, gradients, optimizer state and activations must all fit, with
   headroom. Measure peak memory on the GB10 (`peak` in `pasar show --json`), or work it out:
   BF16 weights take 2 bytes per parameter, and full training with Adam roughly 16 bytes per
   parameter before activations.
2. **Then the cheapest card that fits and is fast enough.** Cost is rate × time, so a faster card
   can cost less in total: a card twice the price that runs three times faster is cheaper.
3. **Smoke-test on a cheap card first.** A few minutes on the cheapest card that fits (or a
   cut-down config that fits a cheap one) proves the image builds, the data is reachable, and
   checkpoint and resume work. Also test resuming locally first: run a few minutes, cancel,
   resubmit. The smoke test costs money too, so it is part of the ask.
4. **More than one GPU (`H100:4`) only if the code shards** across them (DDP, FSDP and the like).
   A single-process script on four GPUs pays for four and uses one.

## The napkin math

- **Speedup.** Use the column that limits the job (TFLOPS for large-batch, matmul-heavy training;
  bandwidth for small batches, decoding and optimizer-heavy steps), in two steps:
  1. Compare like for like. The GB10 row is achieved, the others are peak, and a datacenter card
     reaches roughly two-thirds of its listed peak on real matmuls (bandwidth usually fares a bit
     better; two-thirds stays on the safe side). So the ratio is the cloud figure × 2/3 ÷ the
     GB10's: an H100 is 989 × 2/3 ÷ 88 ≈ 7.5× the GB10, an A100 312 × 2/3 ÷ 88
     ≈ 2.4×.
  2. Discount that for the parts of a real job a faster GPU doesn't speed up: data loading,
     Python, small kernels. Taking about two-thirds of it again is a fair first guess for a
     training loop that keeps the GPU busy; take less if it doesn't. The H100 comes out near 5×.

  Both two-thirds are rough. Say so.
- **Cloud time** = local run time ÷ speedup, plus a few minutes to start (longer the first time an
  image is built).
- **Cost per hour** = the GPU's `$/HOUR` from `pasar cloud` (times the count, for `H100:4`), plus
  what pasar adds for the sandbox around it: 4 × `cpu_hour_cost_sandbox` + 32 ×
  `mem_gib_hour_cost_sandbox`, both in the RATES column. On a cheap card that addition is a large
  share of the total.
- **Cost** = cloud time × cost per hour. That is what `pasar submit` would estimate for the same
  `--time`.
- **Ceiling.** One approval lets an attempt run up to the target's `timeout_factor` × `--time`
  (1.5 × by default), unless `--max-cost` or what is left of the job cap stops it sooner.

**Worked example.** The speed ratios come from the table; the job's timings and the prices are
illustrative, not real rates or measurements.

- A fine-tune timed at 1.1 s/step over 200 steps, with 20,000 steps to go: about 6 hours.
  `pasar ls` says it would start in about an hour, so the result is about 7 hours away.
- It peaked at 50 GB on the GB10, so it needs an 80 GB card: `A100-80GB`, `H100` or bigger.
- The H100 is about 7.5× the GB10 like for like, and about 5× once the rest of the job is
  counted, so about 1h15m in the cloud, plus a few minutes to start.
- Say `pasar cloud` shows the H100 at $3/hour and the sandbox adds $1/hour: $4/hour, so about $5.
  Pass `--time 1h15m --max-cost 7`. That lets the attempt run up to 1h45m, and it stays under the
  $10 job cap.
- The A100-80GB is about 2.4× the GB10 like for like, so about 1.6× for the whole job: about 3h45m.
  At, say, $2/hour plus $1, that's about $11: slower, dearer, and over the cap. The faster card is
  the cheaper one.

## Asking the user

Put these in the ask, briefly:

1. **Local:** the projected time to a result, and how you measured it (steps timed, pasar's
   projection, queue wait).
2. **GPU:** the one you propose and why (memory needed, what limits the job).
3. **Speedup:** what you expect and how rough it is.
4. **Cloud time:** the expected run time, and the `--time` you'd pass.
5. **Cost:** time × $/hour from `pasar cloud`, the `--max-cost` you'd pass, and the job cap. For a
   sweep, per job and in total.
6. **Needs:** checkpointing to `pasar_job.persist_dir()` (does it already?), any `--env` keys,
   where its data comes from, and a smoke test first if the cloud path is untested.

For example, with the numbers above: "This fine-tune would take ~6 h on the GB10 (1.1 s/step,
timed over 200 steps, 20k steps) plus ~1 h in the queue. On an H100 (it needs ~50 GB) I'd expect
~5× faster, a rough guess (its peak discounted to what cards really reach, against the GB10's
measured speed, then discounted again for the rest of the job): ~1h15m. At ~$4/h from `pasar
cloud` that's ~$5; I'd pass `--max-cost 7`, under the $10 job cap. It already checkpoints to persist_dir. Want
me to submit it?"

## Spending prudently

- Stay well under the job cap. A run that would need most of it is better preceded by a short
  first run, as a separate small job that proves the setup and checkpoints, then the user's
  decision on the rest.
- Always pass `--max-cost`, a little above the estimate (the example: $5 estimated, `--max-cost
  7`). A cap that bites pauses the job rather than killing it, and the next approval resumes from
  the checkpoint.
- Size `--time` for the whole run, not for a slice of it. A job may pause at its approved run
  time only 5 times in its whole life (`pause_limit`): every `time_limit` pause counts, across all
  its attempts, not just ones in a row, and the fifth fails it. So don't plan one run as a string
  of short attempts. Still checkpoint every 10-15 minutes of wall-clock time, and save the first
  one early: a pause, a reclaim or the cap resumes from it.
- `pasar cancel` a cloud job you no longer need, awaiting or running.
- Results left at the provider are billed as storage even when nothing runs. `pasar pull` them.

## Submitting

Once the user has said yes:

    $ pasar submit --time 1h15m --max-cost 7 --on modal --gpu H100 --tag sft \
        --note "sft on the full set" -- .venv/bin/python train.py
    submitted #7 train (awaiting) on modal
      estimated $5.00, capped at $7.00 (your --max-cost) for this run
      it will be paused after 1h45m instead of 1h52m, to stay under that cap
      a person has to approve it before it launches: POST /api/jobs/7/approve (no web UI for it yet)

(Figures illustrative.) The last line is for the user, not for you.

Extra flags, cloud jobs only:

| Flag | Meaning |
|---|---|
| `--on TARGET` | Which configured cloud target to run on, or a group of them (pasar picks one account at submit, and may move the job to another before its first launch if that account can no longer pay for it). Omit for the local GPU. |
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
- `--on` named a group and none of its accounts will take the job: each is out of the month's
  budget for it, or is refusing to launch anything (the error says which, per account); ask the
  user.

## Approval, and what's waiting

A cloud job never launches on its own: it lands `awaiting`, priced at submit time, and starts
only once a person approves it. `pasar cloud [--json]` shows each target, its budgets, the job
cap, its GPUs and what's waiting (prices elided here):

    $ pasar cloud
    TARGET  OWNER  GROUP  PROVIDER  RUNNING  TODAY          MONTH           JOB CAP  STORED              RATES
    modal   alice  -      modal     1/2      $… / $50.00    $… / $300.00    $10.00   0.0 GiB (0 job(s))  cpu_hour_cost_sandbox=$…, ...

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

OWNER says whose credit a target spends; GROUP is the group (if any) it belongs to. A job's own
`cloud` object carries the same two fields, so a job detail view can say whose account it ran on.
MONTH ends in `(budget exhausted)` once pasar's own budget for the target is spent, and in
`(credit exhausted)` once the provider's own books say the account's free allowance is gone (only
where pasard has read them). In `--json`, a target's `budget_exhausted` is the first,
`credit_exhausted`, `credit_used` and `credit_as_of` the second; the credit fields are `null`
when pasard doesn't know.

If a job on a target is refused right after it was approved, or a target that should work reports
no provider, `pasar cloud check` re-runs the account survey: for each target with a `profile`, it
authenticates, reads which workspace the token reaches (loudly, if two targets share one — that
means one allowance counted twice), and how much credit is used. LEFT OF BUDGET is the target's
monthly budget in pasar's config less that, not the provider's own allowance. It exits non-zero on any problem,
so it doubles as a health check; it never prints a token, and reports rather than raises when
`modal` is not installed.

## Pauses, and why a cloud job ended

A cloud job goes back to `awaiting` rather than ending when:

| Reason | What happened | What to do |
|---|---|---|
| `time_limit` | Its approved run time ran out before it finished. | Tell the user; they approve it again to carry on. |
| `job_cap` | It has spent all its lifetime cap (`max_job_cost`). It waits for the user to raise the cap and approve it again, but only for `approval_ttl` (24 h by default; `summary` gives the time): unapproved by then, it is cancelled. Resubmitting starts over from scratch: a new job can't resume from this one's checkpoint, which is pulled home once this job ends. | Tell the user, with the deadline. Never work around it. |
| `cloud_preempted` | The provider reclaimed the machine (a spot interruption, a host failure). Not a failure. | Tell the user; they approve it again to run again. |
| `price_rose` | Its price rose past what was approved, between approval and launch. `summary` has the old ceiling and the new price. | Tell the user; they approve again if the new price is fine. |
| `moved` | Submitted to a group and moved to another account of the group before its first launch, because the first could no longer pay for it (by pasar's ledger or its provider's own books), is refusing to launch anything, or its provider failed. An approval it had was for the first account's credit, so it is dropped. `summary` names both accounts. | Tell the user; they approve it to spend the new account's credit. |

If a group job's account refuses to start it (`account_unusable`, below) before it has ever run,
pasar moves it to another account in the group and it waits in `awaiting` with reason
`account_unusable`: its approval was for the refusing account's owner's credit, so it is dropped.
Its `spec.target` changes, the refused attempt stays in its history, and `summary` names the
account that refused, why, and the new account and its owner. Tell the user; they approve it to
spend the new account's credit. Only a job that has never run is moved, and each account gets one
try.

An approved job starts a fresh attempt from the same code snapshot, with `PASAR_RESUMING=1`. It
resumes from its last checkpoint if it wrote one under `pasar_job.persist_dir()`, or starts over
if it never checkpointed. `pasar wait` keeps waiting through `awaiting` (it isn't a terminal
state); use `--timeout` if you don't want to wait on a human.

Cloud-only terminal reasons:

| Reason | What happened | What to do |
|---|---|---|
| `pause_limit` | It ran out of its approved time for the 5th time in its life (every `time_limit` pause counts, not only ones in a row) without finishing. One pause is not a failure, but 5 means `--time` was far too short or it isn't resuming. | Check that it checkpoints and resumes, then ask about resubmitting with a `--time` for the whole run. A resubmit starts over; this job's checkpoints are pulled home. |
| `out_of_credit` | The provider ended an attempt on an account its own books say is out of free credit. Not the job's fault, but approving it again would only be refused, so it fails instead of pausing; `summary` says whose account and when its cycle resets. | Tell the user. Resubmitting to the group (`--on <group>`) picks an account with credit left; a resubmit starts over, and this job's checkpoints are pulled home. |
| `account_unusable` | Its account refused to start it before any sandbox existed, so nothing was spent. The account itself can't run anything right now: a workspace past its spend limit (its billing can still look fine) or credentials Modal no longer accepts. Only a job pinned to that account ends this way: one named to it by `--on <account>`, or one that has already run there. A group job that never ran is moved instead and waits to be approved there (see above), and fails only once every account it could go to refused or couldn't take it; `summary` lists each account and its reason. pasar skips that account for new group submits, and moves group jobs waiting on it that never ran to another account (`moved`), for an hour, then tries it again. | Tell the user which account refused and why: they raise that workspace's spend limit, or fix its credentials. Then resubmit. |
| `target_gone` | Its target is no longer configured here, or its provider could not be set up (often a bad `~/.modal.toml`; pasard's log says which). `failed` if it was running: its sandbox may still be billing, so tell the user to end it at the provider. A waiting job is left waiting when only the provider failed, and `cancelled` when the target left the config; its summary says whether anything was spent and where earlier attempts' files are. | Tell the user what the summary says to fix. |

## Getting results back

What a cloud job wrote under `pasar_job.persist_dir()` is pulled into `<pull_dir>/<id>/` when it
finishes, then deleted at the provider. A pull skipped for disk space, or given up on, leaves it
there only until `cloud.persist.sweeps_at`, when it is deleted for good. For a job nobody pulled,
that is the only copy.

`pasar pull <id> [--to DIR] [--keep]` fetches it by hand. It verifies every file against what
the provider listed, and only then deletes the remote copy, exactly the files it verified
(`--keep` leaves it, for the sweep). If the job's files changed while it pulled, it keeps the
remote copy and says so: pull again with a different `--to` for the rest. It is refused for a
job that hasn't finished yet (its checkpoint is still live and the next attempt may resume from
it), a local job (there is nothing on a provider to pull), or a destination that already has
something in it. A job that never wrote anything
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
- `owner`/`group`: whose credit this runs on, and which group (if any) its target belongs to —
  both `null` once the target is no longer configured, like `job_cap`.
- `console_url`: only while the daemon is watching the attempt.
- `needs_more_time`: extra seconds a running attempt's own pace projects past its approved run
  time, or `null` when it's on pace or there isn't enough progress yet to judge (that takes 5
  minutes and at least 3 progress reports since the attempt started). A flagged job keeps
  running; the user can raise its ceiling (`POST /api/jobs/{id}/approve?extend=1`, never for an
  agent to call) or let it pause at the limit and approve it again. Tell them.
- `persist` (`null` until the job finishes): what it left behind. `files`/`bytes`/`pulled_at`/
  `pulled_to` of the last pull that landed, `remote_deleted`, `remote_bytes` (still at the
  provider, as last measured), `sweeps_at`, `swept_at`/`swept_bytes` and `last_error`.
