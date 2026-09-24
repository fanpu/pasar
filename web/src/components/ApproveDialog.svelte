<script lang="ts">
  import * as api from "../lib/api";
  import { gpuOn, money, runTimeHow } from "../lib/cloud";
  import { dur } from "../lib/format";
  import type { CloudTarget, JobView } from "../lib/types";
  import Modal from "./Modal.svelte";

  interface Props {
    job: JobView;
    // The target the job runs on, for its GPU's memory and live rate; `null` if no longer listed.
    target: CloudTarget | null;
    // Give a *running* job more time (`?extend=1`) rather than approve an awaiting one.
    extend?: boolean;
    onclose: () => void;
    // Called once the server has agreed, with a line fit for a toast.
    ondone?: (message: string) => void;
  }
  let { job, target, extend = false, onclose, ondone = () => {} }: Props = $props();

  // Every job handed to this dialog is a cloud job; the fallback only keeps the types honest.
  const c = $derived(job.cloud!);
  const gpu = $derived(gpuOn(target, c.gpu));
  const capLeft = $derived(c.job_cap === null ? null : Math.max(0, c.job_cap - c.job_spent));

  const title = $derived(extend ? `Give #${job.id} ${job.name} more time?` : `Approve #${job.id} ${job.name}?`);
  const label = $derived.by(() => {
    if (extend) return c.job_cap === null ? "Give more time" : `Give more time · within ${money(c.job_cap)} cap`;
    return c.max_cost === null ? "Approve" : `Approve · up to ${money(c.max_cost)}`;
  });

  // Nothing may be approved without its worst case on the button: a price that couldn't be had
  // (the rate cache's last fetch failed, say) would otherwise approve at one nobody saw.
  const unpriced = $derived(!extend && c.max_cost === null);

  let busy = $state(false);
  let error = $state<string | null>(null);

  async function confirm(): Promise<void> {
    if (busy || unpriced) return;
    busy = true;
    error = null;
    try {
      await api.approve(job.id, extend);
      ondone(extend ? `#${job.id} got more time` : `#${job.id} approved · up to ${money(c.max_cost)}`);
      onclose();
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }
</script>

<Modal {title} {onclose}>
  <div class="kv facts-list">
    <span class="k">GPU</span>
    <span class="v">
      {c.gpu} on {c.target}{target?.owner ? ` · ${target.owner}'s account` : ""}
      {#if gpu}<span class="dim">({gpu.memory_gb != null ? `${gpu.memory_gb}GB · ` : ""}{money(gpu.hourly_rate)}/hour now)</span>{/if}
    </span>

    {#if extend}
      <span class="k">Approved so far</span>
      <span class="v">{c.approved_seconds === null ? "–" : dur(c.approved_seconds)} of run time, up to {money(c.max_cost)}</span>
      <span class="k">Its pace needs</span>
      <span class="v">{c.needs_more_time === null ? "no more right now" : `about ${dur(c.needs_more_time)} more`}</span>
      <span class="k">New ceiling</span>
      <span class="v">priced at today's rate when you confirm</span>
    {:else}
      <span class="k">Run time</span>
      <span class="v">
        {c.approved_seconds === null ? "–" : dur(c.approved_seconds)}
        <span class="dim">({runTimeHow(c)})</span>
      </span>
      <span class="k">Estimated cost</span>
      <span class="v">{money(c.estimated_cost)}</span>
      <span class="k">This attempt can spend</span>
      <span class="v">{c.max_cost === null ? "can't be priced right now" : `up to ${money(c.max_cost)}`}</span>
    {/if}

    <span class="k">Job cap</span>
    <span class="v">
      {#if c.job_cap === null}
        {money(c.job_spent)} spent · no cap (target not configured)
      {:else}
        {money(c.job_spent)} of {money(c.job_cap)} spent · {money(capLeft)} left
      {/if}
    </span>
  </div>

  {#if extend}
    <p class="mnote">
      The daemon adds a little over what its pace needs and prices it now. It refuses rather than
      go past the job's own --max-cost, the job cap, or the target's budgets.
    </p>
  {:else}
    <p class="mnote">Results it saves come home to pasar's pull directory when it finishes.</p>
  {/if}

  {#if error}<p class="err" role="alert">{error}</p>{/if}
  <div class="macts">
    <button class="btn" type="button" disabled={busy} onclick={onclose}>Cancel</button>
    <button class="btn primary" type="button" disabled={busy || unpriced} onclick={confirm}>{label}</button>
  </div>
</Modal>

<style>
  .facts-list { grid-template-columns: 150px 1fr; margin-top: 4px; font-size: 13.5px; }
  .facts-list .v { font-weight: 800; }
  .facts-list .v .dim { font-weight: 700; }
  .mnote { font-size: 12.5px; color: var(--ink-2); font-weight: 700; margin: 10px 0 0; line-height: 1.5; }
  .err { color: var(--fail); font-weight: 800; font-size: 12.5px; margin: 10px 0 0; }
  .macts { display: flex; gap: 8px; margin-top: 16px; }
  .macts .btn { flex: 1; }
  /* The worst-case figure reads as one phrase: give it the room rather than wrap it. */
  .macts .btn.primary { flex: 2; white-space: nowrap; }
  .btn:disabled { opacity: .6; cursor: default; }
</style>
