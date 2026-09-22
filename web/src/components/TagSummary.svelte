<script lang="ts">
  import Modal from "./Modal.svelte";
  import { ApiError, cancelJob, restartJob } from "../lib/api";
  import { jobColor } from "../lib/colors";
  import { dur } from "../lib/format";
  import { tagSummary, type TagSummary as TagSummaryData } from "../lib/jobfilter";
  import type { JobView } from "../lib/types";

  interface Props {
    tag: string;
    jobs: JobView[];
  }
  let { tag, jobs }: Props = $props();

  const summary = $derived<TagSummaryData>(tagSummary(jobs));
  const color = $derived(jobColor({ id: 0, tags: [tag] }));
  const finished = $derived(summary.completed + summary.failed);
  const queuedJobs = $derived(jobs.filter((j) => j.state === "queued"));
  const failedJobs = $derived(jobs.filter((j) => j.state === "failed"));

  type Action = "cancel" | "retry";
  let confirming = $state<Action | null>(null);
  let running = $state(false);
  let result = $state<string | null>(null);

  function plural(n: number): string {
    return n === 1 ? "job" : "jobs";
  }

  function open(action: Action): void {
    result = null;
    confirming = action;
  }
  function close(): void {
    if (running) return;
    confirming = null;
  }

  /** Runs `call` for every job in `targets` one at a time (not in parallel), so a burst of cancels
   * or restarts doesn't hammer the server all at once. Failures are collected but don't stop the
   * rest of the batch — the first error message is reported alongside the count. */
  async function runBulk(targets: JobView[], call: (id: number) => Promise<unknown>, verb: string): Promise<void> {
    running = true;
    let ok = 0;
    let firstError: string | null = null;
    for (const job of targets) {
      try {
        await call(job.id);
        ok++;
      } catch (e) {
        firstError ??= e instanceof ApiError ? e.message : String(e);
      }
    }
    running = false;
    confirming = null;
    const failedCount = targets.length - ok;
    result = failedCount === 0 ? `${verb} ${ok}` : `${verb} ${ok} · ${failedCount} couldn't be: ${firstError}`;
  }

  function confirmCancel(): void {
    void runBulk(queuedJobs, cancelJob, "cancelled");
  }
  function confirmRetry(): void {
    void runBulk(failedJobs, (id) => restartJob(id, {}), "retried");
  }
</script>

<div class="summary" style="background: {color}14; border-color: {color}55">
  <div class="sfact">
    <div class="l">jobs</div>
    <div class="v">{summary.total}</div>
    <div class="s">{summary.running} running · {summary.queued} queued</div>
  </div>
  <div class="sfact">
    <div class="l">success rate</div>
    <div class="v">{finished > 0 ? `${summary.completed} / ${finished}` : "–"}</div>
    {#if finished > 0}<div class="s">{summary.failed} failed</div>{/if}
  </div>
  <div class="sfact">
    <div class="l">GPU time</div>
    <div class="v">{dur(summary.gpuSeconds)}</div>
    {#if summary.queuedSeconds > 0}<div class="s">~{dur(summary.queuedSeconds)} still queued</div>{/if}
  </div>
  <div class="sfact">
    <div class="l">typical run</div>
    <div class="v">{summary.medianRun !== null ? dur(summary.medianRun) : "–"}</div>
    {#if summary.medianEstimate !== null}
      <div class="s">estimates said {dur(summary.medianEstimate)}{summary.estimateOk ? " ✓" : ""}</div>
    {/if}
  </div>
  <div class="sacts">
    {#if queuedJobs.length > 0}
      <button type="button" class="btn danger" onclick={() => open("cancel")}>cancel {queuedJobs.length} queued</button>
    {/if}
    {#if failedJobs.length > 0}
      <button type="button" class="btn" onclick={() => open("retry")}>retry {failedJobs.length} failed</button>
    {/if}
    {#if result}<div class="result" class:err={result.includes("couldn't be")}>{result}</div>{/if}
  </div>
</div>

{#if confirming === "cancel"}
  <Modal title={`Cancel ${queuedJobs.length} queued ${tag} ${plural(queuedJobs.length)}?`} onclose={close}>
    <div class="macts">
      <button type="button" class="btn danger" disabled={running} onclick={confirmCancel}>
        Cancel {queuedJobs.length} {plural(queuedJobs.length)}
      </button>
      <button type="button" class="btn" disabled={running} onclick={close}>Back</button>
    </div>
  </Modal>
{:else if confirming === "retry"}
  <Modal title={`Retry ${failedJobs.length} failed ${tag} ${plural(failedJobs.length)}?`} onclose={close}>
    <div class="macts">
      <button type="button" class="btn primary" disabled={running} onclick={confirmRetry}>
        Retry {failedJobs.length} {plural(failedJobs.length)}
      </button>
      <button type="button" class="btn" disabled={running} onclick={close}>Back</button>
    </div>
  </Modal>
{/if}

<style>
  .summary {
    display: grid; grid-template-columns: repeat(4, 1fr) auto; gap: 10px;
    border: 1.5px solid; border-radius: 18px; padding: 10px; margin-bottom: 12px; align-items: center;
  }
  .sfact { background: var(--card); border-radius: 14px; padding: 8px 12px; }
  .sfact .l { font-size: 11.5px; font-weight: 800; color: var(--ink-2); }
  .sfact .v { font-size: 17px; font-weight: 900; }
  .sfact .s { font-size: 12px; font-weight: 700; color: var(--ink-2); }
  .sacts { display: flex; flex-direction: column; gap: 6px; }
  .result { font-size: 12px; font-weight: 800; color: var(--ink-2); }
  .result.err { color: var(--fail); }
  .macts { display: flex; gap: 8px; }

  @media (max-width: 760px) {
    .summary { grid-template-columns: 1fr 1fr; }
    .sacts { grid-column: 1 / -1; flex-direction: row; }
    .sacts .btn { flex: 1; }
  }
</style>
