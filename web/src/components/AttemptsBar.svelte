<script lang="ts">
  import { dur } from "../lib/format";
  import type { AttemptView, JobDetail } from "../lib/types";

  interface Props {
    detail: JobDetail;
    now: number;
  }
  let { detail, now }: Props = $props();

  const WAITING = "#e8dfe5";
  const RUNNING = "#9fd6b5";
  const LOST = "#f3a0b2";

  interface Seg {
    color: string;
    seconds: number;
  }

  const show = $derived(detail.attempts.length > 1 || detail.preemptions > 0);

  function buildSegments(attempts: AttemptView[], submitTime: number): Seg[] {
    const segs: Seg[] = [];
    if (attempts.length === 0) return segs;
    const firstGap = attempts[0].start_time - submitTime;
    if (firstGap > 0) segs.push({ color: WAITING, seconds: firstGap });
    for (let i = 0; i < attempts.length; i++) {
      const a = attempts[i];
      const end = a.end_time ?? now;
      const total = Math.max(0, end - a.start_time);
      const head = i > 0 ? (a.restart_cost ?? 0) : 0;
      const tail = a.wasted_work ?? 0;
      const running = Math.max(0, total - head - tail);
      if (head > 0) segs.push({ color: LOST, seconds: head });
      if (running > 0) segs.push({ color: RUNNING, seconds: running });
      if (tail > 0) segs.push({ color: LOST, seconds: tail });
      if (i < attempts.length - 1 && a.end_time !== null) {
        const gap = attempts[i + 1].start_time - a.end_time;
        if (gap > 0) segs.push({ color: WAITING, seconds: gap });
      }
    }
    return segs;
  }

  const segments = $derived(buildSegments(detail.attempts, detail.submit_time));

  const totals = $derived.by((): { wasted: number; restart: number } => {
    let wasted = 0;
    let restart = 0;
    for (const a of detail.attempts) {
      wasted += a.wasted_work ?? 0;
      restart += a.restart_cost ?? 0;
    }
    return { wasted, restart };
  });
</script>

{#if show}
  <div class="fact attemptsfact">
    <div class="l">attempts</div>
    <div class="attempts">
      {#each segments as seg, i (i)}
        <i style="flex: {seg.seconds}; background: {seg.color}"></i>
      {/each}
    </div>
    <div class="s">
      <span style="color:#3d9a67">■</span> <span class="lbl">running</span>
      <span style="color:#e27892">■</span> <span class="lbl">lost ({dur(totals.wasted)} unsaved work + {dur(totals.restart)} restart)</span>
      <span style="color:#cfc3ca">■</span> <span class="lbl">waiting</span>
    </div>
  </div>
{/if}

<style>
  .attemptsfact { margin-bottom: 12px; }
  .attempts { display: flex; gap: 2px; height: 14px; border-radius: 99px; overflow: hidden; margin: 6px 0 4px; }
  .attempts i { height: 100%; }
</style>
