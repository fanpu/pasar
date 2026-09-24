// The cloud lanes under the schedule chart: one row per cloud account with an attempt in view,
// each attempt a bar on the chart's own time axis. Cloud jobs hold none of this machine's memory,
// so they stay off the memory chart (see Timeline.svelte) and are drawn here instead.
import type { JobView } from "./types";

/** `run`: an attempt still going, drawn solid up to now; `past`: one that has ended. */
export type LaneBarKind = "run" | "past";

export interface LaneBar {
  id: number;
  /** 1-based attempt number within the job, as the job panel counts them. */
  attempt: number;
  kind: LaneBarKind;
  start: number;
  /** When it ended, or `now` for a running attempt. */
  end: number;
  /** For a running attempt: where its approved window runs out (`cloud.approved_seconds` from
   * its start), drawn dashed past `now`; `null` once that is behind it, or when the window is
   * not known. Always `null` for a finished attempt. */
  until: number | null;
  /** Its row within the lane: an account may run several attempts at once. */
  row: number;
  /** How the attempt ended (`spans[i][2]`, which for a cloud attempt may also be `paused`);
   * `null` while it runs. */
  endKind: string | null;
}

export interface Lane {
  target: string;
  owner: string | null;
  rows: number;
  bars: LaneBar[];
}

/** The cloud lanes for the window `[t0, t1)`: one per account (`cloud.target`) that has an
 * attempt in it, sorted by name, each attempt one bar. Jobs that have not run yet (awaiting or
 * queued with no attempt) have nothing on the time axis and draw nothing; a job that paused shows
 * each attempt apart, with the gap between. Attempts that overlap in time go on separate rows. */
export function cloudLanes(jobs: JobView[], now: number, t0: number, t1: number): Lane[] {
  const byTarget = new Map<string, { owner: string | null; bars: LaneBar[] }>();
  for (const job of jobs) {
    const c = job.cloud;
    if (!c) continue;
    job.spans.forEach(([start, end, endKind], i) => {
      const live = end === null;
      const stop = live ? Math.max(start, now) : end;
      const windowEnd = live && c.approved_seconds !== null ? start + c.approved_seconds : null;
      const until = windowEnd !== null && windowEnd > stop ? windowEnd : null;
      if (Math.max(stop, until ?? stop) <= t0 || start >= t1) return;
      let lane = byTarget.get(c.target);
      if (!lane) {
        lane = { owner: null, bars: [] };
        byTarget.set(c.target, lane);
      }
      lane.owner ??= c.owner;
      lane.bars.push({ id: job.id, attempt: i + 1, kind: live ? "run" : "past", start, end: stop,
                       until, row: 0, endKind: endKind ?? null });
    });
  }
  return [...byTarget.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([target, { owner, bars }]) => {
      const { placed, rows } = pack(bars);
      return { target, owner, rows, bars: placed };
    });
}

/** Puts each bar on the lowest row where it overlaps nothing already there (a running attempt
 * reaches to the end of its dashed window), earliest start first, so the layout does not depend
 * on the order jobs arrive in. A job's later attempt keeps its earlier one's row when that is
 * free, so a pause reads as a gap in one line. */
function pack(bars: LaneBar[]): { placed: LaneBar[]; rows: number } {
  const ordered = [...bars].sort((a, b) => a.start - b.start || a.id - b.id || a.attempt - b.attempt);
  const rowEnds: number[] = [];
  const lastRow = new Map<number, number>();
  const placed = ordered.map((b) => {
    const prev = lastRow.get(b.id);
    let row = prev !== undefined && rowEnds[prev] <= b.start
      ? prev
      : rowEnds.findIndex((e) => e <= b.start);
    if (row === -1) row = rowEnds.push(0) - 1;
    lastRow.set(b.id, row);
    rowEnds[row] = Math.max(b.end, b.until ?? b.end);
    return { ...b, row };
  });
  return { placed, rows: rowEnds.length };
}
