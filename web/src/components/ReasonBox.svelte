<script lang="ts">
  import { dur, fmtGib, isOom, reasonLabel } from "../lib/format";
  import { mascot } from "../lib/mascot.svelte";
  import type { JobDetail, JobView } from "../lib/types";

  interface Props {
    job: JobView;
    detail: JobDetail | null;
  }
  let { job, detail }: Props = $props();

  // Pull the mascot state out as a primitive first: `job` is a fresh object every snapshot tick
  // even when nothing relevant changed, and deriving `image` straight off `job.reason` would
  // re-pick a random variant every tick instead of once per shown state.
  const reasonState = $derived(isOom(job.reason) ? "oom" : job.reason === "lost" ? "confused" : "failed");
  const image = $derived(mascot.pick(reasonState));

  const tailLines = $derived.by((): string[] => {
    if (detail === null || detail.attempts.length === 0) return [];
    const last = detail.attempts[detail.attempts.length - 1];
    if (!last.log_tail) return [];
    return last.log_tail.split("\n").slice(-8);
  });
</script>

{#if job.state === "failed"}
  <div class="reason">
    <img src={image} alt="" />
    <div>
      <div class="t">{reasonLabel(job.reason)}</div>
      <div class="m mono">{job.summary}</div>
      {#if tailLines.length > 0}
        <pre class="tail">{tailLines.join("\n")}</pre>
      {/if}
      <div class="m">
        peaked at {fmtGib(job.peak)} of {fmtGib(job.limit)} · ran {dur(job.run_time)} · retries {job.retries_used} of {job.retries}
      </div>
    </div>
  </div>
{/if}

<style>
  .reason { display: flex; gap: 12px; align-items: center; background: var(--fail-bg); border-radius: 18px; padding: 12px 14px; margin-bottom: 12px; }
  .reason img { width: 84px; height: 84px; flex: none; }
  .reason .t { font-weight: 900; color: var(--fail); font-size: 16px; }
  .reason .m { margin-top: 3px; font-size: 12px; color: #7b3a47; word-break: break-word; }
  .tail {
    background: #2f2933; color: #e9e0ea; border-radius: 12px; padding: 8px 10px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; line-height: 1.5;
    margin: 6px 0; max-height: 140px; overflow-y: auto; white-space: pre-wrap; word-break: break-word;
  }
</style>
