import type { JobEvent } from "./types";

const EXCLUDED_KEYS = new Set(["step", "total_steps", "ts"]);
const MAX_SERIES = 4;

function keyOrder(a: string, b: string): number {
  if (a === "loss") return -1;
  if (b === "loss") return 1;
  return a.localeCompare(b);
}

/** Turns progress-event payloads into up-to-4 named series, `[ts, value][]` in ts order. Every
 * numeric (finite) payload key other than `step`/`total_steps`/`ts` becomes a series; keys are
 * sorted alphabetically except `loss`, which always comes first when present. */
export function progressSeries(events: JobEvent[]): Record<string, [number, number][]> {
  const keys = new Set<string>();
  for (const e of events) {
    if (e.kind !== "progress") continue;
    for (const [k, v] of Object.entries(e.payload)) {
      if (EXCLUDED_KEYS.has(k)) continue;
      if (typeof v === "number" && Number.isFinite(v)) keys.add(k);
    }
  }
  const chosen = [...keys].sort(keyOrder).slice(0, MAX_SERIES);

  const result: Record<string, [number, number][]> = {};
  for (const k of chosen) result[k] = [];

  const ordered = events.filter((e) => e.kind === "progress").slice().sort((a, b) => a.ts - b.ts);
  for (const e of ordered) {
    for (const k of chosen) {
      const v = e.payload[k];
      if (typeof v === "number" && Number.isFinite(v)) result[k].push([e.ts, v]);
    }
  }
  return result;
}
