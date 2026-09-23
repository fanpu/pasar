"""Whether a running cloud job's own pace is on track to outrun the run time its approval
bought — from progress reports alone, projected the same way `pasar.eta` projects the schedule.

`--time` is a guess made on the local GPU; rented hardware can be several times faster or slower,
so a job can be on pace to overrun long before it is ever stopped. This module answers one
question — has this attempt reported enough of its own progress to say, with some confidence,
that it will finish after its approval runs out? — and leaves what to do about that answer (mark
the job, let a person extend it) to the daemon.
"""

from pasar.eta import remaining_time
from pasar.models import Attempt, Job

MIN_OBSERVATION = 300  # seconds of this attempt's own progress trusted before its pace is judged

MIN_REPORTS = 3
"""Progress reports this attempt must have made before any verdict is drawn from its pace.

Time alone is not enough evidence. One report is one data point, and a single bad one — a
reporting bug, a resume that restated the step count, `step=1` of `total_steps=1000000` an hour
in — is indistinguishable from a job genuinely crawling, yet it would drive both the warning and
the size of the extension somebody is asked to approve. Three is the smallest count that shows a
*cadence* rather than an event: two intervals between reports, so a job has to keep saying it is
behind before anyone is told that it is. It stays small deliberately — a job that reports once a
minute clears it well inside `MIN_OBSERVATION`, so in practice the time threshold is still what
binds for jobs that report at a sane rate, and this only bites the pathological ones.
"""


def needs_more_time(store, job: Job, atts: list[Attempt], now: float, approved_seconds: float,
                     min_observation: float = MIN_OBSERVATION,
                     min_reports: int = MIN_REPORTS) -> float | None:
    """Extra seconds this attempt's own pace projects past `approved_seconds`, or `None` when
    there is nothing to flag yet.

    `None` covers three different reasons a caller does not need to tell apart — it only ever
    needs to know whether to warn:

    - The attempt has not reported enough progress to be trusted yet: fewer than
      `min_observation` seconds have passed since its first progress report this attempt, or it
      has made fewer than `min_reports` reports at all (see `MIN_REPORTS`). A run that has barely
      started can swing wildly on one early, noisy report, and one report — however long ago it
      landed — is never a pace.
    - `eta.remaining_time` fell back to the `--time` estimate rather than the job's own progress
      (no `total_steps`, or no steps done yet this attempt). There is no pace of "its own" to
      project from, so there is nothing to warn about — warning from `--time` would just be the
      guess this exists to correct.
    - The projection lands at or under `approved_seconds`: the job is on pace, or ahead of it.

    The projection reuses `eta.remaining_time` — the same call the schedule itself makes for
    `pasar ls`/`show` and the projected end times — rather than a second implementation of the
    "elapsed x (total - step) / done" arithmetic, so the warning and the schedule can never
    quietly disagree about how fast a job is moving.

    Measured against the *attempt's* own start, not the job's cumulative run time across earlier
    attempts: an approval is per attempt (`Daemon.approve`), so what it buys is this attempt's own
    clock — the same one `Daemon._pause_overdue` measures against when it decides whether to stop
    the job.
    """
    if not atts or atts[-1].end_time is not None:
        return None
    att = atts[-1]
    first = store.first_event(job.id, "progress", att.n)
    if first is None or now - first["ts"] < min_observation:
        return None
    if store.count_events(job.id, "progress", att.n) < min_reports:
        return None
    left, source = remaining_time(store, job, atts, now)
    if source != "progress":
        return None
    overrun = (now - att.start_time) + left - approved_seconds
    return overrun if overrun > 0 else None
