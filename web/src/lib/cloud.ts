// Small wording helpers for cloud jobs, shared by the Cloud card and the Approve dialog. Each one
// reads only the fields the stream already carries (`cloud_view`, `persist_view`,
// `cloud_status_view` in views.py) and never guesses a figure the daemon did not send.
import { dur, fmtGib, GIB } from "./format";
import type { CloudGpu, CloudJob, CloudPersist, CloudTarget, JobView } from "./types";

/** A size of saved results: GiB as everywhere else, but small results (a metrics file, a
 * LoRA adapter) in MiB or KiB rather than a "0.0 GiB" that reads as nothing at all. */
export function resultSize(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || bytes >= 0.1 * GIB) return fmtGib(bytes);
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KiB`;
}

/** Dollars with cents; "–" when the daemon could not price it. */
export function money(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "–";
  return `$${v.toFixed(2)}`;
}

/** "Sep 26" in the viewer's own time zone. */
export function shortDate(ts: number): string {
  return new Date(ts * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

/** The GPU row a target offers under `name`, if the target still lists it. */
export function gpuOn(target: CloudTarget | null | undefined, name: string): CloudGpu | null {
  return target?.gpus.find((g) => g.name.toLowerCase() === name.toLowerCase()) ?? null;
}

/** "H100 · 80GB", or just "H100" when the provider's memory table doesn't know the card. */
export function gpuLabel(c: CloudJob, target: CloudTarget | null | undefined): string {
  const g = gpuOn(target, c.gpu);
  return g?.memory_gb != null ? `${c.gpu} · ${g.memory_gb}GB` : c.gpu;
}

/** " · <owner>'s account" next to wherever the account is already named; "" when the owner is
 * unknown (the job's target is no longer configured). */
export function ownerNote(owner: string | null | undefined): string {
  return owner ? ` · ${owner}'s account` : "";
}

// Why an awaiting job came back to a person, for jobs whose `summary` is empty.
const BACK_REASONS: Record<string, string> = {
  time_limit: "paused at its approved run time",
  reclaimed: "the provider took the machine back",
  price_rose: "the price rose since it was approved",
  job_cap: "at its job cap",
  preempted: "the provider took the machine back",
};

/** Why an awaiting job is back for another approval; "" for one that was never approved. */
export function whyBack(job: JobView): string {
  if (job.summary) return job.summary;
  if (job.reason === null) return "";
  return BACK_REASONS[job.reason] ?? job.reason;
}

/** How the run time one approval buys was arrived at, next to that run time. */
export function runTimeHow(c: CloudJob): string {
  if (c.approved_seconds === null) return "can't be priced right now";
  if (c.full_seconds !== null && c.full_seconds > c.approved_seconds + 1) {
    const cause = c.user_capped ? "its own --max-cost" : "the job cap";
    return `of the ${dur(c.full_seconds)} it asked for; ${cause} cuts it short`;
  }
  return "all the time it asked for";
}

export interface PhaseLook { cls: string; icon: string; text: string }

/** A pill for where a cloud attempt has got to (`cloud.phase`); `null` when there is no live
 * attempt to report on, in which case the job's own state pill says it better. */
export function phaseLook(phase: CloudJob["phase"]): PhaseLook | null {
  switch (phase) {
    case null:
      return null;
    case "pending":
      return { cls: "s-queued", icon: "◷", text: "pending" };
    case "starting":
      return { cls: "s-queued", icon: "◷", text: "starting" };
    case "running":
      return { cls: "s-running", icon: "●", text: "running" };
    case "success":
      return { cls: "s-completed", icon: "⇧", text: "wrapping up" };
    case "exit-code":
      return { cls: "s-failed", icon: "✕", text: "exited" };
    case "signal":
      return { cls: "s-failed", icon: "✕", text: "killed" };
    case "stopped":
      return { cls: "s-stopping", icon: "◐", text: "stopped" };
    case "reclaimed":
      return { cls: "s-stopping", icon: "◐", text: "reclaimed" };
    case "time_limit":
      return { cls: "s-stopping", icon: "◐", text: "at its time" };
  }
}

/** What a finished cloud job left behind, in one line: pulled home, still at the target, being
 * pulled, or nothing at all. */
export function persistLine(p: CloudPersist | null, target: string): string {
  if (p === null) return "nothing saved";
  if (p.pulled_to !== null) {
    const pulled = `pulled ${resultSize(p.bytes)} → ${p.pulled_to}`;
    if (!p.remote_deleted && p.sweeps_at !== null) return `${pulled} · copy at ${target} until ${shortDate(p.sweeps_at)}`;
    return pulled;
  }
  if (p.swept_at !== null && (p.swept_bytes ?? 0) > 0) {
    return `never pulled; ${resultSize(p.swept_bytes)} deleted from ${target} on ${shortDate(p.swept_at)}`;
  }
  if (p.remote_bytes === 0 || p.swept_at !== null || (p.sweeps_at === null && p.remote_bytes === null)) {
    return "nothing saved";
  }
  const where = p.sweeps_at !== null ? `at ${target} until ${shortDate(p.sweeps_at)}` : `at ${target}`;
  return p.last_error ? `${where} · pull failed: ${p.last_error}` : `${where} · pulling…`;
}
