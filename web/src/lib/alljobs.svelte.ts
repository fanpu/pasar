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
    const cache = this.jobs;
    const needsFetch = cache === null
      || live.some((j) => !cache.some((c) => c.id === j.id))
      || this.#lastFetch === null
      || this.#clock() - this.#lastFetch >= this.#minRefetchMs;
    if (needsFetch && !this.#fetching) {
      this.#fetching = true;
      this.#fetchAll().then(
        (jobs) => {
          this.jobs = jobs;
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
    if (cache === null || live.length === 0) return;
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
    if (changed) this.jobs = next;
  }
}

function sameJob(a: JobView, b: JobView): boolean {
  return a === b || JSON.stringify(a) === JSON.stringify(b);
}
