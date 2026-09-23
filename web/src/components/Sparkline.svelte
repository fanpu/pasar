<script lang="ts">
  interface Props {
    points: [number, number][];
    color: string;
    height?: number;
    /** How the min–max summary reads to a screen reader. Whole numbers suit the GPU tiles
     * (watts, °C, %); a loss of 0.412 needs decimals or it announces as "0–0". */
    format?: (v: number) => string;
  }
  let { points, color, height = 30, format = (v) => String(Math.round(v)) }: Props = $props();

  const values = $derived(points.map(([, v]) => v));
  const lo = $derived(values.length ? Math.min(...values) : 0);
  const hi = $derived(values.length ? Math.max(...values) : 0);
  const label = $derived(`${format(lo)}–${format(hi)}`);

  const top = 3;
  const path = $derived.by(() => {
    if (values.length === 0) return "";
    const span = hi - lo || 1;
    const bottom = height - top;
    return values
      .map((v, i) => {
        const x = values.length > 1 ? (i / (values.length - 1)) * 100 : 0;
        const y = bottom - ((v - lo) / span) * (bottom - top);
        return `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join("");
  });
  const fillPath = $derived(path ? `${path} L100,${height} L0,${height}Z` : "");
</script>

{#if values.length > 0}
  <svg class="spark" style="height: {height}px" viewBox="0 0 100 {height}" preserveAspectRatio="none" role="img" aria-label={label}>
    <path d={fillPath} fill="{color}22" />
    <path d={path} fill="none" stroke={color} stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round" />
  </svg>
{/if}

<style>
  .spark { width: 100%; display: block; margin-top: 4px; }
</style>
