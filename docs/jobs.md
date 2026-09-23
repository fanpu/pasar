# Writing jobs for pasar

pasar can stop your job at any time to make room for a higher-bid job that asked to preempt, then start it again later
with the same command. Jobs that follow these conventions lose almost nothing when that happens.

## 1. Make the command resumable

When the job starts, look for a checkpoint and continue from it. `PASAR_RESUMING=1` tells you an
earlier attempt ran.

## 2. Checkpoint regularly

At least every 30 minutes. Work since the last checkpoint is lost on preemption.

## 3. Save when asked to stop

pasar sends SIGTERM and waits `PASAR_GRACE_SECONDS` (default 120) before SIGKILL. Submit with
`--grace` if saving takes longer.

## 4. Report checkpoints

Install the helper into your job's environment (it has no dependencies; it isn't on PyPI, so
install straight from the repo):

    uv pip install "pasar-job @ git+https://github.com/fanpu/pasar#subdirectory=packages/pasar-job"

```python
import pasar_job

pasar_job.apply_memory_limit()            # shared jobs: PyTorch raises OOM at your limit
pasar_job.on_preempt(save_checkpoint)     # runs on SIGTERM, then exits

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

Without Python, append JSON lines yourself:

    echo '{"event": "checkpoint", "step": 1200}' >> "$PASAR_EVENTS"

Events: `checkpoint`, `resumed`, `progress` (`step`, `total_steps`, plus any numeric metrics like
`loss` — these are charted in the job panel), `note` (`text`).

`progress` needs `total_steps`: once a running job reports it, pasar projects the job's finish from
its pace (time so far × steps left ÷ steps done this attempt) instead of from `--time`, so the
queue behind it gets more accurate start times. Pass `resumed` the step you resumed from, so a
resumed attempt's pace is counted from there.

## 5. Whole GPU or shared, and estimates

Whole GPU (no `--mem`) is the default and the recommendation. Sharing only pays off when a job
leaves the GPU idle much of the time; otherwise every job on the box finishes later.

Your job is a **good candidate for sharing** if it has any of the following.
- Heavy CPU work between GPU steps (data loading, preprocessing, tokenization, RL environment steps, Python control flow)
- Small model or small batch size, so kernels don't fill the GPU
- Many tiny kernels in eager mode
- Frequent I/O waits (disk, network, checkpointing)

Your job is a **poor candidate for sharing** if it has any of the following.
- Large-batch training that already pins compute or memory bandwidth
- Tight memory usage close to the GPU's capacity
- A deadline or a need for early results (sharing delays every job's completion)

To share, `--mem` is what your job needs; pasar adds max(2 GiB, 10%) on top. Going over is tolerated until
the machine runs short of memory, then the job furthest over its limit is stopped. `--time` is used
to plan the queue; overrunning is fine.

## 6. Cloud jobs checkpoint more, not less

A job submitted with `--on <target>` (see [the agent guide](../src/pasar/agents.md#cloud-jobs))
follows everything above, with the stakes raised: it is paused when it reaches its approved run
time, the provider can reclaim the GPU under it without warning, and every minute of lost work
was paid for. Checkpoint on time, not on steps, and checkpoint more often than you would locally:

- **Checkpoint every 10 to 15 minutes**, not every 30 as in step 2 above. A step count tuned on
  the local GPU can be much too far apart on a faster rented one, or much too close on a slower
  one — measure the interval in wall-clock time.
- **Save the first checkpoint early**, within the first few minutes of useful work, so an early
  pause or reclaim doesn't lose the time spent starting up (building the image, loading data,
  compiling).
- **Time your save once and double it for `--grace`.** Writes to storage outside the container
  are usually slower than a local job's writes to local disk.
- **Report every checkpoint and resume**, as in step 4. `progress` reports are also what pasar
  uses to tell whether a running job is on pace to need more time than its approval bought — that
  needs at least 3 progress reports as well as 5 minutes of them, so a job with a short approved
  window and a slow reporting interval may hit its pause before the warning has enough to go on.
  Reporting `progress` every minute or so keeps well clear of that.
- **Keep only the last two checkpoints.** Stored checkpoints cost money too; a second one just
  covers a save interrupted halfway.
- **Test resuming locally first.** Run a few minutes on the local GPU, cancel, and resubmit. A
  cloud run is an expensive place to find out resuming is broken.

A cloud job is paused, not killed, when it reaches the run time somebody approved: the next
attempt starts from this directory, or from nothing if it never wrote to it.

```python
import os, pasar_job

ckpt = os.path.join(pasar_job.persist_dir(), "last.pt")
if pasar_job.resuming() and os.path.exists(ckpt):
    step = load(ckpt)
    pasar_job.resumed(step)
...
save(ckpt); pasar_job.checkpoint(step)
```

`pasar_job.persist_dir()` is a directory shared by every attempt of this job — a cloud volume
mounted at `$PASAR_PERSIST_DIR`, or locally your job's own directory (`$PASAR_JOB_DIR`), or the
working directory outside pasar entirely. Write checkpoints there, not to a path you invent
yourself, so the attempt after a pause or a reclaim can find them.

## Environment

| Variable | Meaning |
|---|---|
| `PASAR_JOB_ID` | job ID |
| `PASAR_ATTEMPT` | attempt number, from 1 |
| `PASAR_RESUMING` | `1` if an earlier attempt ran |
| `PASAR_EVENTS` | file to append events to |
| `PASAR_JOB_DIR` | the job's directory in pasar's data dir |
| `PASAR_PERSIST_DIR` | cloud only: the job's persist volume — see `pasar_job.persist_dir()` above |
| `PASAR_MEM_LIMIT_BYTES` | shared jobs: your limit |
| `PASAR_GRACE_SECONDS` | time between SIGTERM and SIGKILL |

Working from a checkout instead? Install the local copy in editable mode:

    uv pip install -e packages/pasar-job
