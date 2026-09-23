// Mirrors src/pasar/views.py, src/pasar/db.py (_event, metric_summaries), src/pasar/api.py
// (SubmitBody, RestartBody). The Python code is the source of truth.

export type JobState = "queued" | "running" | "stopping" | "completed" | "failed" | "cancelled";
export type EndKind = "completed" | "failed" | "preempted" | "cancelled";
export type Span = [start: number, end: number | null, endKind: EndKind | null];

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

export interface Snapshot { status: StatusView; jobs: JobView[] }

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
export type MascotManifest = Record<string, string[]>;

export interface SubmitBody {
  command: string; time: string | number; cwd: string; mem?: string | number | null; bid?: number;
  preempt?: boolean; preemptible?: boolean; grace?: string | number | null; retries?: number; name?: string;
  note?: string; tags?: string[]; submitter?: string; env?: Record<string, string> | null;
}
export interface RestartBody {
  mem?: string | number | null; whole_gpu?: boolean; time?: string | number | null; bid?: number;
  retries?: number; preempt?: boolean;
}
