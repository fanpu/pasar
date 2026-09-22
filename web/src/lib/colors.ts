/** Ten hues, all at least 3:1 against the page and as far apart as the soft style allows. Colour is
 * never the only cue: every block, row and chip also shows the job's id (and the table its tags). */
export const JOB_COLORS = [
  "#3b8fd9", "#d9577f", "#8a63d2", "#23a47a", "#c9761f",
  "#6d7607", "#c03710", "#aa3a97", "#3a65d6", "#c765ce",
];

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

// Palette slots handed out to the first tags currently on screen (see `assignTagColors`).
let tagSlots = new Map<string, number>();

/** Shares the palette out among the first tags of `jobs` (the live snapshot), so tags on screen
 * together don't share a colour until there are more tags than colours. Tags are placed in order
 * of their oldest job, each at its hash slot or the next free one, so a tag's colour only changes
 * when an older tag it collided with leaves the screen. Tags not placed here use their hash slot. */
export function assignTagColors(jobs: ColorKey[]): void {
  const n = JOB_COLORS.length;
  const oldest = new Map<string, number>();
  for (const j of jobs) {
    if (j.tags.length > 0) oldest.set(j.tags[0], Math.min(oldest.get(j.tags[0]) ?? Infinity, j.id));
  }
  const order = [...oldest].sort((a, b) => a[1] - b[1]).map(([tag]) => tag);
  const slots = new Map<string, number>();
  let used = new Set<number>();
  for (const tag of order) {
    if (used.size === n) used = new Set();
    let slot = hash(tag) % n;
    while (used.has(slot)) slot = (slot + 1) % n;
    used.add(slot);
    slots.set(tag, slot);
  }
  tagSlots = slots;
}

/** A job's colour: jobs that share a first tag share a colour, so the palette groups tagged jobs
 * at a glance. Untagged jobs fall back to their id, as before. Used everywhere a job's colour is
 * drawn (timeline blocks, the job table's id chips and tag pills, the memory pool bar, charts) so
 * a given job always reads as the same colour. */
export function jobColor(job: ColorKey): string {
  const n = JOB_COLORS.length;
  if (job.tags.length > 0) return JOB_COLORS[tagSlots.get(job.tags[0]) ?? hash(job.tags[0]) % n];
  return JOB_COLORS[((job.id % n) + n) % n];
}

/** A soft, muted take on a job's colour for "finished" states (past timeline blocks, ended job
 * rows): a light tint fill and a faded border, so finished jobs stay distinguishable from each
 * other without reading as still-live. */
export function mutedJobColor(job: ColorKey): { fill: string; border: string } {
  const c = jobColor(job);
  return { fill: `${c}17`, border: `${c}66` };
}
