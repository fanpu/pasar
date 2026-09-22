import { getAllJobs } from "./api";
import type { JobView } from "./types";

export interface AllJobsOptions {
  fetchAll?: () => Promise<JobView[]>;
  minRefetchMs?: number;
  clock?: () => number;
}

/** Cache of every job, past and present, kept fresh while a filter or sort is active (the ~100
 * jobs/day dataset is small enough to filter/sort client-side). `$state.raw` because the array is
 * replaced wholesale on every fetch or overlay, never mutated in place. */
export class AllJobs {
  jobs = $state.raw<JobView[] | null>(null);

  #fetchAll: () => Promise<JobView[]>;
  #minRefetchMs: number;
  #clock: () => number;
  #lastFetch: number | null = null;
  #fetching = false;
  // The most recent `live` an active `update()` call has seen, kept so a fetch that was started
  // several ticks ago can still overlay it on resolve — the `live` array closed over at fetch time
  // would otherwise be however-many-ticks stale.
  #latestLive: JobView[] = [];

  constructor(opts?: AllJobsOptions) {
    this.#fetchAll = opts?.fetchAll ?? getAllJobs;
    this.#minRefetchMs = opts?.minRefetchMs ?? 30000;
    this.#clock = opts?.clock ?? (() => Date.now());
  }

  /** `active=false` does nothing (keeps whatever's cached). `active=true` fetches the full list
   * when it's never been loaded, when `live` contains a job id the cache doesn't have yet, or once
   * `minRefetchMs` has passed since the last fetch — at most one fetch in flight at a time. Either
   * way, `live`'s jobs are overlaid onto the cache by id (live wins); jobs in the cache that aren't
   * in `live` are left as they were. */
  update(active: boolean, live: JobView[]): void {
    if (!active) return;
    this.#latestLive = live;
    const cache = this.jobs;
    const needsFetch = cache === null
      || live.some((j) => !cache.some((c) => c.id === j.id))
      || this.#lastFetch === null
      || this.#clock() - this.#lastFetch >= this.#minRefetchMs;
    if (needsFetch && !this.#fetching) {
      this.#fetching = true;
      this.#fetchAll().then(
        (jobs) => {
          // Overlay whatever `live` is *now* (not whatever it was when this fetch started) —
          // several snapshot ticks, each with their own overlay, may have landed while the fetch
          // was in flight, and assigning the fetched list as-is would replace those fresher rows
          // with the older ones the fetch happened to return.
          this.jobs = this.#mergedWithLive(jobs, this.#latestLive);
          this.#lastFetch = this.#clock();
          this.#fetching = false;
        },
        () => {
          this.#fetching = false;
        },
      );
    }
    this.#overlay(live);
  }

  #overlay(live: JobView[]): void {
    const cache = this.jobs;
    if (cache === null) return;
    const next = this.#mergedWithLive(cache, live);
    if (next !== cache) this.jobs = next;
  }

  /** Returns `cache` overlaid with `live` by id (live wins on content differences; jobs in `cache`
   * that aren't in `live` are kept as-is; jobs in `live` but not `cache` are appended). Returns
   * `cache` itself (same reference) when nothing actually changed, so callers can skip a
   * reassignment — see the note in `#overlay`'s caller about why that matters for reactivity. */
  #mergedWithLive(cache: JobView[], live: JobView[]): JobView[] {
    if (live.length === 0) return cache;
    const liveById = new Map(live.map((j) => [j.id, j]));
    let changed = false;
    const next = cache.map((j) => {
      const l = liveById.get(j.id);
      // A live snapshot's jobs are freshly parsed from JSON on every tick, so `l` is never the
      // same object as the cached `j` even when nothing about the job actually changed — comparing
      // by reference would mark every tick "changed" and reassign `jobs` forever. Compare content
      // instead so an unchanged job (the common case for anything already running) is a no-op.
      if (!l || sameJob(l, j)) return j;
      changed = true;
      return l;
    });
    for (const j of live) {
      if (!cache.some((c) => c.id === j.id)) {
        next.push(j);
        changed = true;
      }
    }
    return changed ? next : cache;
  }
}

function sameJob(a: JobView, b: JobView): boolean {
  return a === b || JSON.stringify(a) === JSON.stringify(b);
}
