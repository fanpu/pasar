import { dur, reasonLabel } from "./format";
import type { AttemptView, JobDetail, JobEvent } from "./types";

export interface EventRow {
  ts: number;
  icon: string;
  text: string;
}

// Rows built from an attempt's own lifecycle (submitted/started/ended) sort before an event row
// at the exact same timestamp.
const LIFECYCLE = 0;
const EVENT = 1;

interface RankedRow extends EventRow {
  rank: number;
}

// Why a cloud attempt paused (its `reason`), worded for the event list.
const PAUSE_REASONS: Record<string, string> = {
  time_limit: "out of approved time",
  cloud_preempted: "taken back by the provider",
};

function endRow(a: AttemptView): RankedRow | null {
  if (a.end_kind === null || a.end_time === null) return null;
  const ts = a.end_time;
  switch (a.end_kind) {
    case "completed":
      return { ts, icon: "✓", text: "completed", rank: LIFECYCLE };
    case "preempted": {
      const suffix = a.wasted_work !== null ? ` · ${dur(a.wasted_work)} unsaved work` : "";
      return { ts, icon: "⏸", text: `preempted${suffix}`, rank: LIFECYCLE };
    }
    case "failed": {
      const label = reasonLabel(a.reason);
      const text = a.summary ? `${label}: ${a.summary}` : label;
      return { ts, icon: "✕", text, rank: LIFECYCLE };
    }
    case "cancelled":
      return { ts, icon: "–", text: "cancelled", rank: LIFECYCLE };
    case "paused": {
      const why = PAUSE_REASONS[a.reason ?? ""] ?? reasonLabel(a.reason);
      return { ts, icon: "⏸", text: `attempt ${a.n} paused${why ? ` · ${why}` : ""} · waiting for approval`, rank: LIFECYCLE };
    }
  }
}

function checkpointRow(e: JobEvent): RankedRow {
  const text = e.step !== null ? `checkpoint · step ${e.step}` : "checkpoint";
  return { ts: e.ts, icon: "💾", text, rank: EVENT };
}

function resumedRow(e: JobEvent, attemptsByN: Map<number, AttemptView>): RankedRow {
  let text = e.step !== null ? `resumed · step ${e.step}` : "resumed";
  const restartCost = attemptsByN.get(e.attempt)?.restart_cost ?? null;
  if (restartCost !== null) text += ` (restart cost ${dur(restartCost)})`;
  return { ts: e.ts, icon: "▶", text, rank: EVENT };
}

function noteRow(e: JobEvent): RankedRow {
  const text = typeof e.payload.text === "string" ? e.payload.text : "";
  return { ts: e.ts, icon: "📝", text, rank: EVENT };
}

/** Newest-first timeline rows combining an attempt's own lifecycle (submitted/started/ended)
 * with checkpoint/resumed/note events. Progress events never become rows. */
export function eventRows(detail: JobDetail, events: JobEvent[]): EventRow[] {
  const attemptsByN = new Map(detail.attempts.map((a) => [a.n, a]));
  const rows: RankedRow[] = [];

  const submitText = detail.submitter ? `submitted by ${detail.submitter}` : "submitted";
  rows.push({ ts: detail.submit_time, icon: "◷", text: submitText, rank: LIFECYCLE });

  for (const a of detail.attempts) {
    rows.push({ ts: a.start_time, icon: "▶", text: `attempt ${a.n} started`, rank: LIFECYCLE });
    const end = endRow(a);
    if (end) rows.push(end);
  }

  for (const e of events) {
    if (e.kind === "checkpoint") rows.push(checkpointRow(e));
    else if (e.kind === "resumed") rows.push(resumedRow(e, attemptsByN));
    else if (e.kind === "note") rows.push(noteRow(e));
    // progress events never become rows
  }

  rows.sort((a, b) => (a.ts !== b.ts ? b.ts - a.ts : a.rank - b.rank));

  return rows.map(({ ts, icon, text }) => ({ ts, icon, text }));
}
