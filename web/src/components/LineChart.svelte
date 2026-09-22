<script lang="ts">
  import { hm } from "../lib/format";

  interface Props {
    label: string;
    points: [number, number][];
    color: string;
    format: (v: number) => string;
    max?: number;
    note?: string;
  }
  let { label, points, color, format, max, note }: Props = $props();

  const values = $derived(points.map(([, v]) => v));
  const latest = $derived(values.length > 0 ? values[values.length - 1] : 0);
  const dataMax = $derived(values.length > 0 ? Math.max(...values) : 0);
  const scaleMax = $derived(max ?? (dataMax > 0 ? dataMax * 1.1 : 1));

  const TOP = 2;
  const BOTTOM = 54;

  function xAt(i: number): number {
    return points.length > 1 ? (i / (points.length - 1)) * 100 : 0;
  }
  function yAt(v: number): number {
    return BOTTOM - (v / scaleMax) * (BOTTOM - TOP);
  }

  const path = $derived.by(() => {
    if (points.length < 2) return "";
    return points.map(([, v], i) => `${i ? "L" : "M"}${xAt(i).toFixed(1)},${yAt(v).toFixed(1)}`).join("");
  });
  const fillPath = $derived(path ? `${path} L100,56 L0,56Z` : "");

  let svgEl = $state<SVGSVGElement | null>(null);
  let hoverIndex = $state<number | null>(null);
  const hover = $derived(hoverIndex !== null ? points[hoverIndex] : null);

  function onmove(e: PointerEvent): void {
    if (points.length < 2 || !svgEl) return;
    const rect = svgEl.getBoundingClientRect();
    if (rect.width === 0) return;
    const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    hoverIndex = Math.round(ratio * (points.length - 1));
  }
  function onleave(): void {
    hoverIndex = null;
  }
</script>

<div class="chart">
  <div class="l">{label} <b>{format(latest)}</b></div>
  {#if points.length < 2}
    <p class="chartempty dim">not enough data yet</p>
  {:else}
    <div class="chartinner">
      <svg
        bind:this={svgEl}
        viewBox="0 0 100 56"
        preserveAspectRatio="none"
        role="img"
        aria-label={`${label}: latest ${format(latest)}, max ${format(dataMax)}`}
        onpointermove={onmove}
        onpointerleave={onleave}
      >
        <path d={fillPath} fill="{color}1f" />
        <path d={path} fill="none" stroke={color} stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round" />
        {#if hoverIndex !== null}
          <line x1={xAt(hoverIndex)} x2={xAt(hoverIndex)} y1="0" y2="56" class="crosshair" />
        {/if}
      </svg>
      {#if hover}
        <div class="tip" style="left: {xAt(hoverIndex ?? 0)}%">
          <b>{format(hover[1])}</b> <span class="dim">{hm(hover[0])}</span>
        </div>
      {/if}
    </div>
  {/if}
  {#if note}<div class="small dim chartnote">{note}</div>{/if}
</div>

<style>
  .chartinner { position: relative; }
  .crosshair { stroke: var(--ink-3); stroke-width: 1; vector-effect: non-scaling-stroke; }
  .tip {
    position: absolute; bottom: 100%; transform: translateX(-50%); white-space: nowrap;
    background: #3b3340; color: #fff; border-radius: 10px; padding: 3px 8px;
    font-size: 11px; font-weight: 700; pointer-events: none; margin-bottom: 4px; z-index: 1;
  }
  .tip .dim { color: #cbbfcb; }
  .chartempty { margin: 14px 0 2px; }
  .chartnote { margin-top: 4px; }
</style>
