import { GIB } from "../lib/format";
import type { AttemptView, JobDetail, JobView, StatusView } from "../lib/types";

export { GIB };

/** Fixed clock so `hm`/`dur` outputs in tests are deterministic (tests run with TZ=UTC). */
export const NOW = Date.UTC(2026, 8, 21, 14, 26) / 1000;

export function job(overrides: Partial<JobView> = {}): JobView {
  return {
    id: 1,
    name: "job",
    state: "queued",
    reason: null,
    summary: "",
    stop_requested: null,
    bid: 1000,
    command: "",
    cwd: "",
    note: "",
    tags: [],
    submitter: "",
    git_commit: null,
    mode: "shared",
    mem_request: 4 * GIB,
    limit: 6 * GIB,
    usage: null,
    over_limit: false,
    peak: 0,
    est_runtime: 0,
    run_time: 0,
    remaining: 0,
    preempt: false,
    expected_runtime: 0,
    eta_source: "estimate",
    preemptible: true,
    grace: 0,
    retries: 0,
    retries_used: 0,
    submit_time: 0,
    queue_time: 0,
    start_time: null,
    end_time: null,
    attempts: 0,
    preemptions: 0,
    lost: { preemption: 0, failure: 0, known: true },
    progress: null,
    last_checkpoint: null,
    projected: [],
    spans: [],
    ...overrides,
  };
}

export function status(overrides: Partial<StatusView> = {}): StatusView {
  return {
    now: NOW,
    version: 0,
    mem_total: 0,
    mem_available: 0,
    psi_some_avg10: null,
    pool: 105 * GIB,
    reserved: 0,
    external: 0,
    free: 105 * GIB,
    pressure_since: null,
    blocked: [],
    waiting: [],
    machine_events: [],
    hot_temp_c: 85,
    grafana_url: null,
    ...overrides,
  };
}

export function attempt(overrides: Partial<AttemptView> = {}): AttemptView {
  return {
    job_id: 1,
    n: 1,
    unit: "u1",
    start_time: NOW - 600,
    end_time: null,
    end_kind: null,
    exit_code: null,
    signal: null,
    reason: null,
    summary: "",
    log_tail: "",
    peak_mem: 0,
    wasted_work: null,
    restart_cost: null,
    ...overrides,
  };
}

export function jobDetail(overrides: Partial<JobDetail> = {}): JobDetail {
  const { attempts, ...rest } = overrides;
  return { ...job(rest as Partial<JobView>), attempts: attempts ?? [] };
}
