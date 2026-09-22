<script lang="ts">
  import { GIB, fmtGib, hm } from "../lib/format";
  import { jobColor } from "../lib/colors";
  import { mascot } from "../lib/mascot.svelte";
  import { layout, timeTicks, WINDOW_AFTER, WINDOW_BEFORE, type BlockKind } from "../lib/timeline";
  import type { JobView } from "../lib/types";

  interface Props {
    jobs: JobView[];
    pool: number;
    now: number;
    onopen: (id: number) => void;
  }
  let { jobs, pool, now, onopen }: Props = $props();

  // Unique per component instance so clipPath ids never collide if several Timelines mount.
  const uid = Math.random().toString(36).slice(2);

  // jsdom has no layout, so a measured width of 0 falls back to a sensible default.
  let wrapperWidth = $state(0);
  const width = $derived(wrapperWidth || 800);
  const height = $derived(width < 760 ? 150 : 220);

  const L = 44, R = 10, T = 10, B = 24;

  const t0 = $derived(now - WINDOW_BEFORE);
  const t1 = $derived(now + WINDOW_AFTER);

  function x(t: number): number {
    const clamped = Math.max(t0, Math.min(t1, t));
    return L + ((clamped - t0) / (t1 - t0)) * (width - L - R);
  }
  function y(g: number): number {
    return T + (1 - g / pool) * (height - T - B);
  }

  const jobsById = $derived(new Map(jobs.map((j) => [j.id, j])));
  const laidOut = $derived(layout(jobs, pool, now));
  const isEmpty = $derived(laidOut.length === 0);
  const ticks = $derived(timeTicks(t0, t1, width));

  function kindLabel(kind: BlockKind): string {
    return kind === "proj" ? "projected" : kind === "past" ? "finished" : "running";
  }

  function ariaLabel(job: JobView, kind: BlockKind, start: number, end: number): string {
    if (kind === "run") return `#${job.id} ${job.name}, running until ~${hm(end)}`;
    if (kind === "past") return `#${job.id} ${job.name}, finished ${hm(start)}–${hm(end)}`;
    return `#${job.id} ${job.name}, projected ${hm(start)}–${hm(end)}`;
  }

  interface Tip { x: number; y: number; job: JobView; kind: BlockKind; start: number; end: number }
  let tip = $state<Tip | null>(null);

  function showTip(e: MouseEvent, job: JobView, kind: BlockKind, start: number, end: number) {
    tip = { x: e.clientX + 14, y: e.clientY + 12, job, kind, start, end };
  }
  function hideTip() {
    tip = null;
  }
  function open(job: JobView) {
    onopen(job.id);
  }
  function onBlockKeydown(e: KeyboardEvent, job: JobView) {
    if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
      e.preventDefault(); // stop Space from scrolling the page
      open(job);
    }
  }
</script>

<div class="sec">
  <h3>Schedule <span class="dim">memory over time · solid = running, dashed = projected from estimates</span></h3>
  <div class="tlwrap" bind:clientWidth={wrapperWidth}>
    {#if isEmpty}
      <div class="tlempty" style="height: {height}px">
        <img src={mascot.pick("hmm")} alt="" width="64" height="64" />
        <p>Nothing scheduled.</p>
      </div>
    {:else}
      <svg class="tl" style="height: {height}px">
        {#each [0, pool / 2, pool] as g (g)}
          <line x1={L} x2={width - R} y1={y(g)} y2={y(g)} class="gridline" />
          <text x={L - 6} y={y(g) + 4} text-anchor="end" class="axislabel">{Math.round(g / GIB)}G</text>
        {/each}
        {#each ticks as t (t)}
          <line x1={x(t)} x2={x(t)} y1={T} y2={height - B} class="hourline" />
          <text x={x(t)} y={height - 7} text-anchor="middle" class="axislabel">{hm(t)}</text>
        {/each}
        {#each laidOut as b, i (`${b.id}:${b.kind}:${b.start}`)}
          {@const job = jobsById.get(b.id)}
          {#if job}
            {@const X = x(b.start) + 1}
            {@const Y = y(b.hi) + 1}
            {@const w = Math.max(0, x(b.end) - x(b.start) - 2)}
            {@const h = y(b.lo) - y(b.hi) - 2}
            {@const c = jobColor(job.id)}
            {@const label = w > 60 ? `#${job.id} ${job.name}` : `#${job.id}`}
            {@const clipId = `tl-clip-${uid}-${job.id}-${b.kind}-${i}`}
            <g
              role="button"
              tabindex="0"
              aria-label={ariaLabel(job, b.kind, b.start, b.end)}
              class="blk"
              onclick={() => open(job)}
              onkeydown={(e) => onBlockKeydown(e, job)}
              onmousemove={(e) => showTip(e, job, b.kind, b.start, b.end)}
              onmouseleave={hideTip}
            >
              <rect
                x={X}
                y={Y}
                width={w}
                height={h}
                rx="9"
                fill={b.kind === "run" ? `${c}2e` : b.kind === "past" ? "#f2edf0" : "#ffffff"}
                stroke={b.kind === "past" ? "#e4d9df" : c}
                stroke-width="1.8"
                stroke-dasharray={b.kind === "proj" ? "5 3" : undefined}
              />
              {#if b.kind !== "past"}
                <rect x={X} y={Y} width="4" height={h} rx="2" fill={c} />
              {/if}
              <clipPath id={clipId}>
                <rect x={X} y={Y} width={Math.max(0, w - 6)} height={h} />
              </clipPath>
              <text clip-path="url(#{clipId})" x={X + 11} y={Y + 17} class="blklabel" class:past={b.kind === "past"}>{label}</text>
              {#if h > 34 && w > 80}
                <text clip-path="url(#{clipId})" x={X + 11} y={Y + 32} class="blksub">
                  {b.kind === "proj" ? `~${hm(b.start)}–${hm(b.end)}` : `until ~${hm(b.end)}`} · ★{job.bid}
                </text>
              {/if}
            </g>
          {/if}
        {/each}
        <line x1={x(now)} x2={x(now)} y1={T - 4} y2={height - B} class="nowline" />
        <circle cx={x(now)} cy={T - 4} r="3.5" class="nowdot" />
      </svg>
    {/if}
  </div>
  <div class="tlkey">
    <span><svg width="22" height="12"><rect x="1" y="1" width="20" height="10" rx="4" fill="#8a63d233" stroke="#8a63d2" stroke-width="1.5" /></svg>running</span>
    <span><svg width="22" height="12"><rect x="1" y="1" width="20" height="10" rx="4" fill="#fff" stroke="#8a63d2" stroke-width="1.5" stroke-dasharray="3 2" /></svg>projected</span>
    <span><svg width="22" height="12"><rect x="1" y="1" width="20" height="10" rx="4" fill="#f2edf0" /></svg>finished</span>
    <span><svg width="10" height="14"><rect x="4" y="0" width="2" height="14" fill="#ff8fab" /></svg>now</span>
  </div>
</div>

{#if tip}
  <div class="tip" style="left: {tip.x}px; top: {tip.y}px">
    <b>#{tip.job.id} {tip.job.name}</b><br />
    <span class="dim">{kindLabel(tip.kind)} · {hm(tip.start)}–{hm(tip.end)}</span><br />
    ★ {tip.job.bid} · {tip.job.mode === "whole" ? "whole GPU" : fmtGib(tip.job.limit)}
  </div>
{/if}

<style>
  .tlwrap { width: 100%; }
  .tl { width: 100%; display: block; }
  .tl :global(text) { font-family: Nunito, sans-serif; }
  .gridline { stroke: #f4eaef; stroke-width: 1; }
  .hourline { stroke: #f7eff3; }
  .axislabel { font-size: 11px; font-weight: 700; fill: var(--ink-3); }
  .blk { cursor: pointer; }
  .blk:focus-visible rect:first-child { outline: 2px solid var(--accent); outline-offset: 1px; }
  .blklabel { font-size: 12px; font-weight: 800; fill: var(--ink); }
  .blklabel.past { fill: var(--ink-3); }
  .blksub { font-size: 11px; font-weight: 700; fill: var(--ink-2); }
  .nowline { stroke: var(--accent); stroke-width: 2; }
  .nowdot { fill: var(--accent); }

  .tlempty { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; color: var(--ink-2); font-weight: 700; }
  .tlempty img { width: 64px; height: 64px; }
  .tlempty p { margin: 0; }

  .tlkey { display: flex; gap: 14px; font-size: 12px; font-weight: 700; color: var(--ink-2); margin-top: 6px; flex-wrap: wrap; }
  .tlkey span { display: inline-flex; align-items: center; gap: 5px; }

  .tip { position: fixed; pointer-events: none; background: #3b3340; color: #fff; border-radius: 12px; padding: 8px 11px; font-size: 12.5px; font-weight: 700; box-shadow: 0 6px 18px rgba(0, 0, 0, 0.18); z-index: 40; line-height: 1.45; }
  .tip .dim { color: #cbbfcb; }
</style>
