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

/** How far the live window reaches before/after `now`, in seconds. */
export const WINDOW_BEFORE = 90 * 60;
export const WINDOW_AFTER = 240 * 60;

/** Zoom limits for the window's length, in seconds. */
export const MIN_SPAN = 30 * 60;
export const MAX_SPAN = 90 * 86400;
/** The window can't reach further ahead than the projection does. */
export const MAX_AHEAD = 7 * 86400;
/** How far back the live snapshot lists finished jobs (matches the API's `RECENT`); windows
 * reaching further back fetch history. */
export const LIVE_HISTORY = 86400;
/** Share of the live window that lies before `now`. */
export const LIVE_LEAD = WINDOW_BEFORE / (WINDOW_BEFORE + WINDOW_AFTER);

export interface Window { t0: number; t1: number }

/** Keeps a window within the zoom limits and no further ahead than `now + MAX_AHEAD`. */
export function clampWindow(t0: number, t1: number, now: number): Window {
  const span = Math.min(MAX_SPAN, Math.max(MIN_SPAN, t1 - t0));
  const mid = (t0 + t1) / 2;
  let a = mid - span / 2;
  const limit = now + MAX_AHEAD;
  if (a + span > limit) a = limit - span;
  return { t0: a, t1: a + span };
}

/** Zooms by `factor` (< 1 zooms in) keeping the time under `anchor` fixed on screen. */
export function zoomAround(w: Window, anchor: number, factor: number, now: number): Window {
  const span = Math.min(MAX_SPAN, Math.max(MIN_SPAN, (w.t1 - w.t0) * factor));
  const frac = (anchor - w.t0) / (w.t1 - w.t0);
  const t0 = anchor - frac * span;
  return clampWindow(t0, t0 + span, now);
}

/** Held-key speeds: W/S change the window's length by a factor of e^ZOOM_RATE per second, and
 * A/D move it by PAN_RATE window-lengths per second. */
export const ZOOM_RATE = 2.5;
export const PAN_RATE = 0.8;

/** One animation step of held W/A/S/D keys, `dt` seconds long. `zoom` is +1 for out (S), -1 for
 * in (W); `pan` is +1 for later (D), -1 for earlier (A). Zooming keeps `anchor` in place. */
export function keyStep(w: Window, zoom: number, pan: number, dt: number, anchor: number,
                        now: number): Window {
  let out = w;
  if (zoom !== 0) out = zoomAround(out, anchor, Math.exp(zoom * ZOOM_RATE * dt), now);
  if (pan !== 0) {
    const d = pan * PAN_RATE * dt * (out.t1 - out.t0);
    out = clampWindow(out.t0 + d, out.t1 + d, now);
  }
  return out;
}

/** The live window for a given length. Up to the default length, `LIVE_LEAD` of it lies
 * before `now`; longer windows are for looking back, so they reach only a little ahead
 * (`WINDOW_AFTER`, or a tenth of the window if that's more). */
export function liveWindow(now: number, span: number): Window {
  const after = span <= WINDOW_BEFORE + WINDOW_AFTER
    ? span * (1 - LIVE_LEAD)
    : Math.max(WINDOW_AFTER, span / 10);
  return { t0: now + after - span, t1: now + after };
}

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
 * them). The window `[t0, t1)` (default: the live window around `now`) only affects which
 * segments are dropped up front; blocks are not clipped to it — the renderer clamps for drawing. */
export function blocks(jobs: JobView[], pool: number, now: number,
                       t0 = now - WINDOW_BEFORE, t1 = now + WINDOW_AFTER): Block[] {
  const result: Block[] = [];

  for (const job of jobs) {
    const height = baseHeight(job, pool);

    for (const [start, end] of job.spans) {
      if (end !== null && end > t0 && start < t1) {
        result.push({ id: job.id, kind: "past", start, end, lo: 0, hi: height });
      }
    }

    const isLive = job.state === "running" || job.state === "stopping";
    const openSpan = isLive ? job.spans.find(([, end]) => end === null) : undefined;

    if (openSpan && openSpan[0] < t1) {
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

/** `stack(blocks(jobs, pool, now, t0, t1), pool)`. */
export function layout(jobs: JobView[], pool: number, now: number,
                       t0 = now - WINDOW_BEFORE, t1 = now + WINDOW_AFTER): Block[] {
  return stack(blocks(jobs, pool, now, t0, t1), pool);
}

const H = 3600;
const D = 86400;
const STEPS = [5 * 60, 10 * 60, 15 * 60, 30 * 60, H, 2 * H, 3 * H, 6 * H, 12 * H, D, 2 * D, 7 * D];
/** Closest ticks may sit to each other, in pixels. */
const MIN_TICK_GAP = 70;

/** Seconds to add to a UTC timestamp to get local wall-clock time at that moment. */
function tzShift(t: number): number {
  return -new Date(t * 1000).getTimezoneOffset() * 60;
}

/** The tick step for a window drawn `width` pixels wide: the smallest one that keeps ticks at
 * least `MIN_TICK_GAP` px apart. */
export function tickStep(t0: number, t1: number, width: number): number {
  const perPx = (t1 - t0) / Math.max(1, width - 54);
  return STEPS.find((s) => s / perPx >= MIN_TICK_GAP) ?? STEPS[STEPS.length - 1];
}

/** Tick marks within `[t0, t1]` on a local wall-clock grid (whole hours fall on :00, days on
 * midnight), spaced by `tickStep`. */
export function timeTicks(t0: number, t1: number, width: number): number[] {
  const step = tickStep(t0, t1, width);
  const ticks: number[] = [];
  const shift = tzShift(t0);
  for (let local = Math.ceil((t0 + shift) / step) * step; ; local += step) {
    const t = local - tzShift(local - shift);
    if (t > t1) break;
    if (t >= t0) ticks.push(t);
  }
  return ticks;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "Sep 21". */
export function dayLabel(ts: number): string {
  const d = new Date(ts * 1000);
  return `${MONTHS[d.getMonth()]} ${d.getDate()}`;
}

/** A tick's label: the date for day steps and at midnight, otherwise the time of day. */
export function tickLabel(ts: number, step: number): string {
  const d = new Date(ts * 1000);
  if (step >= D || (d.getHours() === 0 && d.getMinutes() === 0)) return dayLabel(ts);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** A moment, as the time of day when it's on the same local day as `now`, else with the date. */
export function when(ts: number, now: number): string {
  const a = new Date(ts * 1000);
  const b = new Date(now * 1000);
  const hm = `${String(a.getHours()).padStart(2, "0")}:${String(a.getMinutes()).padStart(2, "0")}`;
  const sameDay = a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  return sameDay ? hm : `${dayLabel(ts)} ${hm}`;
}
