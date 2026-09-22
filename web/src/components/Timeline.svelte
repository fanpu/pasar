<script lang="ts">
  import { getJobsBetween } from "../lib/api";
  import { GIB, fmtGib } from "../lib/format";
  import { jobColor } from "../lib/colors";
  import { mascot } from "../lib/mascot.svelte";
  import {
    clampWindow, layout, liveWindow, LIVE_HISTORY, tickLabel, tickStep, timeTicks, when,
    WINDOW_AFTER, WINDOW_BEFORE, zoomAround, type BlockKind, type Window,
  } from "../lib/timeline";
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

  // The visible window: its length, and where it starts once the user has moved it (null
  // means it follows `now`).
  const DEFAULT_SPAN = WINDOW_BEFORE + WINDOW_AFTER;
  const PRESETS = [
    { label: "6h", span: 6 * 3600 },
    { label: "1d", span: 86400 },
    { label: "1w", span: 7 * 86400 },
    { label: "30d", span: 30 * 86400 },
  ];
  let span = $state(DEFAULT_SPAN);
  let pinnedT0 = $state<number | null>(null);
  const following = $derived(pinnedT0 === null);
  const win = $derived(pinnedT0 === null ? liveWindow(now, span) : clampWindow(pinnedT0, pinnedT0 + span, now));
  const t0 = $derived(win.t0);
  const t1 = $derived(win.t1);
  const plotWidth = $derived(width - L - R);

  function setWindow(w: Window) {
    const c = clampWindow(w.t0, w.t1, now);
    span = c.t1 - c.t0;
    pinnedT0 = c.t0;
  }
  function panBy(seconds: number) {
    setWindow({ t0: t0 + seconds, t1: t1 + seconds });
  }
  function zoomBy(factor: number) {
    if (following) {
      const w = clampWindow(0, span * factor, Infinity);
      span = w.t1 - w.t0;
    } else {
      setWindow(zoomAround(win, (t0 + t1) / 2, factor, now));
    }
  }
  function showPreset(s: number) {
    span = s;
    pinnedT0 = null;
  }
  function reset() {
    span = DEFAULT_SPAN;
    pinnedT0 = null;
  }
  function timeAt(px: number): number {
    return t0 + ((px - L) / plotWidth) * (t1 - t0);
  }

  // Jobs that finished before the live snapshot's reach, fetched when the window goes there.
  let history = $state<JobView[]>([]);
  let loading = $state(false);
  let fetched: Window | null = null;
  let fetchSeq = 0;
  $effect(() => {
    const a = t0, b = Math.min(t1, now);
    if (a >= now - LIVE_HISTORY) return;
    if (fetched && fetched.t0 <= a && fetched.t1 >= b) return;
    const pad = (b - a) / 2;
    const range = { t0: Math.floor(a - pad), t1: Math.ceil(b + pad) };
    const timer = setTimeout(async () => {
      const seq = ++fetchSeq;
      fetched = range;
      loading = true;
      try {
        const got = await getJobsBetween(range.t0, range.t1);
        if (seq === fetchSeq) history = got;
      } catch {
        if (seq === fetchSeq) fetched = null; // try again when the window next moves
      } finally {
        if (seq === fetchSeq) loading = false;
      }
    }, 200);
    return () => clearTimeout(timer);
  });
  const shown = $derived.by(() => {
    if (history.length === 0) return jobs;
    const live = new Set(jobs.map((j) => j.id));
    return [...jobs, ...history.filter((j) => !live.has(j.id))];
  });

  // Drag to pan; ctrl/⌘ + wheel (or a trackpad pinch) to zoom; horizontal wheel to pan.
  let svgEl = $state<SVGSVGElement | null>(null);
  let drag: { x0: number; t0: number; moved: boolean } | null = null;
  let justDragged = false;
  let dragging = $state(false);

  function onpointerdown(e: PointerEvent) {
    if (e.button !== 0) return;
    drag = { x0: e.clientX, t0, moved: false };
  }
  function onpointermove(e: PointerEvent) {
    if (!drag) return;
    const dx = e.clientX - drag.x0;
    if (!drag.moved) {
      if (Math.abs(dx) < 5) return;
      drag.moved = true;
      dragging = true;
      svgEl?.setPointerCapture?.(e.pointerId);
      hideTip();
    }
    const t = drag.t0 - dx * ((t1 - t0) / plotWidth);
    setWindow({ t0: t, t1: t + (t1 - t0) });
  }
  function onpointerup() {
    if (drag?.moved) {
      justDragged = true;
      setTimeout(() => (justDragged = false));
    }
    drag = null;
    dragging = false;
  }
  $effect(() => {
    const el = svgEl;
    if (!el) return;
    const onwheel = (e: WheelEvent) => {
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        const px = e.clientX - el.getBoundingClientRect().left;
        // A wheel notch (deltaY ±100 or more) zooms by ~1.3×; a trackpad pinch's small deltas zoom smoothly.
        const d = Math.max(-50, Math.min(50, e.deltaY));
        setWindow(zoomAround(win, timeAt(px), Math.exp(d * 0.005), now));
      } else if (Math.abs(e.deltaX) > Math.abs(e.deltaY) || e.shiftKey) {
        e.preventDefault();
        const d = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
        panBy(d * ((t1 - t0) / plotWidth));
      }
    };
    el.addEventListener("wheel", onwheel, { passive: false });
    return () => el.removeEventListener("wheel", onwheel);
  });

  function x(t: number): number {
    const clamped = Math.max(t0, Math.min(t1, t));
    return L + ((clamped - t0) / (t1 - t0)) * (width - L - R);
  }
  function y(g: number): number {
    return T + (1 - g / pool) * (height - T - B);
  }

  const jobsById = $derived(new Map(shown.map((j) => [j.id, j])));
  const laidOut = $derived(layout(shown, pool, now, t0, t1));
  const isEmpty = $derived(laidOut.length === 0);
  const ticks = $derived(timeTicks(t0, t1, width));
  const step = $derived(tickStep(t0, t1, width));

  function kindLabel(kind: BlockKind): string {
    return kind === "proj" ? "projected" : kind === "past" ? "finished" : "running";
  }

  function ariaLabel(job: JobView, kind: BlockKind, start: number, end: number): string {
    if (kind === "run") return `#${job.id} ${job.name}, running until ~${when(end, now)}`;
    if (kind === "past") return `#${job.id} ${job.name}, ran ${when(start, now)}–${when(end, now)}`;
    return `#${job.id} ${job.name}, projected ${when(start, now)}–${when(end, now)}`;
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
    if (justDragged) return;
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
  <h3>
    Schedule
    <span class="dim">{following ? "memory over time" : `${when(t0, now)} – ${when(t1, now)}`}{loading ? " · loading…" : ""}</span>
    <span class="tlnav" role="toolbar" aria-label="Schedule range">
      {#each PRESETS as p (p.label)}
        <button class="chip" class:on={following && span === p.span} aria-pressed={following && span === p.span} onclick={() => showPreset(p.span)}>{p.label}</button>
      {/each}
      <span class="gap"></span>
      <button class="chip" aria-label="Earlier" onclick={() => panBy(-(t1 - t0) / 2)}>‹</button>
      <button class="chip" aria-label="Later" onclick={() => panBy((t1 - t0) / 2)}>›</button>
      <button class="chip" aria-label="Zoom out" onclick={() => zoomBy(2)}>−</button>
      <button class="chip" aria-label="Zoom in" onclick={() => zoomBy(0.5)}>+</button>
      {#if !following || span !== DEFAULT_SPAN}
        <button class="chip now" onclick={reset}>now</button>
      {/if}
    </span>
  </h3>
  <div class="tlwrap" bind:clientWidth={wrapperWidth}>
    {#if isEmpty}
      <div class="tlempty">
        <img src={mascot.pick("hmm")} alt="" width="64" height="64" />
        <p>{following && t1 > now ? "Nothing scheduled." : loading ? "Looking back…" : "Nothing ran in this window."}</p>
      </div>
    {/if}
    <svg
      class="tl"
      class:dragging
      style="height: {height}px"
      bind:this={svgEl}
      role="presentation"
      {onpointerdown}
      {onpointermove}
      {onpointerup}
      onpointercancel={onpointerup}
    >
      {#each [0, pool / 2, pool] as g (g)}
        <line x1={L} x2={width - R} y1={y(g)} y2={y(g)} class="gridline" />
        <text x={L - 6} y={y(g) + 4} text-anchor="end" class="axislabel">{Math.round(g / GIB)}G</text>
      {/each}
      {#each ticks as t (t)}
        <line x1={x(t)} x2={x(t)} y1={T} y2={height - B} class="hourline" />
        <text x={x(t)} y={height - 7} text-anchor="middle" class="axislabel">{tickLabel(t, step)}</text>
      {/each}
      {#each laidOut as b, i (`${b.id}:${b.kind}:${b.start}`)}
        {@const job = jobsById.get(b.id)}
        {#if job}
          {@const X = x(b.start) + 1}
          {@const Y = y(b.hi) + 1}
          {@const w = Math.max(1.5, x(b.end) - x(b.start) - 2)}
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
              rx={Math.min(9, w / 3)}
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
                {b.kind === "proj" ? `~${when(b.start, now)}–${when(b.end, now)}` : b.kind === "past" ? `${when(b.start, now)}–${when(b.end, now)}` : `until ~${when(b.end, now)}`} · ★{job.bid}
              </text>
            {/if}
          </g>
        {/if}
      {/each}
      {#if now >= t0 && now <= t1}
        <line x1={x(now)} x2={x(now)} y1={T - 4} y2={height - B} class="nowline" />
        <circle cx={x(now)} cy={T - 4} r="3.5" class="nowdot" />
      {/if}
    </svg>
  </div>
  <div class="tlkey">
    <span><svg width="22" height="12"><rect x="1" y="1" width="20" height="10" rx="4" fill="#8a63d233" stroke="#8a63d2" stroke-width="1.5" /></svg>running</span>
    <span><svg width="22" height="12"><rect x="1" y="1" width="20" height="10" rx="4" fill="#fff" stroke="#8a63d2" stroke-width="1.5" stroke-dasharray="3 2" /></svg>projected</span>
    <span><svg width="22" height="12"><rect x="1" y="1" width="20" height="10" rx="4" fill="#f2edf0" /></svg>finished</span>
    <span><svg width="10" height="14"><rect x="4" y="0" width="2" height="14" fill="#ff8fab" /></svg>now</span>
    <span class="hint">drag to move · ctrl + scroll to zoom</span>
  </div>
</div>

{#if tip}
  <div class="tip" style="left: {tip.x}px; top: {tip.y}px">
    <b>#{tip.job.id} {tip.job.name}</b><br />
    <span class="dim">{kindLabel(tip.kind)} · {when(tip.start, now)}–{when(tip.end, now)}</span><br />
    ★ {tip.job.bid} · {tip.job.mode === "whole" ? "whole GPU" : fmtGib(tip.job.limit)}
  </div>
{/if}

<style>
  .tlwrap { width: 100%; position: relative; }
  .tl { width: 100%; display: block; cursor: grab; touch-action: pan-y; user-select: none; }
  .tl.dragging { cursor: grabbing; }
  .tl.dragging .blk { cursor: grabbing; }
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

  .tlempty { position: absolute; inset: 0; pointer-events: none; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; color: var(--ink-2); font-weight: 700; }
  .tlempty img { width: 64px; height: 64px; }
  .tlempty p { margin: 0; }

  .tlkey { display: flex; gap: 14px; font-size: 12px; font-weight: 700; color: var(--ink-2); margin-top: 6px; flex-wrap: wrap; }
  .tlkey span { display: inline-flex; align-items: center; gap: 5px; }
  .tlkey .hint { margin-left: auto; color: var(--ink-3); }

  h3 { flex-wrap: wrap; }
  .tlnav { margin-left: auto; display: inline-flex; align-items: center; gap: 4px; flex-wrap: wrap; }
  .tlnav .gap { width: 6px; }
  .chip { border: 1.5px solid var(--line-2); background: var(--card); color: var(--ink-2); border-radius: 99px; min-width: 30px; padding: 3px 10px; font-size: 12px; font-weight: 800; line-height: 1.2; }
  .chip:hover { color: var(--ink); border-color: var(--accent); }
  .chip.on { background: var(--accent); border-color: var(--accent); color: #fff; }
  .chip.now { color: var(--accent-ink); border-color: var(--accent); }

  .tip { position: fixed; pointer-events: none; background: #3b3340; color: #fff; border-radius: 12px; padding: 8px 11px; font-size: 12.5px; font-weight: 700; box-shadow: 0 6px 18px rgba(0, 0, 0, 0.18); z-index: 40; line-height: 1.45; }
  .tip .dim { color: #cbbfcb; }
</style>
