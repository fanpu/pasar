"""What a cloud job is likely to cost, and the ledger the budget gate reads before it lets one
run: money already spent, and money already promised by jobs still running.

Rates come from a live provider and are trusted at face value; everything here is `float`, to
match `CloudTarget.rates` and `Provider.rates()` and because a provider's numbers are not
guaranteed to arrive as `Decimal` (Modal's SDK does, the fake does not) — coercing at the one
place a rate is read keeps that difference from leaking into every caller, and a cost estimate
was never meant to be exact to the cent.
"""

import datetime as dt
from collections.abc import Callable


def hourly_rate(rates: dict, gpu: str, count: int, cpu: float = 4.0, mem_gib: float = 32.0) -> float:
    """Dollars per hour for `count` GPUs of `gpu` plus the CPU and memory the sandbox around
    them costs. The GPU rate alone understates the job; an unpriced GPU, a rate table missing
    the sandbox's own rates, or a rate that is zero or negative all raise `KeyError` rather than
    quietly pricing that part at zero or less — nothing gets approved against a price that
    cannot be real."""
    gpu_key = f"gpu_hour_cost_{gpu.lower()}"
    if gpu_key not in rates:
        raise KeyError(f"no rate for GPU {gpu!r}; refusing to estimate blind")
    if "cpu_hour_cost_sandbox" not in rates or "mem_gib_hour_cost_sandbox" not in rates:
        raise KeyError("rate table is missing the sandbox's CPU or memory rate")
    gpu_rate = float(rates[gpu_key])
    cpu_rate = float(rates["cpu_hour_cost_sandbox"])
    mem_rate = float(rates["mem_gib_hour_cost_sandbox"])
    if gpu_rate <= 0 or cpu_rate <= 0 or mem_rate <= 0:
        raise KeyError(f"non-positive rate for {gpu!r}; refusing to estimate against a price "
                       "that cannot be real")
    return float(count) * gpu_rate + cpu * cpu_rate + mem_gib * mem_rate


def estimate(rate: float, seconds: float) -> float:
    """Dollars for running at `rate` per hour for `seconds`; a non-positive duration costs
    nothing rather than going negative."""
    return rate * max(0.0, seconds) / 3600


class Ledger:
    """The spend `cloud_spend` table, read and written through one clock so every caller agrees
    on what day it is."""

    def __init__(self, store, clock: Callable[[], float]):
        self.store = store
        self.clock = clock

    def _open(self, row: dict) -> bool:
        """True if `row` is still an actively running, unbilled attempt — still billing, so its
        cost belongs in `committed()`, not in `settled_day`/`settled_month`. A row is closed
        (not open) once it is billed, once its job or attempt no longer exists, or once the
        attempt it names has been superseded by a retry or has ended."""
        if row["billed"] is not None:
            return False
        job = self.store.get_job(row["job_id"])
        if job is None:
            return False
        attempt = self.store.current_attempt(row["job_id"])
        return attempt is not None and attempt.n == row["attempt"] and attempt.end_time is None

    def committed(self, target: str, replacing: tuple[int, int, float] | None = None) -> float:
        """Dollars still to come from `target`'s running jobs. Each running attempt contributes
        at least its recorded estimate — it is still billing, so its estimate is never money
        already accounted for — and more once elapsed time against its own runtime estimate
        says it has run past that: `elapsed/est_runtime` is the best proxy this has for money
        already burnt, and a spend gate that under-counts a runaway job is worse than one that
        over-counts a job about to finish on time. A job with no useful pace information (a
        non-positive `est_runtime`) contributes its full estimate rather than nothing.

        `replacing` is `(job_id, attempt, estimated)`: price that one attempt at the given figure
        instead of the one on its row, so a caller can ask what the total *would* be before
        writing anything. Raising a running attempt's ceiling replaces that attempt's commitment
        rather than adding a second one beside it, so adding the new figure to `committed()`
        would count the attempt twice and refuse extensions the budget could well afford."""
        total = 0.0
        for row in self.store.cloud_spend(target):
            if not self._open(row):
                continue
            estimated = row["estimated"]
            if replacing is not None and (row["job_id"], row["attempt"]) == replacing[:2]:
                estimated = replacing[2]
            job = self.store.get_job(row["job_id"])
            attempt = self.store.current_attempt(row["job_id"])
            est_runtime = job.spec.est_runtime
            if est_runtime <= 0:
                total += estimated
                continue
            elapsed = self.clock() - attempt.start_time
            total += max(estimated, estimated * elapsed / est_runtime)
        return total

    def spent_day(self, target: str) -> float:
        """Dollars recorded for today, billed or not: an open attempt still counts here at its
        flat estimate. That overlaps `committed()`, which prices the same open attempts again
        (at or above their estimate) — add the two together and an open attempt is counted
        twice. Use `settled_day()` alongside `committed()` instead; use this alone only when
        nothing else in the sum touches `committed()`."""
        rows = self.store.cloud_spend_on(target, self._day(self.clock()))
        return sum(self._effective(r) for r in rows)

    def spent_month(self, target: str) -> float:
        """Dollars recorded for this month, billed or not — see `spent_day()`'s docstring: it
        double-counts against `committed()` the same way, and `settled_month()` is the one to
        pair with it."""
        rows = self.store.cloud_spend_in_month(target, self._day(self.clock())[:7])
        return sum(self._effective(r) for r in rows)

    def settled_day(self, target: str) -> float:
        """Dollars for today from attempts that are no longer open — billed rows at their billed
        figure, and any other closed row at its estimate — excluding every attempt `committed()`
        is still pricing. `settled_day(target) + committed(target)` counts each of today's
        attempts, open or closed, exactly once; this is what a budget gate should sum against
        `daily_budget`, not `spent_day()`."""
        rows = self.store.cloud_spend_on(target, self._day(self.clock()))
        return sum(self._effective(r) for r in rows if not self._open(r))

    def settled_month(self, target: str) -> float:
        """The month equivalent of `settled_day()`: pair with `committed()` against
        `monthly_budget`, not `spent_month()`."""
        rows = self.store.cloud_spend_in_month(target, self._day(self.clock())[:7])
        return sum(self._effective(r) for r in rows if not self._open(r))

    def record(self, target: str, job_id: int, attempt: int, estimated: float,
               billed: float | None = None) -> None:
        """Save (or refresh) one attempt's cost. Called first with just the estimate, at launch,
        so it counts toward spend before the job finishes; called again with `billed` once the
        provider's own figure is known, which then stands in for the estimate everywhere this
        row is totalled. The row's day is set once, at its first call, and never moves — a bill
        that arrives late still belongs to the day the job ran, not the day it was billed."""
        self.store.record_cloud_spend(target, job_id, attempt, self._day(self.clock()),
                                      estimated, billed)

    @staticmethod
    def _effective(row: dict) -> float:
        return row["billed"] if row["billed"] is not None else row["estimated"]

    @staticmethod
    def _day(ts: float) -> str:
        """Local calendar day: `daily_budget` is a human-facing cap, and resetting it at UTC
        midnight would reset it mid-evening for anyone west of Greenwich."""
        return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")  # noqa: DTZ006
