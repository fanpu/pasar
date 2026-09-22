<script lang="ts">
  import { gib } from "../lib/format";
  import { jobColor } from "../lib/colors";
  import Sparkline from "./Sparkline.svelte";
  import type { GpuSeries, JobView, StatusView } from "../lib/types";

  interface Props {
    status: StatusView;
    jobs: JobView[];
    gpu: GpuSeries | null;
  }
  let { status, jobs, gpu }: Props = $props();

  // Stopping jobs are still holding their memory until they actually exit, so they belong in the
  // pool bar too — otherwise it under-counts what's reserved right as a preemption is in flight.
  const activeJobs = $derived(jobs.filter((j) => j.state === "running" || j.state === "stopping"));

  function reserved(j: JobView): number {
    return Math.max(j.limit, j.usage ?? 0);
  }

  // status.pool can be 0 (e.g. no GPU reachable yet); guard so the bar segments never compute a
  // NaN/Infinity width.
  function pct(value: number): number {
    return status.pool > 0 ? (value / status.pool) * 100 : 0;
  }

  function latest(series: [number, number][] | undefined): number | null {
    if (!series || series.length === 0) return null;
    return series[series.length - 1][1];
  }

  const power = $derived(latest(gpu?.power_w));
  const temp = $derived(latest(gpu?.temp_c));
  const util = $derived(latest(gpu?.util_pct));
</script>

<div class="tiles">
  <div class="tile">
    <div class="l"><span>memory pool</span><span>{gib(status.free)} GiB free</span></div>
    <div class="n">{gib(status.reserved)}&nbsp;<span>/ {gib(status.pool)} GiB reserved</span></div>
    <div class="pool">
      {#each activeJobs as j (j.id)}
        <i style="width: {pct(reserved(j))}%; background: {jobColor(j.id)}"></i>
      {/each}
      {#if status.external > 0}
        <i style="width: {pct(status.external)}%; background: var(--ink-3)"></i>
      {/if}
    </div>
    <div class="poolkey">
      {#each activeJobs as j (j.id)}
        <span><i style="background: {jobColor(j.id)}"></i>#{j.id} {gib(reserved(j))} GiB</span>
      {/each}
      {#if status.external > 0}
        <span><i style="background: var(--ink-3)"></i>other</span>
      {/if}
      <span><i style="background: #f6eef2"></i>free</span>
    </div>
  </div>

  <div class="tile">
    <div class="l"><span>power</span><span class="faint">GPU</span></div>
    <div class="n">{#if power !== null}{Math.round(power)}<span> W</span>{:else}–{/if}</div>
    {#if gpu && gpu.power_w.length > 0}
      <Sparkline points={gpu.power_w} color="#d9577f" />
    {/if}
  </div>

  <div class="tile">
    <div class="l"><span>temperature</span><span class="faint">GPU</span></div>
    <div class="n">{#if temp !== null}{Math.round(temp)}<span> °C</span>{:else}–{/if}</div>
    {#if gpu && gpu.temp_c.length > 0}
      <Sparkline points={gpu.temp_c} color="#c9761f" />
    {/if}
  </div>

  <div class="tile">
    <div class="l"><span>utilisation</span><span class="faint">GPU</span></div>
    <div class="n">{#if util !== null}{Math.round(util)}<span> %</span>{:else}–{/if}</div>
    {#if gpu && gpu.util_pct.length > 0}
      <Sparkline points={gpu.util_pct} color="#3b8fd9" />
    {/if}
  </div>
</div>

<style>
  .tiles { display: grid; grid-template-columns: 2fr 1fr 1fr 1fr; gap: 12px; margin: 16px 0; }
  .tile { background: var(--card); border: 1.5px solid var(--line); border-radius: var(--r); padding: 12px 15px; box-shadow: var(--shadow); min-width: 0; }
  .tile .l { font-size: 12px; font-weight: 800; color: var(--ink-2); display: flex; justify-content: space-between; }
  .tile .n { font-size: 26px; font-weight: 900; letter-spacing: -.3px; line-height: 1.15; }
  .tile .n span { font-size: 14px; color: var(--ink-2); font-weight: 800; }
  .pool { display: flex; gap: 2px; height: 12px; margin-top: 8px; border-radius: 99px; overflow: hidden; background: #f6eef2; }
  .pool i { height: 100%; }
  .pool i:first-child { border-radius: 99px 0 0 99px; }
  .poolkey { display: flex; gap: 10px; margin-top: 6px; font-size: 11.5px; font-weight: 700; color: var(--ink-2); flex-wrap: wrap; }
  .poolkey i { display: inline-block; width: 8px; height: 8px; border-radius: 3px; margin-right: 4px; vertical-align: 0; }

  @media (max-width: 760px) {
    .tiles { grid-template-columns: 1fr 1fr 1fr; gap: 8px; }
    .tiles .tile:first-child { grid-column: 1 / -1; }
    .tile { padding: 10px 11px; }
    .tile .n { font-size: 20px; }
    .tile .l .faint { display: none; }
  }
</style>
