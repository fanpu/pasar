// Fixtures for the cloud block: `cloud_status_view`/`cloud_view`/`persist_view` in views.py, and
// the `cloud` field the stream snapshot gains alongside `jobs`. Builds on the plain job fixture
// in ../fixtures.ts; only the cloud-specific pieces live here.
import { GIB } from "../../lib/format";
import type { CloudBlock, CloudGpu, CloudJob, CloudPersist, CloudTarget, JobView } from "../../lib/types";
import { job as jobFixture, NOW } from "../fixtures";

export function cloudGpu(overrides: Partial<CloudGpu> = {}): CloudGpu {
  return { name: "H100", hourly_rate: 2.75, memory_gb: 80, ...overrides };
}

/** One configured cloud target, as `cloud_status_view` builds it. */
export function cloudTarget(overrides: Partial<CloudTarget> = {}): CloudTarget {
  return {
    name: "modal-a",
    provider: "modal",
    owner: "First Owner",
    group: "modal",
    configured: true,
    daily_budget: 30,
    monthly_budget: 30,
    spent_today: 6.4,
    spent_month: 18.2,
    committed: 1.35,
    max_running: 3,
    max_job_cost: 10,
    running: 2,
    left: 30 - 18.2 - 1.35,
    budget_exhausted: false,
    credit_used: null, credit_exhausted: null, credit_as_of: null,
    rates: {
      gpu_hour_cost_h100: 2.75,
      gpu_hour_cost_a100: 1.6,
      cpu_hour_cost_sandbox: 0.05,
      mem_gib_hour_cost_sandbox: 0.01,
    },
    known_stored_bytes: 3 * GIB,
    known_stored_jobs: 1,
    gpus: [cloudGpu(), cloudGpu({ name: "A100", hourly_rate: 1.6, memory_gb: 40 })],
    ...overrides,
  };
}

/** `persist_view`'s default: a job that hasn't finished, or finished and left nothing behind. */
export function cloudPersist(overrides: Partial<CloudPersist> = {}): CloudPersist {
  return {
    files: null,
    bytes: null,
    pulled_at: null,
    pulled_to: null,
    remote_deleted: false,
    remote_bytes: null,
    swept_at: null,
    swept_bytes: null,
    sweeps_at: null,
    last_error: null,
    ...overrides,
  };
}

/** `cloud_view`'s embedded object, defaulted for a queued-but-unapproved job on `modal-a`. */
export function cloudJob(overrides: Partial<CloudJob> = {}): CloudJob {
  return {
    target: "modal-a",
    gpu: "H100",
    phase: null,
    estimated_cost: 1.8,
    max_cost: 6,
    user_capped: false,
    approved_seconds: null,
    full_seconds: null,
    job_cap: 10,
    owner: "First Owner",
    group: "modal",
    job_spent: 0,
    console_url: null,
    needs_more_time: null,
    blocked: null,
    persist: null,
    ...overrides,
  };
}

/** A cloud job's `JobView`: the plain job fixture with `cloud` filled in. */
export function cloudJobView(overrides: Partial<JobView> = {}, cloudOverrides: Partial<CloudJob> = {}): JobView {
  return jobFixture({ ...overrides, cloud: cloudJob(cloudOverrides) });
}

// ---- Awaiting your OK ----

/** Never approved: submitted, priced live, waiting on a first decision. */
export const awaitingFresh = cloudJobView(
  {
    id: 201,
    name: "sweep-wd-3",
    state: "awaiting",
    reason: null,
    summary: "",
    submitter: "agent-3",
    submit_time: NOW - 120,
    queue_time: NOW - 120,
    est_runtime: 3 * 3600,
  },
  { estimated_cost: 4.2, max_cost: 8, approved_seconds: 10800, full_seconds: 10800 },
);

/** Paused at its approved run time; needs a person to approve it again before it can resume. */
export const awaitingReapproval = cloudJobView(
  {
    id: 202,
    name: "finetune-a",
    state: "awaiting",
    reason: "time_limit",
    summary: "paused at its approved run time; approve it again to carry on",
    submitter: "First Owner",
    submit_time: NOW - 9000,
    queue_time: NOW - 300,
    est_runtime: 6 * 3600,
    run_time: 3 * 3600,
    attempts: 1,
  },
  {
    gpu: "A100",
    estimated_cost: 3.1,
    max_cost: 9,
    approved_seconds: 3600,
    full_seconds: 5400,
    job_spent: 4.9,
  },
);

export const awaiting: JobView[] = [awaitingFresh, awaitingReapproval];

// ---- Running in the cloud ----

/** On pace: nothing for a person to do. */
const runningStarting = cloudJobView(
  {
    id: 210,
    name: "probe-run",
    state: "running",
    submitter: "agent-3",
    submit_time: NOW - 60,
    queue_time: NOW - 60,
    start_time: NOW - 30,
    run_time: 30,
    est_runtime: 2 * 3600,
    attempts: 1,
  },
  { phase: "starting", approved_seconds: 7200, full_seconds: 7200 },
);

/** Has a live console link while the daemon polls its attempt. */
const runningWithConsole = cloudJobView(
  {
    id: 211,
    name: "eval-batch",
    state: "running",
    submitter: "First Owner",
    submit_time: NOW - 5400,
    queue_time: NOW - 5400,
    start_time: NOW - 5000,
    run_time: 5000,
    est_runtime: 4 * 3600,
    attempts: 1,
  },
  {
    phase: "running",
    console_url: "https://modal.com/apps/placeholder/eval-batch",
    approved_seconds: 14400,
    full_seconds: 14400,
    job_spent: 3.6,
  },
);

/** Its own pace projects it past its approved run time; needs a person to extend it. */
const runningNeedsMoreTime = cloudJobView(
  {
    id: 212,
    name: "train-long",
    state: "running",
    submitter: "agent-3",
    submit_time: NOW - 20000,
    queue_time: NOW - 20000,
    start_time: NOW - 19000,
    run_time: 19000,
    est_runtime: 5 * 3600,
    attempts: 1,
  },
  {
    phase: "running",
    approved_seconds: 18000,
    full_seconds: 18000,
    needs_more_time: 1800,
    job_spent: 8.7,
  },
);

/** Approved, but not launched yet: the target is already running as many jobs as it may. */
export const queuedApproved = cloudJobView(
  {
    id: 213,
    name: "sweep-wd-4",
    state: "queued",
    submitter: "agent-3",
    submit_time: NOW - 300,
    queue_time: NOW - 120,
    est_runtime: 2 * 3600,
  },
  { estimated_cost: 3.1, max_cost: 6.5, approved_seconds: 2 * 3600, full_seconds: 2 * 3600, blocked: "concurrency" },
);

export const needsTime: JobView[] = [runningNeedsMoreTime];
export const running: JobView[] = [runningStarting, runningWithConsole, runningNeedsMoreTime];

// ---- Recent results ----

/** Completed, and its checkpoint has been pulled down locally. */
const recentCompleted = cloudJobView(
  {
    id: 220,
    name: "sweep-wd-1",
    state: "completed",
    reason: null,
    summary: "",
    submitter: "agent-3",
    submit_time: NOW - 30000,
    queue_time: NOW - 30000,
    start_time: NOW - 29000,
    end_time: NOW - 3600,
    run_time: 25400,
    est_runtime: 7 * 3600,
    attempts: 1,
  },
  {
    phase: null,
    job_spent: 6.1,
    persist: cloudPersist({
      files: 4,
      bytes: 512 * 1024 * 1024,
      pulled_at: NOW - 3500,
      pulled_to: "/home/agent-3/pasar-pulled/220",
      remote_deleted: true,
      remote_bytes: 0,
    }),
  },
);

/** Failed, and what it left is still sitting on the target until the retention sweep runs. */
const recentFailed = cloudJobView(
  {
    id: 221,
    name: "finetune-b",
    state: "failed",
    reason: "exit",
    summary: "crashed with exit code 1",
    submitter: "First Owner",
    submit_time: NOW - 50000,
    queue_time: NOW - 50000,
    start_time: NOW - 49000,
    end_time: NOW - 40000,
    run_time: 9000,
    est_runtime: 6 * 3600,
    attempts: 2,
  },
  {
    phase: null,
    job_spent: 2.4,
    persist: cloudPersist({
      files: 2,
      bytes: 64 * 1024 * 1024,
      pulled_at: NOW - 39900,
      pulled_to: "/home/first-owner/pasar-pulled/221",
      remote_deleted: false,
      remote_bytes: 64 * 1024 * 1024,
      sweeps_at: NOW + 6 * 86400,
    }),
  },
);

/** Cancelled before it ever checkpointed: nothing was saved anywhere. */
const recentCancelled = cloudJobView(
  {
    id: 222,
    name: "sweep-wd-2",
    state: "cancelled",
    reason: "cancelled",
    summary: "cancelled while running",
    submitter: "agent-3",
    submit_time: NOW - 8000,
    queue_time: NOW - 8000,
    start_time: NOW - 7000,
    end_time: NOW - 6900,
    run_time: 100,
    est_runtime: 3 * 3600,
    attempts: 1,
  },
  { phase: null, job_spent: 0.1, persist: cloudPersist() },
);

/** Completed a moment ago; its results are still at the target, waiting for their pull. */
const recentAtTarget = cloudJobView(
  {
    id: 223,
    name: "eval-batch-2",
    state: "completed",
    reason: "exit",
    summary: "",
    submitter: "agent-2",
    submit_time: NOW - 5000,
    queue_time: NOW - 5000,
    start_time: NOW - 4000,
    end_time: NOW - 30,
    run_time: 3970,
    est_runtime: 3600,
    attempts: 1,
  },
  { phase: null, job_spent: 1.9, persist: cloudPersist({ sweeps_at: NOW + 3 * 86400 }) },
);

/** Failed before it ever started a sandbox (`launch_error`): its one attempt is recorded, but
 * nothing was ever running at the target to save anything. */
const recentNeverLaunched = cloudJobView(
  {
    id: 224,
    name: "sweep-wd-5",
    state: "failed",
    reason: "launch_error",
    summary: "pasar_job is not importable in the target's environment",
    submitter: "agent-3",
    submit_time: NOW - 2000,
    queue_time: NOW - 2000,
    start_time: NOW - 1900,
    end_time: NOW - 1900,
    run_time: 0,
    est_runtime: 3600,
    attempts: 1,
  },
  { phase: null, job_spent: 0, persist: cloudPersist({ remote_bytes: 0, sweeps_at: null }) },
);

export const recent: JobView[] = [
  recentAtTarget, recentCompleted, recentFailed, recentCancelled, recentNeverLaunched,
];

export function cloudBlock(overrides: Partial<CloudBlock> = {}): CloudBlock {
  return {
    targets: [cloudTarget()],
    awaiting,
    needs_time: needsTime,
    recent,
    ...overrides,
  };
}
