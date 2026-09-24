// Per-job sprites: a small "is this local or in the cloud, and what's it doing" picture next to
// a job row. Purely derived from fields the table and cloud card already read — nothing new is
// fetched for it — so `spriteState` below mirrors StatePill's and cloud.ts's own readings of the
// same job (preempted queued jobs, the cloud phase pill, an awaiting job that's really paused).
import type { JobSpriteKind, JobSpriteManifest, JobSpriteState, JobView } from "./types";

export function spriteKind(job: JobView): JobSpriteKind {
  return job.cloud !== null ? "cloud" : "local";
}

/** Which sprite state a job is in right now. */
export function spriteState(job: JobView): JobSpriteState {
  switch (job.state) {
    case "running":
      if (job.cloud !== null && (job.cloud.phase === "pending" || job.cloud.phase === "starting")) {
        return "starting";
      }
      // Only a local job carries a memory limit to be over; a cloud job's "over" equivalent
      // (past its approved time) already has its own `needs_more_time` flag and pill.
      if (job.cloud === null && job.over_limit) return "over";
      return "running";
    case "stopping":
      return "stopping";
    case "queued":
      // A local job re-queued after being preempted (StatePill's "queued · preempted ×N");
      // a cloud job here is simply approved and waiting for its next launch pass.
      if (job.cloud === null && job.preemptions > 0) return "preempted";
      return "queued";
    case "awaiting":
      // The same signal cloud.ts's `whyBack` uses to tell a fresh submission (no reason yet)
      // from one that's back for another approval because it ran before and paused.
      return job.reason !== null ? "paused" : "awaiting";
    case "completed":
    case "failed":
    case "cancelled":
      return job.state;
    default:
      return "idle";
  }
}

/** A plain, visually-similar state to fall back to when the exact one has no sprite — e.g. a
 * "starting" sprite is just a "running" one when the user hasn't drawn a separate pose for it. */
const NEIGHBOR: Partial<Record<JobSpriteState, JobSpriteState>> = {
  starting: "running",
  over: "running",
  paused: "awaiting",
  preempted: "queued",
  stopping: "running",
};

/** The variant a job keeps for as long as it's on screen: deterministic on its id, so it doesn't
 * change pose every time the manifest or the job's own fields happen to re-render it. */
function pickVariant(urls: string[], jobId: number): string {
  return urls[((jobId % urls.length) + urls.length) % urls.length];
}

/** The sprite URL for `job`, or `null` when nothing in `manifest` fits — including when the user
 * has supplied no job sprites at all, in which case every row renders exactly as it did before
 * this feature existed. Fallback chain: the exact state, then a plain neighbour (if one applies),
 * then "idle", then nothing. */
export function spriteUrl(manifest: JobSpriteManifest, job: JobView): string | null {
  const state = spriteState(job);
  const states = manifest[spriteKind(job)] ?? {};
  const neighbor = NEIGHBOR[state];
  const candidates: JobSpriteState[] = neighbor ? [state, neighbor, "idle"] : [state, "idle"];
  for (const candidate of candidates) {
    const urls = states[candidate];
    if (urls && urls.length > 0) return pickVariant(urls, job.id);
  }
  return null;
}

/** An accessible name for the sprite, since its `alt` is empty (purely decorative next to the
 * job's own name/state text): "cloud job, running", "local job, over" and so on. */
export function spriteLabel(job: JobView): string {
  return `${spriteKind(job)} job, ${spriteState(job)}`;
}
