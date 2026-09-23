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
    them costs. The GPU rate alone understates the job; an unpriced GPU or a rate table missing
    the sandbox's own rates raises `KeyError` rather than quietly pricing that part at zero."""
    gpu_key = f"gpu_hour_cost_{gpu.lower()}"
    if gpu_key not in rates:
        raise KeyError(f"no rate for GPU {gpu!r}; refusing to estimate blind")
    if "cpu_hour_cost_sandbox" not in rates or "mem_gib_hour_cost_sandbox" not in rates:
        raise KeyError("rate table is missing the sandbox's CPU or memory rate")
    return (float(count) * float(rates[gpu_key]) + cpu * float(rates["cpu_hour_cost_sandbox"])
            + mem_gib * float(rates["mem_gib_hour_cost_sandbox"]))


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

    def committed(self, target: str) -> float:
        """Dollars still to come from `target`'s running jobs: each one's recorded estimate,
        less the share its elapsed time has already used up. A job already past its own
        estimate contributes nothing further here — its estimate has already been spent as far
        as this number is concerned, and letting it go negative would let one overrunning job
        buy headroom for approving another."""
        total = 0.0
        for row in self.store.cloud_spend(target):
            if row["billed"] is not None:
                continue
            job = self.store.get_job(row["job_id"])
            if job is None:
                continue
            attempt = self.store.current_attempt(row["job_id"])
            if attempt is None or attempt.n != row["attempt"] or attempt.end_time is not None:
                continue
            est_runtime = job.spec.est_runtime
            if est_runtime <= 0:
                continue
            elapsed = self.clock() - attempt.start_time
            remaining_frac = max(0.0, 1 - elapsed / est_runtime)
            total += row["estimated"] * remaining_frac
        return total

    def spent_day(self, target: str) -> float:
        rows = self.store.cloud_spend_on(target, self._day(self.clock()))
        return sum(self._effective(r) for r in rows)

    def spent_month(self, target: str) -> float:
        rows = self.store.cloud_spend_in_month(target, self._day(self.clock())[:7])
        return sum(self._effective(r) for r in rows)

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
        return dt.datetime.fromtimestamp(ts, tz=dt.UTC).strftime("%Y-%m-%d")
