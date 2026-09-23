import { getSparks } from "./api";
import type { SparkMap } from "./types";

export interface SparksOptions {
  fetchSparks?: (ids: number[]) => Promise<SparkMap>;
  minRefetchMs?: number;
  clock?: () => number;
}

/** Row sparklines for the jobs currently on screen, refreshed far more slowly than the snapshot
 * stream. A training curve barely moves between ticks, so re-sending every point on the daemon's
 * 2s tick would be waste; fetching by visible id also covers the filtered table, whose rows come
 * from the all-jobs cache rather than the stream. `$state.raw` because the map is replaced
 * wholesale on every fetch, never mutated in place. */
export class Sparks {
  data = $state.raw<SparkMap>({});

  #fetchSparks: (ids: number[]) => Promise<SparkMap>;
  #minRefetchMs: number;
  #clock: () => number;
  #lastIds: string | null = null;
  #lastFetch: number | null = null;
  #fetching = false;

  constructor(opts?: SparksOptions) {
    this.#fetchSparks = opts?.fetchSparks ?? getSparks;
    this.#minRefetchMs = opts?.minRefetchMs ?? 10000;
    this.#clock = opts?.clock ?? (() => Date.now());
  }

  /** Fetches when the visible rows have changed (a new job scrolled in, a filter was applied) or
   * `minRefetchMs` has passed, at most one fetch in flight. Ids are sorted so that the same rows
   * in a different order — a re-sort of the table — isn't mistaken for a new set. */
  update(ids: number[]): void {
    if (ids.length === 0 || this.#fetching) return;
    const sorted = [...ids].sort((a, b) => a - b);
    const key = sorted.join(",");
    const stale = this.#lastFetch === null || this.#clock() - this.#lastFetch >= this.#minRefetchMs;
    if (key === this.#lastIds && !stale) return;

    this.#fetching = true;
    this.#lastIds = key;
    this.#fetchSparks(sorted).then(
      (data) => {
        this.data = data;
        this.#lastFetch = this.#clock();
        this.#fetching = false;
      },
      () => {
        // Leave `data` alone: a stale curve beats a row that empties out whenever the daemon
        // blips. The next `update()` past `minRefetchMs` tries again.
        this.#lastFetch = this.#clock();
        this.#fetching = false;
      },
    );
  }
}
