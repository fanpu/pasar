// Mirrors src/pasar/views.py, src/pasar/db.py (_event, metric_summaries), src/pasar/api.py
// (SubmitBody, RestartBody). The Python code is the source of truth.

export type JobState =
  | "queued" | "running" | "stopping" | "completed" | "failed" | "cancelled"
  | "awaiting"; // cloud only: submitted, waiting for a person to approve the cost
export type EndKind = "completed" | "failed" | "preempted" | "cancelled"
  | "paused"; // cloud only: stopped at its approved run time or taken back; resumes on approval
export type Span = [start: number, end: number | null, endKind: EndKind | null];

/** `persist_view` in views.py: what a finished cloud job left in its persist dir, where it went,
 * and when what is still at the provider goes. `null` until the job is terminal. */
export interface CloudPersist {
  files: number | null; bytes: number | null; pulled_at: number | null; pulled_to: string | null;
  remote_deleted: boolean; remote_bytes: number | null;
  swept_at: number | null; swept_bytes: number | null; sweeps_at: number | null;
  last_error: string | null;
}

/** `cloud_view` in views.py: the `cloud` object embedded in a cloud job's `JobView`; `null` for a
 * job that ran locally. */
export interface CloudJob {
  target: string; gpu: string;
  phase: "pending" | "starting" | "running" | "success" | "exit-code" | "stopped" | "reclaimed"
       | "time_limit" | "signal" | null;
  estimated_cost: number | null; max_cost: number | null; user_capped: boolean;
  approved_seconds: number | null; full_seconds: number | null;
  job_cap: number | null;
  // Whose credit this runs on, and which group (if any) its target belongs to; both `null` once
  // the target is no longer configured, like `job_cap`.
  owner: string | null; group: string | null;
  job_spent: number; console_url: string | null;
  needs_more_time: number | null;
  // Why an approved, queued job hasn't launched yet, if the last scheduling pass said.
  blocked: "budget" | "concurrency" | null;
  persist: CloudPersist | null;
}

export interface JobView {
  id: number; name: string; state: JobState; reason: string | null; summary: string;
  stop_requested: "preempt" | "cancel" | null; bid: number; command: string; cwd: string;
  note: string; tags: string[]; submitter: string; git_commit: string | null;
  mode: "whole" | "shared"; mem_request: number | null; limit: number; usage: number | null;
  over_limit: boolean; peak: number; est_runtime: number; run_time: number; remaining: number;
  /** run_time + remaining; from progress reports when `eta_source` is "progress". */
  expected_runtime: number; eta_source: "progress" | "estimate";
  preemptible: boolean; preempt: boolean; grace: number; retries: number; retries_used: number;
  submit_time: number; queue_time: number; start_time: number | null; end_time: number | null;
  attempts: number; preemptions: number;
  lost: { preemption: number; failure: number; known: boolean };
  progress: { step: number | null; total_steps: number | null; ts: number } | null;
  last_checkpoint: { step: number | null; ts: number } | null;
  projected: [number, number][];
  spans: Span[];
  cloud: CloudJob | null;
}

export interface AttemptView {
  job_id: number; n: number; unit: string; start_time: number; end_time: number | null;
  end_kind: EndKind | null; exit_code: number | null; signal: string | null;
  reason: string | null; summary: string; log_tail: string; peak_mem: number;
  wasted_work: number | null; restart_cost: number | null;
}

export interface JobDetail extends Omit<JobView, "attempts"> { attempts: AttemptView[] }

export interface MachineEvent { ts: number; kind: string; text: string }

export interface StatusView {
  now: number; version: number; mem_total: number; mem_available: number;
  psi_some_avg10: number | null; pool: number; reserved: number; external: number; free: number;
  pressure_since: number | null; blocked: number[]; waiting: number[];
  machine_events: MachineEvent[]; hot_temp_c: number; grafana_url: string | null;
}

/** One GPU a cloud target can be asked for (`Daemon.cloud_gpus`): the name `--gpu` accepts, its
 * live $/hour, and its memory in GB — `null` when the provider's memory table doesn't know it. */
export interface CloudGpu { name: string; hourly_rate: number; memory_gb: number | null }

/** One configured cloud target, as `cloud_status_view` and `GET /api/cloud` show it. */
export interface CloudTarget {
  name: string; provider: string; configured: boolean;
  owner: string; // whose account pays for this target
  group: string; // the group (if any) this target belongs to; "" when it has none
  daily_budget: number; monthly_budget: number;
  spent_today: number; spent_month: number; committed: number;
  max_running: number; max_job_cost: number; running: number;
  // The smaller of what the daily and the monthly budget still allow, and whether that is <= 0
  // -- the same two figures the budget gate checks a launch against. pasar's own ledger.
  left: number; budget_exhausted: boolean;
  // The provider's own books, as pasard last read them (unix seconds for `credit_as_of`); all
  // `null` when unknown -- a provider that cannot say, or a read not made yet or that failed.
  credit_used: number | null; credit_exhausted: boolean | null; credit_as_of: number | null;
  rates: Record<string, number>;
  known_stored_bytes: number; known_stored_jobs: number;
  gpus: CloudGpu[];
}

/** The `cloud` block on the stream snapshot: `null` when no cloud target is configured. */
export interface CloudBlock {
  targets: CloudTarget[];
  awaiting: JobView[];   // State.AWAITING cloud jobs
  needs_time: JobView[]; // running cloud jobs past their approved pace
  recent: JobView[];     // up to 5 most recently finished cloud jobs
}

export interface Snapshot { status: StatusView; jobs: JobView[]; cloud: CloudBlock | null }

export interface JobEvent {
  attempt: number; ts: number; kind: "checkpoint" | "resumed" | "progress" | "note";
  step: number | null; payload: Record<string, unknown>;
}

export interface MetricSummary { attempt: number; metric: string; avg: number | null; max: number | null; total: number | null }
export type Series = [number, number][];
/** One job's dashboard-row sparkline: the metric pasar picked for it, thinned for row size. */
export interface JobSpark { key: string; points: Series; latest: number }
export type SparkMap = Record<number, JobSpark>;
export interface GpuSeries { power_w: Series; temp_c: Series; util_pct: Series }
export type JobSpriteKind = "local" | "cloud";
export type JobSpriteState =
  | "running" | "starting" | "queued" | "awaiting" | "over" | "stopping" | "preempted"
  | "paused" | "completed" | "failed" | "cancelled" | "idle";
/** `job_manifest` in mascot.py: per-job sprites, kind first (there's no crossing from one to the
 * other), then state; absent states simply have no entry, since — unlike the mood art — there is
 * no built-in fallback to draw on. */
export type JobSpriteManifest = Record<JobSpriteKind, Partial<Record<JobSpriteState, string[]>>>;

// Keyed by MascotState, plus "peek" (a single-URL array) and "jobs" (a JobSpriteManifest, not a
// plain URL list — callers that index a mood/peek key should keep treating the result as
// `string[]`, since only "jobs" itself has the other shape).
export interface MascotManifest {
  [key: string]: string[] | JobSpriteManifest;
}

export interface SubmitBody {
  command: string; time: string | number; cwd: string; mem?: string | number | null; bid?: number;
  preempt?: boolean; preemptible?: boolean; grace?: string | number | null; retries?: number; name?: string;
  note?: string; tags?: string[]; submitter?: string; env?: Record<string, string> | null;
}
export interface RestartBody {
  mem?: string | number | null; whole_gpu?: boolean; time?: string | number | null; bid?: number;
  retries?: number; preempt?: boolean;
}
