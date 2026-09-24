// Pure filter/sort model for the job list. The filter itself lives in the page's query string
// (see router.svelte.ts); this module only parses/serializes it and applies it to a job list.
import type { JobState, JobView } from "./types";

export type SortKey = "id" | "name" | "state" | "bid" | "memory" | "time" | "submitted" | "ended" | "by";
export type StateFilter = "running" | "queued" | "completed" | "failed" | "cancelled";
export interface Sort { key: SortKey; desc: boolean }
export interface Filter { q: string; states: StateFilter[]; tags: string[]; by: string[]; sort: Sort | null }

export const EMPTY_FILTER: Filter = { q: "", states: [], tags: [], by: [], sort: null };
export const STATE_FILTERS: StateFilter[] = ["running", "queued", "completed", "failed", "cancelled"];

const SORT_KEYS: SortKey[] = ["id", "name", "state", "bid", "memory", "time", "submitted", "ended", "by"];

function dedupe<T>(values: T[]): T[] {
  return [...new Set(values)];
}

function parseSort(raw: string | null): Sort | null {
  if (!raw) return null;
  const desc = raw.startsWith("-");
  const key = (desc ? raw.slice(1) : raw) as SortKey;
  return SORT_KEYS.includes(key) ? { key, desc } : null;
}

/** Accepts `location.search`-shaped input, with or without the leading `?`. Unknown params and
 * invalid values (an unknown state, an empty tag/by, a bad sort key) are silently dropped. */
export function parseFilter(search: string): Filter {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  const q = params.get("q") ?? "";
  const states = dedupe(params.getAll("state").filter((s): s is StateFilter => STATE_FILTERS.includes(s as StateFilter)));
  const tags = dedupe(params.getAll("tag").filter((t) => t !== ""));
  const by = dedupe(params.getAll("by").filter((b) => b !== ""));
  const sort = parseSort(params.get("sort"));
  return { q, states, tags, by, sort };
}

/** Inverse of `parseFilter`: `""` when the filter is empty, else a `?`-prefixed query string with
 * params in a stable order (q, state, tag, by, sort) so URLs are predictable and diffable. */
export function filterToSearch(f: Filter): string {
  const params = new URLSearchParams();
  if (f.q) params.set("q", f.q);
  for (const s of f.states) params.append("state", s);
  for (const t of f.tags) params.append("tag", t);
  for (const b of f.by) params.append("by", b);
  if (f.sort) params.set("sort", f.sort.desc ? `-${f.sort.key}` : f.sort.key);
  const s = params.toString();
  return s ? `?${s}` : "";
}

export function isActive(f: Filter): boolean {
  return f.q !== "" || f.states.length > 0 || f.tags.length > 0 || f.by.length > 0 || f.sort !== null;
}

/** Maps a job's raw state onto a filter bucket: running and stopping both count as "running";
 * awaiting (cloud jobs waiting on a person to approve their cost) counts as "queued" until the
 * job list grows its own awaiting group. */
function stateFilterOf(state: JobState): StateFilter {
  if (state === "running" || state === "stopping") return "running";
  if (state === "awaiting") return "queued";
  return state;
}

/** `q` matches `#id`/id, name, command or note (case-insensitive substring). `states` (if any)
 * matches the job's bucket from `stateFilterOf`. `tags` requires every listed tag on the job.
 * `by` requires the submitter be one of the listed ones. */
export function matches(job: JobView, f: Filter): boolean {
  if (f.q) {
    const q = f.q.toLowerCase();
    const haystacks = [`#${job.id}`, String(job.id), job.name, job.command, job.note];
    if (!haystacks.some((h) => h.toLowerCase().includes(q))) return false;
  }
  if (f.states.length > 0 && !f.states.includes(stateFilterOf(job.state))) return false;
  if (f.tags.length > 0 && !f.tags.every((t) => job.tags.includes(t))) return false;
  if (f.by.length > 0 && !f.by.includes(job.submitter)) return false;
  return true;
}

export function effectiveSort(f: Filter): Sort {
  return f.sort ?? { key: "id", desc: true };
}

// Sort-key semantics: id/bid/memory(limit)/submitted(submit_time) are plain numeric fields;
// name/by(submitter) use localeCompare; state uses this fixed order; time is run_time once a job
// has started, else est_runtime; ended (end_time) sorts unset values last regardless of direction.
const STATE_ORDER: JobState[] = ["running", "stopping", "queued", "failed", "completed", "cancelled"];

function timeValue(job: JobView): number {
  return job.start_time !== null ? job.run_time : job.est_runtime;
}

function rawCompare(key: Exclude<SortKey, "ended">, a: JobView, b: JobView): number {
  switch (key) {
    case "id": return a.id - b.id;
    case "name": return a.name.localeCompare(b.name);
    case "state": return STATE_ORDER.indexOf(a.state) - STATE_ORDER.indexOf(b.state);
    case "bid": return a.bid - b.bid;
    case "memory": return a.limit - b.limit;
    case "time": return timeValue(a) - timeValue(b);
    case "submitted": return a.submit_time - b.submit_time;
    case "by": return a.submitter.localeCompare(b.submitter);
  }
}

function compare(key: SortKey, a: JobView, b: JobView, desc: boolean): number {
  if (key === "ended") {
    if (a.end_time === null && b.end_time === null) return 0;
    if (a.end_time === null) return 1;
    if (b.end_time === null) return -1;
    return desc ? b.end_time - a.end_time : a.end_time - b.end_time;
  }
  const c = rawCompare(key, a, b);
  return desc ? -c : c;
}

/** Returns a new, sorted array; ties (equal on the sort key) break by id descending. */
export function sortJobs(jobs: JobView[], s: Sort): JobView[] {
  return [...jobs].sort((a, b) => compare(s.key, a, b, s.desc) || b.id - a.id);
}

/** Per-state counts over jobs matching `f`, but ignoring `f.states` itself — i.e. what each state
 * chip's count would become, given the other active filters. */
export function stateCounts(jobs: JobView[], f: Filter): Record<StateFilter, number> {
  const base: Filter = { ...f, states: [] };
  const counts: Record<StateFilter, number> = { running: 0, queued: 0, completed: 0, failed: 0, cancelled: 0 };
  for (const job of jobs) {
    if (matches(job, base)) counts[stateFilterOf(job.state)]++;
  }
  return counts;
}

/** Every tag and submitter seen across `jobs`, sorted and deduplicated, for "+ tag"/"+ submitter"
 * dropdowns. Empty strings (no submitter set) are dropped. */
export function knownValues(jobs: JobView[]): { tags: string[]; by: string[] } {
  const tags = new Set<string>();
  const by = new Set<string>();
  for (const job of jobs) {
    for (const t of job.tags) tags.add(t);
    if (job.submitter) by.add(job.submitter);
  }
  return { tags: [...tags].sort((a, b) => a.localeCompare(b)), by: [...by].sort((a, b) => a.localeCompare(b)) };
}

export interface TagSummary {
  total: number; running: number; queued: number; completed: number; failed: number; cancelled: number;
  gpuSeconds: number; queuedSeconds: number;
  medianRun: number | null; medianEstimate: number | null; estimateOk: boolean | null;
}

function median(nums: number[]): number | null {
  if (nums.length === 0) return null;
  const sorted = [...nums].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid];
}

/** Summary strip shown when exactly one tag filter is active. `running` counts running+stopping;
 * `gpuSeconds` sums `run_time` over all `jobs`; `queuedSeconds` sums `est_runtime` of queued jobs;
 * the medians are over completed jobs' `run_time`/`est_runtime`, null when there are none;
 * `estimateOk` is whether the medians are within 20% of each other, null if either median is. */
export function tagSummary(jobs: JobView[]): TagSummary {
  let running = 0, queued = 0, completed = 0, failed = 0, cancelled = 0;
  let gpuSeconds = 0, queuedSeconds = 0;
  const completedRuns: number[] = [];
  const completedEstimates: number[] = [];
  for (const job of jobs) {
    gpuSeconds += job.run_time;
    switch (stateFilterOf(job.state)) {
      case "running":
        running++;
        break;
      case "queued":
        queued++;
        queuedSeconds += job.est_runtime;
        break;
      case "completed":
        completed++;
        completedRuns.push(job.run_time);
        completedEstimates.push(job.est_runtime);
        break;
      case "failed":
        failed++;
        break;
      case "cancelled":
        cancelled++;
        break;
    }
  }
  const medianRun = median(completedRuns);
  const medianEstimate = median(completedEstimates);
  const estimateOk = medianRun === null || medianEstimate === null
    ? null
    : Math.abs(medianRun - medianEstimate) <= 0.2 * medianEstimate;
  return {
    total: jobs.length, running, queued, completed, failed, cancelled,
    gpuSeconds, queuedSeconds, medianRun, medianEstimate, estimateOk,
  };
}
