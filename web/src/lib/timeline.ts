import type { JobView } from "./types";

export type BlockKind = "past" | "run" | "proj";

export interface Block {
  id: number;
  kind: BlockKind;
  start: number;
  end: number;
  lo: number; // bytes
  hi: number; // bytes
}

/** How far the window reaches before/after `now`, in seconds. */
export const WINDOW_BEFORE = 90 * 60;
export const WINDOW_AFTER = 240 * 60;

/** Height a job's block occupies when it isn't the live "run" block: the whole pool for a
 * whole-GPU job, otherwise its memory limit. */
function baseHeight(job: JobView, pool: number): number {
  return job.mode === "whole" ? pool : job.limit;
}

/** Height for the live "run" block: usage can run over the limit (the job gets stopped only if
 * the machine as a whole is short), so the block grows to show that, capped at the pool. */
function runHeight(job: JobView, pool: number): number {
  if (job.mode === "whole") return pool;
  return Math.min(pool, Math.max(job.limit, job.usage ?? 0));
}

/** Turns jobs into unstacked timeline blocks (`lo`/`hi` are placeholders — `stack` positions
 * them). Window `[now - WINDOW_BEFORE, now + WINDOW_AFTER)` only affects which segments are
 * dropped up front; blocks are not clipped to it — the renderer clamps for drawing. */
export function blocks(jobs: JobView[], pool: number, now: number): Block[] {
  const t0 = now - WINDOW_BEFORE;
  const t1 = now + WINDOW_AFTER;
  const result: Block[] = [];

  for (const job of jobs) {
    const height = baseHeight(job, pool);

    for (const [start, end] of job.spans) {
      if (end !== null && end > t0) {
        result.push({ id: job.id, kind: "past", start, end, lo: 0, hi: height });
      }
    }

    const isLive = job.state === "running" || job.state === "stopping";
    const openSpan = isLive ? job.spans.find(([, end]) => end === null) : undefined;

    if (openSpan) {
      const [start] = openSpan;
      const end = job.projected.length > 0
        ? job.projected[0][1]
        : Math.max(now + 60, start + job.est_runtime);
      result.push({ id: job.id, kind: "run", start, end, lo: 0, hi: runHeight(job, pool) });

      for (const [ps, pe] of job.projected.slice(1)) {
        if (pe <= t0 || ps >= t1) continue;
        result.push({ id: job.id, kind: "proj", start: ps, end: pe, lo: 0, hi: height });
      }
    } else if (job.state === "queued") {
      for (const [ps, pe] of job.projected) {
        if (pe <= t0 || ps >= t1) continue;
        result.push({ id: job.id, kind: "proj", start: ps, end: pe, lo: 0, hi: height });
      }
    }
  }

  return result;
}

const KIND_ORDER: Record<BlockKind, number> = { run: 0, past: 1, proj: 2 };

/** Assigns each block a memory band `[lo, hi)` so time-overlapping blocks never overlap in
 * memory, packing from the bottom of the pool. Blocks are placed in order: earliest start
 * first, then `run` before `past` before `proj`, then taller before shorter, then by id — so
 * the layout is deterministic regardless of input order. */
export function stack(bs: Block[], pool: number): Block[] {
  const ordered = [...bs].sort((a, b) => {
    if (a.start !== b.start) return a.start - b.start;
    if (KIND_ORDER[a.kind] !== KIND_ORDER[b.kind]) return KIND_ORDER[a.kind] - KIND_ORDER[b.kind];
    const ha = a.hi - a.lo;
    const hb = b.hi - b.lo;
    if (ha !== hb) return hb - ha;
    return a.id - b.id;
  });

  const placed: Block[] = [];
  const result: Block[] = [];
  for (const b of ordered) {
    const height = b.hi - b.lo;
    const overlapping = placed.filter((p) => p.start < b.end && b.start < p.end);
    const candidates = Array.from(new Set([0, ...overlapping.map((p) => p.hi)])).sort((x, y) => x - y);
    const chosen = candidates.find((c) => !overlapping.some((p) => c < p.hi && p.lo < c + height));
    const lo = chosen !== undefined && chosen + height <= pool ? chosen : Math.max(0, pool - height);
    const placedBlock: Block = { ...b, lo, hi: lo + height };
    placed.push(placedBlock);
    result.push(placedBlock);
  }
  return result;
}

/** `stack(blocks(jobs, pool, now), pool)`. */
export function layout(jobs: JobView[], pool: number, now: number): Block[] {
  return stack(blocks(jobs, pool, now), pool);
}

/** Whole-hour tick marks within `[t0, t1]`; 30-minute steps once there's room (`width >= 900`)
 * and the window is short enough (`<= 6h`) that half-hour marks stay legible. */
export function timeTicks(t0: number, t1: number, width: number): number[] {
  const span = t1 - t0;
  const step = width >= 900 && span <= 6 * 3600 ? 30 * 60 : 60 * 60;
  const ticks: number[] = [];
  for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) ticks.push(t);
  return ticks;
}
