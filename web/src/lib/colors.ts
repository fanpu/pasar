export const JOB_COLORS = ["#3b8fd9", "#d9577f", "#8a63d2", "#23a47a", "#c9761f"];

interface ColorKey {
  id: number;
  tags: string[];
}

/** A small, stable string hash (FNV-1a) so the same tag always lands on the same palette slot —
 * pure and stateless, so it's deterministic across reloads and between the server and browser. */
function hash(s: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/** A job's colour: jobs that share a first tag share a colour, so the palette groups tagged jobs
 * at a glance. Untagged jobs fall back to their id, as before. Used everywhere a job's colour is
 * drawn (timeline blocks, the job table's id chips and tag pills, the memory pool bar, charts) so
 * a given job always reads as the same colour. */
export function jobColor(job: ColorKey): string {
  const key = job.tags.length > 0 ? hash(job.tags[0]) : job.id;
  return JOB_COLORS[((key % 5) + 5) % 5];
}

/** A soft, muted take on a job's colour for "finished" states (past timeline blocks, ended job
 * rows): a light tint fill and a faded border, so finished jobs stay distinguishable from each
 * other without reading as still-live. */
export function mutedJobColor(job: ColorKey): { fill: string; border: string } {
  const c = jobColor(job);
  return { fill: `${c}17`, border: `${c}66` };
}
