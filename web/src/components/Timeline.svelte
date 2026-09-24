<script lang="ts">
  import { getJobsBetween } from "../lib/api";
  import { GIB, fmtGib } from "../lib/format";
  import { money, ownerNote } from "../lib/cloud";
  import { jobColor, mutedJobColor } from "../lib/colors";
  import { cloudLanes, type Lane, type LaneBar } from "../lib/lanes";
  import { mascot } from "../lib/mascot.svelte";
  import {
    clampWindow, keyStep, layout, liveWindow, LIVE_HISTORY, tickLabel, tickStep, timeTicks, when,
    WINDOW_AFTER, WINDOW_BEFORE, zoomAround, type BlockKind, type Window,
  } from "../lib/timeline";
  import type { JobView } from "../lib/types";

  interface Props {
    jobs: JobView[];
    pool: number;
    now: number;
    onopen: (id: number) => void;
    // When given, blocks for a job it returns true for are faded (opacity 0.25) — used to dim
    // jobs that don't match the current filter without hiding them from the schedule entirely.
    dim?: (job: JobView) => boolean;
  }
  let { jobs, pool, now, onopen, dim }: Props = $props();

  // Unique per component instance so clipPath ids never collide if several Timelines mount.
  const uid = Math.random().toString(36).slice(2);
  // The same cloud glyph the job table badges cloud jobs with (24×24 box).
  const CLOUD_PATH = "M7 18a4 4 0 0 1-.6-7.96 5 5 0 0 1 9.44-2A4.5 4.5 0 0 1 17.5 18H7Z";

  // jsdom has no layout, so a measured width of 0 falls back to a sensible default.
  let wrapperWidth = $state(0);
  const width = $derived(wrapperWidth || 800);
  const narrow = $derived(width < 760);
  const height = $derived(narrow ? 240 : 220);

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
  // Phones get a shorter default window so blocks stay wide enough to read.
  const defaultSpan = $derived(narrow ? 3 * 3600 : DEFAULT_SPAN);
  let chosenSpan = $state<number | null>(null);
  const span = $derived(chosenSpan ?? defaultSpan);
  let pinnedT0 = $state<number | null>(null);
  const following = $derived(pinnedT0 === null);
  const win = $derived(pinnedT0 === null ? liveWindow(now, span) : clampWindow(pinnedT0, pinnedT0 + span, now));
  const t0 = $derived(win.t0);
  const t1 = $derived(win.t1);
  const plotWidth = $derived(width - L - R);

  function setWindow(w: Window) {
    const c = clampWindow(w.t0, w.t1, now);
    chosenSpan = c.t1 - c.t0;
    pinnedT0 = c.t0;
  }
  function panBy(seconds: number) {
    setWindow({ t0: t0 + seconds, t1: t1 + seconds });
  }
  function showPreset(s: number) {
    chosenSpan = s;
    pinnedT0 = null;
  }
  function reset() {
    chosenSpan = null;
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
  // The chart is this machine's memory over time: cloud jobs hold none of it, so they aren't drawn.
  const shown = $derived.by(() => {
    const local = jobs.filter((j) => !j.cloud);
    if (history.length === 0) return local;
    const live = new Set(jobs.map((j) => j.id));
    return [...local, ...history.filter((j) => !j.cloud && !live.has(j.id))];
  });
  // They get a lane per account under it instead, on the same time axis.
  const cloudShown = $derived.by(() => {
    const live = jobs.filter((j) => j.cloud);
    if (history.length === 0) return live;
    const ids = new Set(jobs.map((j) => j.id));
    return [...live, ...history.filter((j) => j.cloud && !ids.has(j.id))];
  });

  // Drag to pan; ctrl/⌘ + wheel (or a trackpad pinch) to zoom; horizontal wheel to pan.
  // On touch screens, one finger pans and two fingers pinch to zoom.
  let svgEl = $state<SVGSVGElement | null>(null);
  let drag: { x0: number; t0: number; moved: boolean } | null = null;
  let justDragged = false;
  let dragging = $state(false);
  const touches = new Map<number, number>(); // pointerId -> clientX
  let pinch: { d0: number; anchor: number; span0: number } | null = null;

  function localX(clientX: number): number {
    return svgEl ? clientX - svgEl.getBoundingClientRect().left : clientX;
  }
  function startPinch() {
    const [a, b] = [...touches.values()];
    pinch = { d0: Math.max(20, Math.abs(a - b)), anchor: timeAt(localX((a + b) / 2)), span0: t1 - t0 };
    drag = null;
    dragging = true;
    hideTip();
  }
  function onpointerdown(e: PointerEvent) {
    if (e.button !== 0) return;
    if (e.pointerType === "touch") {
      touches.set(e.pointerId, e.clientX);
      if (touches.size === 2) {
        startPinch();
        return;
      }
      if (touches.size > 2) return;
    }
    drag = { x0: e.clientX, t0, moved: false };
  }
  function onpointermove(e: PointerEvent) {
    if (e.pointerType !== "touch") hoverX = localX(e.clientX);
    if (touches.has(e.pointerId)) touches.set(e.pointerId, e.clientX);
    if (pinch && touches.size === 2) {
      const [a, b] = [...touches.values()];
      const s = pinch.span0 * (pinch.d0 / Math.max(20, Math.abs(a - b)));
      const start = pinch.anchor - ((localX((a + b) / 2) - L) / plotWidth) * s;
      setWindow({ t0: start, t1: start + s });
      return;
    }
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
  function onpointerup(e: PointerEvent) {
    touches.delete(e.pointerId);
    if (pinch) {
      if (touches.size < 2) pinch = null;
      justDragged = true;
      setTimeout(() => (justDragged = false));
      drag = null;
      dragging = false;
      return;
    }
    if (drag?.moved) {
      justDragged = true;
      setTimeout(() => (justDragged = false));
    }
    drag = null;
    dragging = false;
  }
  // Perfetto-style keys: hold W/S to zoom in/out (around the pointer when it's over the chart),
  // A/D to move earlier/later.
  const KEYS = new Set(["w", "a", "s", "d"]);
  const held = new Set<string>();
  let hoverX: number | null = null;
  let raf = 0;
  let lastFrame = 0;

  function typing(target: EventTarget | null): boolean {
    const el = target as HTMLElement | null;
    if (!el || !el.tagName) return false;
    return ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName) || el.isContentEditable;
  }
  function stepKeys(dt: number) {
    const zoom = (held.has("s") ? 1 : 0) - (held.has("w") ? 1 : 0);
    const pan = (held.has("d") ? 1 : 0) - (held.has("a") ? 1 : 0);
    if (zoom === 0 && pan === 0) return;
    const anchor = hoverX !== null && hoverX >= L && hoverX <= width - R ? timeAt(hoverX) : (t0 + t1) / 2;
    setWindow(keyStep(win, zoom, pan, dt, anchor, now));
  }
  function frame(ts: number) {
    if (held.size === 0) {
      raf = 0;
      return;
    }
    stepKeys(Math.min(0.1, (ts - lastFrame) / 1000));
    lastFrame = ts;
    raf = requestAnimationFrame(frame);
  }
  function onwindowkeydown(e: KeyboardEvent) {
    const k = e.key.toLowerCase();
    if (!KEYS.has(k) || e.ctrlKey || e.metaKey || e.altKey) return;
    if (typing(e.target) || document.querySelector('[aria-modal="true"]')) return;
    e.preventDefault();
    if (held.has(k)) return; // auto-repeat: the animation loop is already running
    held.add(k);
    stepKeys(1 / 30); // a quick tap still moves a little
    if (!raf) {
      lastFrame = performance.now();
      raf = requestAnimationFrame(frame);
    }
  }
  function onwindowkeyup(e: KeyboardEvent) {
    held.delete(e.key.toLowerCase());
  }
  function releaseKeys() {
    held.clear();
  }
  $effect(() => () => {
    if (raf) cancelAnimationFrame(raf);
  });

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
  const cloudById = $derived(new Map(cloudShown.map((j) => [j.id, j])));
  const lanes = $derived(cloudLanes(cloudShown, now, t0, t1));

  // Each lane: a header line naming the account, then one row per attempt running at once.
  const LANE_HEAD = 18, ROW_H = 20, ROW_GAP = 3, LANE_PAD = 6, LANES_GAP = 4;
  function rowY(top: number, row: number): number {
    return top + LANE_HEAD + row * (ROW_H + ROW_GAP);
  }
  const laneBoxes = $derived.by(() => {
    let top = height + LANES_GAP;
    return lanes.map((lane) => {
      const box = { lane, top, bottom: rowY(top, lane.rows) - ROW_GAP };
      top = box.bottom + LANE_PAD;
      return box;
    });
  });
  const lanesTop = $derived(height + LANES_GAP);
  const svgHeight = $derived(laneBoxes.length ? laneBoxes[laneBoxes.length - 1].bottom + 2 : height);
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
  interface CloudTip { x: number; y: number; job: JobView; lane: Lane; bar: LaneBar }
  let ctip = $state<CloudTip | null>(null);

  function showTip(e: MouseEvent, job: JobView, kind: BlockKind, start: number, end: number) {
    tip = { x: e.clientX + 14, y: e.clientY + 12, job, kind, start, end };
  }
  function showCloudTip(e: MouseEvent, job: JobView, lane: Lane, bar: LaneBar) {
    ctip = { x: e.clientX + 14, y: e.clientY + 12, job, lane, bar };
  }
  function hideTip() {
    tip = null;
    ctip = null;
  }

  function attemptLabel(job: JobView, bar: LaneBar): string {
    const of = job.spans.length > 1 ? `attempt ${bar.attempt} of ${job.spans.length}` : "";
    const span = bar.kind === "run"
      ? `running since ${when(bar.start, now)}${bar.until !== null ? `, approved until ~${when(bar.until, now)}` : ""}`
      : `ran ${when(bar.start, now)}–${when(bar.end, now)}`;
    return [span, of].filter(Boolean).join(" · ");
  }
  function cloudAria(job: JobView, lane: Lane, bar: LaneBar): string {
    return `#${job.id} ${job.name} on ${lane.target}, ${attemptLabel(job, bar)}`;
  }
  function cloudState(job: JobView, bar: LaneBar): string {
    if (bar.kind === "past") return `${bar.endKind ?? "ended"} · job ${job.state}`;
    const phase = job.cloud?.phase;
    return phase && phase !== job.state ? `${job.state} · ${phase}` : job.state;
  }
  function cloudCost(job: JobView): string {
    const c = job.cloud!;
    // A live attempt counts at the ceiling reserved for it (see CloudCard's `spentLine`).
    const live = job.spans.some(([, end]) => end === null);
    return `${money(c.job_spent)} ${live ? "spent or held" : "spent"}`;
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

<svelte:window onkeydown={onwindowkeydown} onkeyup={onwindowkeyup} onblur={releaseKeys} />

<div class="sec">
  <h3>
    Schedule
    <span class="dim">{following ? "memory over time" : `${when(t0, now)} – ${when(t1, now)}`}{loading ? " · loading…" : ""}</span>
    <span class="tlnav" role="toolbar" aria-label="Schedule range">
      {#each PRESETS as p (p.label)}
        <button class="chip" class:on={following && span === p.span} aria-pressed={following && span === p.span} onclick={() => showPreset(p.span)}>{p.label}</button>
      {/each}
      {#if !following || chosenSpan !== null}
        <button class="chip now" onclick={reset}>now</button>
      {/if}
    </span>
  </h3>
  <div class="tlwrap" bind:clientWidth={wrapperWidth}>
    {#if isEmpty}
      <div class="tlempty" style="height: {height}px">
        <img src={mascot.pick("hmm")} alt="" width="64" height="64" />
        <p>{following && t1 > now ? "Nothing scheduled." : loading ? "Looking back…" : "Nothing ran in this window."}</p>
      </div>
    {/if}
    <svg
      class="tl"
      class:dragging
      style="height: {svgHeight}px"
      bind:this={svgEl}
      role="presentation"
      {onpointerdown}
      {onpointermove}
      {onpointerup}
      onpointercancel={onpointerup}
      onpointerleave={() => (hoverX = null)}
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
          {@const c = jobColor(job)}
          {@const muted = mutedJobColor(job)}
          {@const label = w > 60 ? `#${job.id} ${job.name}` : w > 34 ? `#${job.id}` : ""}
          {@const clipId = `tl-clip-${uid}-${job.id}-${b.kind}-${i}`}
          <g
            role="button"
            tabindex="0"
            aria-label={ariaLabel(job, b.kind, b.start, b.end)}
            class="blk"
            style={dim?.(job) ? "opacity: 0.25" : undefined}
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
              fill={b.kind === "run" ? `${c}2e` : b.kind === "past" ? muted.fill : "#ffffff"}
              stroke={b.kind === "past" ? muted.border : c}
              stroke-width="1.8"
              stroke-dasharray={b.kind === "proj" ? "5 3" : undefined}
            />
            {#if b.kind !== "past"}
              <rect x={X} y={Y} width="4" height={h} rx="2" fill={c} />
            {/if}
            {#if label && h >= 20}
              <clipPath id={clipId}>
                <rect x={X} y={Y} width={w - 6} height={h} />
              </clipPath>
              <text clip-path="url(#{clipId})" x={X + 11} y={Y + Math.min(17, h / 2 + 4)} class="blklabel" class:past={b.kind === "past"}>{label}</text>
            {/if}
            {#if h > 34 && w > 80}
              <text clip-path="url(#{clipId})" x={X + 11} y={Y + 32} class="blksub">
                {b.kind === "proj" ? `~${when(b.start, now)}–${when(b.end, now)}` : b.kind === "past" ? `${when(b.start, now)}–${when(b.end, now)}` : `until ~${when(b.end, now)}`} · ★{job.bid}
              </text>
            {/if}
          </g>
        {/if}
      {/each}
      {#each laneBoxes as { lane, top, bottom } (lane.target)}
        <g class="lane" data-lane={lane.target}>
          <path d={CLOUD_PATH} transform="translate({L + 1} {top + 2}) scale(0.54)" class="laneic" />
          <text x={L + 17} y={top + 12} class="lanename">{lane.target}<tspan class="laneowner">{lane.owner ? ` · ${lane.owner}` : ""}</tspan></text>
          <rect x={L} y={top + LANE_HEAD - 2} width={plotWidth} height={bottom - top - LANE_HEAD + 4} rx="8" class="lanetrack" />
          {#each ticks as t (t)}
            <line x1={x(t)} x2={x(t)} y1={top + LANE_HEAD - 2} y2={bottom + 2} class="lanehour" />
          {/each}
          {#each lane.bars as bar (`${bar.id}:${bar.attempt}`)}
            {@const job = cloudById.get(bar.id)}
            {#if job}
              {@const X = x(bar.start) + 1}
              {@const Y = rowY(top, bar.row)}
              {@const w = Math.max(1.5, x(bar.end) - x(bar.start) - 2)}
              {@const full = Math.max(w, x(bar.until ?? bar.end) - x(bar.start) - 2)}
              {@const c = jobColor(job)}
              {@const muted = mutedJobColor(job)}
              {@const label = full > 60 ? `#${job.id} ${job.name}` : full > 34 ? `#${job.id}` : ""}
              {@const clipId = `tl-lclip-${uid}-${job.id}-${bar.attempt}`}
              <g
                role="button"
                tabindex="0"
                aria-label={cloudAria(job, lane, bar)}
                class="blk"
                style={dim?.(job) ? "opacity: 0.25" : undefined}
                onclick={() => open(job)}
                onkeydown={(e) => onBlockKeydown(e, job)}
                onmousemove={(e) => showCloudTip(e, job, lane, bar)}
                onmouseleave={hideTip}
              >
                {#if bar.kind === "run"}
                  {#if bar.until !== null}
                    <rect x={X} y={Y} width={full} height={ROW_H} rx={Math.min(7, full / 3)} fill="#ffffff" stroke={c} stroke-width="1.5" stroke-dasharray="5 3" />
                  {/if}
                  <rect x={X} y={Y} width={w} height={ROW_H} rx={Math.min(7, w / 3)} fill="{c}2e" stroke={c} stroke-width="1.5" />
                  <rect x={X} y={Y} width={Math.min(4, w)} height={ROW_H} rx="2" fill={c} />
                {:else}
                  <rect x={X} y={Y} width={w} height={ROW_H} rx={Math.min(7, w / 3)} fill={muted.fill} stroke={muted.border} stroke-width="1.5" />
                {/if}
                {#if label}
                  <clipPath id={clipId}>
                    <rect x={X} y={Y} width={full - 5} height={ROW_H} />
                  </clipPath>
                  <text clip-path="url(#{clipId})" x={X + (bar.kind === "run" ? 9 : 7)} y={Y + 14} class="lanelabel" class:past={bar.kind === "past"}>{label}</text>
                {/if}
              </g>
            {/if}
          {/each}
        </g>
      {/each}
      {#if now >= t0 && now <= t1}
        <line x1={x(now)} x2={x(now)} y1={T - 4} y2={height - B} class="nowline" />
        <circle cx={x(now)} cy={T - 4} r="3.5" class="nowdot" />
        {#if laneBoxes.length}
          <line x1={x(now)} x2={x(now)} y1={lanesTop + LANE_HEAD - 2} y2={svgHeight - 2} class="nowline" />
        {/if}
      {/if}
    </svg>
  </div>
  <div class="tlkey">
    <span><svg width="22" height="12"><rect x="1" y="1" width="20" height="10" rx="4" fill="#8a63d233" stroke="#8a63d2" stroke-width="1.5" /></svg>running</span>
    <span><svg width="22" height="12"><rect x="1" y="1" width="20" height="10" rx="4" fill="#fff" stroke="#8a63d2" stroke-width="1.5" stroke-dasharray="3 2" /></svg>projected</span>
    <span><svg width="22" height="12"><rect x="1" y="1" width="20" height="10" rx="4" fill="#8a63d217" stroke="#8a63d266" stroke-width="1.5" /></svg>finished</span>
    <span><svg width="10" height="14"><rect x="4" y="0" width="2" height="14" fill="#ff8fab" /></svg>now</span>
    {#if laneBoxes.length}
      <span><svg width="22" height="12"><rect x="1" y="1" width="20" height="10" rx="4" fill="#fff" stroke="#5b9bd5" stroke-width="1.5" stroke-dasharray="3 2" /><rect x="1" y="1" width="9" height="10" rx="3" fill="#5b9bd52e" stroke="#5b9bd5" stroke-width="1.5" /></svg>cloud: run so far, then approved time</span>
    {/if}
    <span class="hint mouse">drag or A/D to move · W/S or ctrl + scroll to zoom</span>
    <span class="hint touch">drag to move · pinch to zoom</span>
  </div>
</div>

{#if ctip}
  {@const c = ctip.job.cloud!}
  <div class="tip" style="left: {ctip.x}px; top: {ctip.y}px">
    <b>#{ctip.job.id} {ctip.job.name}</b><br />
    <span class="dim">{c.gpu} on {ctip.lane.target}{ownerNote(ctip.lane.owner ?? c.owner)}</span><br />
    {cloudState(ctip.job, ctip.bar)} · <span class="dim">{attemptLabel(ctip.job, ctip.bar)}</span><br />
    {cloudCost(ctip.job)}{c.job_cap !== null ? ` · cap ${money(c.job_cap)}` : ""}
  </div>
{/if}

{#if tip}
  <div class="tip" style="left: {tip.x}px; top: {tip.y}px">
    <b>#{tip.job.id} {tip.job.name}</b><br />
    <span class="dim">{kindLabel(tip.kind)} · {when(tip.start, now)}–{when(tip.end, now)}{tip.kind === "run" && tip.job.eta_source === "progress" ? " (from progress)" : ""}</span><br />
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
  .blklabel.past { fill: var(--ink-2); }
  .blksub { font-size: 11px; font-weight: 700; fill: var(--ink-2); }
  .nowline { stroke: var(--accent); stroke-width: 2; }
  .laneic { fill: var(--cloud-ic); }
  .lanename { font-size: 11.5px; font-weight: 900; fill: var(--ink-2); }
  .laneowner { font-weight: 700; fill: var(--ink-3); }
  .lanetrack { fill: #fdf7fa; stroke: var(--line); stroke-width: 1; }
  .lanehour { stroke: #f4eaef; }
  .lanelabel { font-size: 11.5px; font-weight: 800; fill: var(--ink); }
  .lanelabel.past { fill: var(--ink-2); }
  .nowdot { fill: var(--accent); }

  .tlempty { position: absolute; inset: 0; pointer-events: none; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; color: var(--ink-2); font-weight: 700; }
  .tlempty img { width: 64px; height: 64px; }
  .tlempty p { margin: 0; }

  .tlkey { display: flex; gap: 14px; font-size: 12px; font-weight: 700; color: var(--ink-2); margin-top: 6px; flex-wrap: wrap; }
  .tlkey span { display: inline-flex; align-items: center; gap: 5px; }
  .tlkey .hint { margin-left: auto; color: var(--ink-3); }
  .tlkey .hint.touch { display: none; }
  @media (pointer: coarse) {
    .tlkey .hint.mouse { display: none; }
    .tlkey .hint.touch { display: inline-flex; }
  }

  h3 { flex-wrap: wrap; }
  .tlnav { margin-left: auto; display: inline-flex; align-items: center; gap: 4px; flex-wrap: wrap; }
  .chip { border: 1.5px solid var(--line-2); background: var(--card); color: var(--ink-2); border-radius: 99px; min-width: 30px; padding: 3px 10px; font-size: 12px; font-weight: 800; line-height: 1.2; }
  .chip:hover { color: var(--ink); border-color: var(--accent); }
  .chip.on { background: var(--accent); border-color: var(--accent); color: #fff; }
  .chip.now { color: var(--accent-ink); border-color: var(--accent); }

  .tip { position: fixed; pointer-events: none; background: #3b3340; color: #fff; border-radius: 12px; padding: 8px 11px; font-size: 12.5px; font-weight: 700; box-shadow: 0 6px 18px rgba(0, 0, 0, 0.18); z-index: 40; line-height: 1.45; }
  .tip .dim { color: #cbbfcb; }
</style>
