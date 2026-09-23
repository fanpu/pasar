"""SQLite persistence for jobs, attempts, events, and machine history."""

import json
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import fields
from pathlib import Path

from pasar.models import Attempt, EndKind, Job, JobSpec, State

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    spec TEXT NOT NULL,
    state TEXT NOT NULL,
    bid INTEGER NOT NULL,
    queue_time REAL NOT NULL,
    submit_time REAL NOT NULL,
    retries_used INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    summary TEXT NOT NULL DEFAULT '',
    git_commit TEXT,
    stop_requested TEXT,
    events_offset INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
CREATE TABLE IF NOT EXISTS attempts (
    job_id INTEGER NOT NULL,
    n INTEGER NOT NULL,
    unit TEXT NOT NULL,
    start_time REAL NOT NULL,
    end_time REAL,
    end_kind TEXT,
    exit_code INTEGER,
    signal TEXT,
    reason TEXT,
    summary TEXT NOT NULL DEFAULT '',
    log_tail TEXT NOT NULL DEFAULT '',
    peak_mem INTEGER NOT NULL DEFAULT 0,
    wasted_work REAL,
    restart_cost REAL,
    PRIMARY KEY (job_id, n)
);
CREATE INDEX IF NOT EXISTS attempts_start ON attempts(start_time);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    step INTEGER,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id, kind);
CREATE TABLE IF NOT EXISTS metric_summaries (
    job_id INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    metric TEXT NOT NULL,
    avg REAL,
    max REAL,
    total REAL,
    PRIMARY KEY (job_id, attempt, metric)
);
CREATE TABLE IF NOT EXISTS machine_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gpu_samples (
    job_id INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    ts REAL NOT NULL,
    gpu INTEGER NOT NULL,
    util REAL, mem_used REAL, mem_total REAL, power REAL, temp REAL
);
CREATE INDEX IF NOT EXISTS gpu_samples_job ON gpu_samples(job_id, attempt, ts);
CREATE TABLE IF NOT EXISTS cloud_spend (
    target TEXT NOT NULL,
    job_id INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    day TEXT NOT NULL,
    estimated REAL NOT NULL,
    billed REAL,
    PRIMARY KEY (target, job_id, attempt)
);
CREATE INDEX IF NOT EXISTS cloud_spend_day ON cloud_spend(target, day);
CREATE TABLE IF NOT EXISTS approvals (
    job_id INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    ts REAL NOT NULL,
    estimated_cost REAL,
    max_cost REAL,
    hourly_rate REAL,
    PRIMARY KEY (job_id, attempt)
);
CREATE TABLE IF NOT EXISTS pulls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    dest TEXT,
    files INTEGER,
    bytes INTEGER,
    remote_deleted INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    auto INTEGER NOT NULL DEFAULT 0,
    remote_files INTEGER,
    remote_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS pulls_job ON pulls(job_id);
CREATE TABLE IF NOT EXISTS cloud_finished (
    job_id INTEGER PRIMARY KEY,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cloud_swept (
    job_id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    files INTEGER NOT NULL,
    bytes INTEGER NOT NULL
);
"""

_JOB_UPDATABLE = {"spec", "state", "bid", "queue_time", "retries_used", "reason", "summary",
                  "stop_requested", "events_offset"}
_ATTEMPT_COLUMNS = [f.name for f in fields(Attempt)]


class Store:
    def __init__(self, path: Path | str):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._add_columns("pulls", {"remote_files": "INTEGER", "remote_bytes": "INTEGER"})

    def _add_columns(self, table: str, columns: dict[str, str]) -> None:
        """Add columns a newer schema gave an existing table: `CREATE TABLE IF NOT EXISTS`
        leaves a table made by an older pasard exactly as it was."""
        have = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns.items():
            if name not in have:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            return self._db.execute(sql, args).lastrowid

    # jobs
    def insert_job(self, spec: JobSpec, bid: int, now: float, git_commit: str | None) -> int:
        return self._x(
            "INSERT INTO jobs (spec, state, bid, queue_time, submit_time, git_commit)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (spec.to_json(), State.QUEUED.value, bid, now, now, git_commit),
        )

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"], spec=JobSpec.from_json(row["spec"]), state=State(row["state"]),
            bid=row["bid"], queue_time=row["queue_time"], submit_time=row["submit_time"],
            retries_used=row["retries_used"], reason=row["reason"], summary=row["summary"],
            git_commit=row["git_commit"], stop_requested=row["stop_requested"],
            events_offset=row["events_offset"],
        )

    def get_job(self, job_id: int) -> Job | None:
        rows = self._q("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return self._job(rows[0]) if rows else None

    def list_jobs(self, states: Iterable[State] | None = None) -> list[Job]:
        if states is None:
            rows = self._q("SELECT * FROM jobs ORDER BY id")
        else:
            vals = [State(s).value for s in states]
            marks = ",".join("?" * len(vals))
            rows = self._q(f"SELECT * FROM jobs WHERE state IN ({marks}) ORDER BY id", tuple(vals))
        return [self._job(r) for r in rows]

    def update_job(self, job_id: int, **values) -> None:
        bad = set(values) - _JOB_UPDATABLE
        if bad:
            raise ValueError(f"cannot update job fields: {sorted(bad)}")
        if "spec" in values:
            values["spec"] = values["spec"].to_json()
        if "state" in values:
            values["state"] = State(values["state"]).value
        cols = ", ".join(f"{k} = ?" for k in values)
        self._x(f"UPDATE jobs SET {cols} WHERE id = ?", (*values.values(), job_id))

    # attempts
    def insert_attempt(self, a: Attempt) -> None:
        vals = [getattr(a, c) for c in _ATTEMPT_COLUMNS]
        vals = [v.value if isinstance(v, EndKind) else v for v in vals]
        marks = ",".join("?" * len(vals))
        self._x(f"INSERT INTO attempts ({', '.join(_ATTEMPT_COLUMNS)}) VALUES ({marks})", tuple(vals))

    def update_attempt(self, job_id: int, n: int, **values) -> None:
        bad = set(values) - set(_ATTEMPT_COLUMNS) - {"job_id", "n"}
        if bad or not values:
            raise ValueError(f"cannot update attempt fields: {sorted(bad)}")
        vals = [v.value if isinstance(v, EndKind) else v for v in values.values()]
        cols = ", ".join(f"{k} = ?" for k in values)
        self._x(f"UPDATE attempts SET {cols} WHERE job_id = ? AND n = ?", (*vals, job_id, n))

    @staticmethod
    def _attempt(row: sqlite3.Row) -> Attempt:
        d = dict(row)
        d["end_kind"] = EndKind(d["end_kind"]) if d["end_kind"] else None
        return Attempt(**d)

    def attempts(self, job_id: int) -> list[Attempt]:
        rows = self._q("SELECT * FROM attempts WHERE job_id = ? ORDER BY n", (job_id,))
        return [self._attempt(r) for r in rows]

    def job_ids_active_between(self, since: float, until: float) -> list[int]:
        """Jobs with an attempt overlapping `[since, until)` (a running attempt has no end yet)."""
        rows = self._q("SELECT DISTINCT job_id FROM attempts WHERE start_time < ? "
                       "AND (end_time IS NULL OR end_time > ?) ORDER BY job_id", (until, since))
        return [r["job_id"] for r in rows]

    def current_attempt(self, job_id: int) -> Attempt | None:
        rows = self._q("SELECT * FROM attempts WHERE job_id = ? ORDER BY n DESC LIMIT 1", (job_id,))
        return self._attempt(rows[0]) if rows else None

    # events
    def add_event(self, job_id: int, attempt: int, ts: float, kind: str, step: int | None,
                  payload: dict) -> None:
        self._x(
            "INSERT INTO events (job_id, attempt, ts, kind, step, payload) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, attempt, ts, kind, step, json.dumps(payload)),
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> dict:
        return {"attempt": row["attempt"], "ts": row["ts"], "kind": row["kind"],
                "step": row["step"], "payload": json.loads(row["payload"])}

    def events(self, job_id: int) -> list[dict]:
        rows = self._q("SELECT * FROM events WHERE job_id = ? ORDER BY id", (job_id,))
        return [self._event(r) for r in rows]

    def progress_events(self, job_ids: Iterable[int]) -> dict[int, list[dict]]:
        """Every job's progress events, in order, from one query — the dashboard needs these for
        many jobs at once and a per-job round trip would be one query per visible row. Jobs with
        no progress events are left out of the result entirely."""
        ids = list(job_ids)
        if not ids:
            return {}
        holes = ",".join("?" * len(ids))
        rows = self._q(f"SELECT * FROM events WHERE job_id IN ({holes}) AND kind = 'progress' "
                       "ORDER BY id", tuple(ids))
        out: dict[int, list[dict]] = {}
        for r in rows:
            out.setdefault(r["job_id"], []).append(self._event(r))
        return out

    def last_event(self, job_id: int, kind: str, attempt: int | None = None) -> dict | None:
        sql = "SELECT * FROM events WHERE job_id = ? AND kind = ?"
        args: tuple = (job_id, kind)
        if attempt is not None:
            sql += " AND attempt = ?"
            args += (attempt,)
        rows = self._q(sql + " ORDER BY id DESC LIMIT 1", args)
        return self._event(rows[0]) if rows else None

    def first_event(self, job_id: int, kind: str, attempt: int) -> dict | None:
        rows = self._q("SELECT * FROM events WHERE job_id = ? AND kind = ? AND attempt = ? "
                       "ORDER BY id LIMIT 1", (job_id, kind, attempt))
        return self._event(rows[0]) if rows else None

    def count_events(self, job_id: int, kind: str, attempt: int) -> int:
        """How many `kind` events this attempt has reported. Counted in SQL rather than by
        reading the rows: the only caller wants the number, and an attempt can report thousands."""
        rows = self._q("SELECT COUNT(*) AS n FROM events WHERE job_id = ? AND kind = ? "
                       "AND attempt = ?", (job_id, kind, attempt))
        return rows[0]["n"]

    def last_event_before(self, job_id: int, kind: str, attempt: int) -> dict | None:
        """The latest `kind` event from an attempt earlier than `attempt`."""
        rows = self._q("SELECT * FROM events WHERE job_id = ? AND kind = ? AND attempt < ? "
                       "ORDER BY id DESC LIMIT 1", (job_id, kind, attempt))
        return self._event(rows[0]) if rows else None

    def has_events(self, job_id: int) -> bool:
        return bool(self._q("SELECT 1 FROM events WHERE job_id = ? LIMIT 1", (job_id,)))

    # machine history
    def add_machine_event(self, ts: float, kind: str, text: str) -> None:
        self._x("INSERT INTO machine_events (ts, kind, text) VALUES (?, ?, ?)", (ts, kind, text))

    def machine_events(self, limit: int = 50) -> list[dict]:
        rows = self._q("SELECT ts, kind, text FROM machine_events ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # metric summaries
    def set_metric_summary(self, job_id: int, attempt: int, metric: str, avg: float | None,
                           max: float | None, total: float | None) -> None:
        self._x(
            "INSERT OR REPLACE INTO metric_summaries (job_id, attempt, metric, avg, max, total)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, attempt, metric, avg, max, total),
        )

    def metric_summaries(self, job_id: int) -> list[dict]:
        rows = self._q(
            "SELECT attempt, metric, avg, max, total FROM metric_summaries WHERE job_id = ?"
            " ORDER BY attempt, metric",
            (job_id,),
        )
        return [dict(r) for r in rows]

    # gpu samples
    def add_gpu_samples(self, job_id: int, attempt: int, ts: float, rows: list[list[float]]) -> None:
        with self._lock:
            self._db.executemany(
                "INSERT INTO gpu_samples (job_id, attempt, ts, gpu, util, mem_used, mem_total, "
                "power, temp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(job_id, attempt, ts, int(r[0]), *r[1:]) for r in rows],
            )

    def gpu_samples(self, job_id: int, attempt: int) -> list[tuple]:
        rows = self._q(
            "SELECT ts, gpu, util, mem_used, mem_total, power, temp FROM gpu_samples "
            "WHERE job_id = ? AND attempt = ? ORDER BY ts",
            (job_id, attempt),
        )
        return [tuple(r) for r in rows]

    # cloud spend
    def record_cloud_spend(self, target: str, job_id: int, attempt: int, day: str,
                           estimated: float, billed: float | None) -> None:
        """Insert one attempt's cost row, or refresh its figures if it already has one. `day`
        only takes effect on the first call for this (target, job_id, attempt): it is left out
        of the update so a later call cannot move a row to a different day. `billed` is sticky:
        once a real figure is recorded, a later call made with `billed=None` (a re-run of
        launch-time bookkeeping, say, after a restart) must not erase it and fall back to the
        estimate."""
        self._x(
            "INSERT INTO cloud_spend (target, job_id, attempt, day, estimated, billed)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(target, job_id, attempt)"
            " DO UPDATE SET estimated = excluded.estimated,"
            " billed = COALESCE(excluded.billed, cloud_spend.billed)",
            (target, job_id, attempt, day, estimated, billed),
        )

    def settle_cloud_spend(self, target: str, job_id: int, attempt: int, billed: float) -> None:
        """Record what an attempt really cost, leaving its estimate where it is so the two can
        still be compared. Does nothing if the attempt has no row: an attempt that never got as
        far as being priced never spent anything either."""
        self._x(
            "UPDATE cloud_spend SET billed = ?"
            " WHERE target = ? AND job_id = ? AND attempt = ?",
            (billed, target, job_id, attempt),
        )

    # approvals
    def add_approval(self, job_id: int, attempt: int, ts: float, estimated_cost: float | None,
                     max_cost: float | None, hourly_rate: float | None = None) -> None:
        """What this attempt was approved to cost: the estimate it was approved against, the
        ceiling that approval buys (the most it may bill, including the submitter's own
        `--max-cost` when that is the lower figure), and the price per hour it was approved at.

        The rate is stored as well as the ceiling because the two answer different questions: the
        ceiling is what this attempt may spend, the rate is what the market was charging when
        somebody said yes. A job whose `--max-cost` binds has a ceiling that cannot move — it is
        the submitter's own number — so only the rate can show that the price has risen since.

        One row per attempt: a paused or reclaimed job needs approving again, so each attempt has
        its own. No approver is recorded — pasard has no authentication, so there is nobody to
        name."""
        self._x("INSERT OR REPLACE INTO approvals (job_id, attempt, ts, estimated_cost, max_cost,"
                " hourly_rate) VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, attempt, ts, estimated_cost, max_cost, hourly_rate))

    def approvals(self, job_id: int) -> list[dict]:
        rows = self._q("SELECT * FROM approvals WHERE job_id = ? ORDER BY attempt", (job_id,))
        return [dict(r) for r in rows]

    def cloud_spend(self, target: str) -> list[dict]:
        rows = self._q("SELECT * FROM cloud_spend WHERE target = ?", (target,))
        return [dict(r) for r in rows]

    def cloud_spend_on(self, target: str, day: str) -> list[dict]:
        rows = self._q("SELECT * FROM cloud_spend WHERE target = ? AND day = ?", (target, day))
        return [dict(r) for r in rows]

    def cloud_spend_in_month(self, target: str, month: str) -> list[dict]:
        rows = self._q("SELECT * FROM cloud_spend WHERE target = ? AND day LIKE ?",
                       (target, month + "-%"))
        return [dict(r) for r in rows]

    def cloud_spend_of_job(self, job_id: int) -> list[dict]:
        """Every attempt's cost row for one job, whichever target and day it fell on: what a
        job's lifetime cap is measured against."""
        rows = self._q("SELECT * FROM cloud_spend WHERE job_id = ? ORDER BY attempt", (job_id,))
        return [dict(r) for r in rows]

    # pulls
    def add_pull(self, job_id: int, ts: float, dest: str | None, files: int | None,
                 size: int | None, remote_deleted: bool, error: str | None,
                 auto: bool = False, remote_files: int | None = None,
                 remote_bytes: int | None = None) -> None:
        """One outcome of one pull of a cloud job's persist dir, `auto` for the automatic one.
        `files` and `size` (the `bytes` column) are what landed locally, verified — NULL when
        nothing did, which is how a refusal or a failure reads; 0 and 0 when the provider showed
        nothing to pull. `error` may be set on a row that did land: a verified local copy whose
        remote one could not then be deleted. `remote_files`/`remote_bytes` are what the
        provider said it held when the pull measured it, before fetching anything — recorded
        whether or not anything then landed, since a pull skipped for want of room is exactly
        the one whose results stay at the provider; NULL when it was refused before measuring."""
        self._x("INSERT INTO pulls (job_id, ts, dest, files, bytes, remote_deleted, error, auto,"
                " remote_files, remote_bytes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, ts, dest, files, size, int(remote_deleted), error, int(auto),
                 remote_files, remote_bytes))

    @staticmethod
    def _pull(row: sqlite3.Row) -> dict:
        d = dict(row)
        d.pop("id")
        d["remote_deleted"] = bool(d["remote_deleted"])
        d["auto"] = bool(d["auto"])
        return d

    def pulls(self, job_id: int) -> list[dict]:
        rows = self._q("SELECT * FROM pulls WHERE job_id = ? ORDER BY id", (job_id,))
        return [self._pull(r) for r in rows]

    def last_pull(self, job_id: int, landed: bool = False) -> dict | None:
        """The job's latest pull, or with `landed` its latest one that got a verified copy of
        at least one file onto local disk. The two differ: a manual pull refused for a bad
        `--to` after the automatic one succeeded is the latest pull, but not where the data is.
        A pull that found nothing has not landed anything: a volume listed straight after a
        sandbox exits may not show what it just wrote yet, so an empty answer is not final."""
        where = " AND files > 0" if landed else ""
        rows = self._q(f"SELECT * FROM pulls WHERE job_id = ?{where} ORDER BY id DESC LIMIT 1",
                       (job_id,))
        return self._pull(rows[0]) if rows else None

    def held_remote(self) -> dict[int, int]:
        """Bytes each finished cloud job is known to still hold at its provider: its latest
        pull's measurement, for every job whose remote copy that pull did not delete and no
        sweep has since. Records only, never the provider — so a floor: a job no pull has
        measured (a running or paused one, whose checkpoint is live) is not in it at all."""
        rows = self._q(
            "SELECT p.job_id, p.remote_bytes FROM pulls p JOIN"
            " (SELECT job_id, MAX(id) AS id FROM pulls WHERE remote_bytes IS NOT NULL"
            "  GROUP BY job_id) latest ON p.id = latest.id"
            " WHERE p.remote_deleted = 0 AND p.remote_bytes > 0"
            " AND p.job_id NOT IN (SELECT job_id FROM cloud_swept)")
        return {r["job_id"]: r["remote_bytes"] for r in rows}

    def count_pulls(self, job_id: int, auto: bool | None = None) -> int:
        """How many pulls of this job were recorded; `auto=True` counts only the automatic ones,
        which is what their retry cap is measured against."""
        where = "" if auto is None else " AND auto = ?"
        args: tuple = (job_id,) if auto is None else (job_id, int(auto))
        return self._q(f"SELECT COUNT(*) AS n FROM pulls WHERE job_id = ?{where}", args)[0]["n"]

    # when a cloud job finished
    def mark_cloud_finished(self, job_id: int, ts: float) -> None:
        """Stamp when a cloud job became terminal. The first stamp wins: being told again, by
        housekeep's backstop say, must not restart the clock its results are kept by."""
        self._x("INSERT OR IGNORE INTO cloud_finished (job_id, ts) VALUES (?, ?)", (job_id, ts))

    def cloud_finished_at(self, job_id: int) -> float | None:
        rows = self._q("SELECT ts FROM cloud_finished WHERE job_id = ?", (job_id,))
        return rows[0]["ts"] if rows else None

    def cloud_finished(self) -> dict[int, float]:
        """Every stamped cloud job and when it finished: what a job's results are aged by, both
        for retrying its pull and for how long a provider keeps them."""
        return {r["job_id"]: r["ts"] for r in self._q("SELECT job_id, ts FROM cloud_finished")}

    # what the retention sweep deleted at a provider
    def mark_cloud_swept(self, job_id: int, ts: float, files: int, size: int) -> None:
        """Record that a finished cloud job's persist dir was swept: `files` and `size` (the
        `bytes` column) are what was deleted, 0 and 0 when there was nothing left to delete. The
        first record wins, like `mark_cloud_finished`: a job is swept once and never again."""
        self._x("INSERT OR IGNORE INTO cloud_swept (job_id, ts, files, bytes) VALUES (?, ?, ?, ?)",
                (job_id, ts, files, size))

    def cloud_swept(self, job_id: int) -> dict | None:
        rows = self._q("SELECT * FROM cloud_swept WHERE job_id = ?", (job_id,))
        return dict(rows[0]) if rows else None

    def swept_jobs(self) -> set[int]:
        return {r["job_id"] for r in self._q("SELECT job_id FROM cloud_swept")}
