# Writing jobs for pasar

pasar can stop your job at any time to make room for a higher bid, then start it again later
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

## 5. Estimate memory and time

`--mem` is what your job needs; pasar adds max(2 GiB, 10%) on top. Going over is tolerated until
the machine runs short of memory, then the job furthest over its limit is stopped. `--time` is used
to plan the queue; overrunning is fine.

## Environment

| Variable | Meaning |
|---|---|
| `PASAR_JOB_ID` | job ID |
| `PASAR_ATTEMPT` | attempt number, from 1 |
| `PASAR_RESUMING` | `1` if an earlier attempt ran |
| `PASAR_EVENTS` | file to append events to |
| `PASAR_JOB_DIR` | the job's directory in pasar's data dir |
| `PASAR_MEM_LIMIT_BYTES` | shared jobs: your limit |
| `PASAR_GRACE_SECONDS` | time between SIGTERM and SIGKILL |

Working from a checkout instead? Install the local copy in editable mode:

    uv pip install -e packages/pasar-job
