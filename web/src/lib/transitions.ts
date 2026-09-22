import { isOom } from "./format";
import type { JobView } from "./types";

export type TransitionKind = "started" | "completed" | "failed" | "oom" | "lost" | "preempted" | "cancelled";

export interface Transition {
  kind: TransitionKind;
  job: JobView;
  at: number;
}

function classifyReason(reason: string | null): TransitionKind {
  if (isOom(reason)) return "oom";
  if (reason === "lost") return "lost";
  return "failed";
}

function wasRunning(job: JobView): boolean {
  return job.state === "running" || job.state === "stopping";
}

function classify(before: JobView | undefined, next: JobView): TransitionKind | null {
  if (before === undefined) {
    // Brand-new job: only a "started" transition is produced, and only if it's already running.
    return next.state === "running" ? "started" : null;
  }
  if (!wasRunning(before) && next.state === "running") return "started";
  if (next.state === "completed") return "completed";
  if (next.state === "failed") return classifyReason(next.reason);
  if (next.state === "cancelled") return "cancelled";
  if (wasRunning(before) && next.state === "queued") {
    if (next.reason === "preempted") return "preempted";
    return classifyReason(next.reason);
  }
  return null;
}

/** Diffs two job lists into the transitions that happened since `prev`. `prev === null` means
 * this is the first snapshot ever seen, so nothing has "happened" yet — everything is just
 * initial state. Output order follows `next`. */
export function transitions(prev: JobView[] | null, next: JobView[], now: number): Transition[] {
  if (prev === null) return [];
  const prevById = new Map(prev.map((j) => [j.id, j]));
  const result: Transition[] = [];
  for (const job of next) {
    const kind = classify(prevById.get(job.id), job);
    if (kind !== null) result.push({ kind, job, at: now });
  }
  return result;
}
