<script lang="ts">
  import { getJob, cancelJob, setBid, restartJob, ApiError } from "../lib/api";
  import { mascot } from "../lib/mascot.svelte";
  import { hm } from "../lib/format";
  import JobChip from "./JobChip.svelte";
  import StatePill from "./StatePill.svelte";
  import Facts from "./Facts.svelte";
  import ReasonBox from "./ReasonBox.svelte";
  import AttemptsBar from "./AttemptsBar.svelte";
  import BidDialog from "./BidDialog.svelte";
  import type { JobDetail, JobView } from "../lib/types";

  interface Props {
    id: number;
    live: JobView | null;
    now: number;
    grafanaUrl: string | null;
    onclose: () => void;
    onrestartwith: (job: JobDetail) => void;
  }
  let { id, live, now, grafanaUrl, onclose, onrestartwith }: Props = $props();

  const FINISHED = new Set<JobView["state"]>(["completed", "failed", "cancelled"]);
  const TABS = [
    { key: "overview", label: "Overview" },
    { key: "logs", label: "Logs" },
    { key: "metrics", label: "Metrics" },
    { key: "events", label: "Events" },
  ] as const;
  type TabKey = (typeof TABS)[number]["key"];

  let detail = $state<JobDetail | null>(null);
  let notFound = $state(false);
  let headingEl = $state<HTMLElement | null>(null);
  let activeTab = $state<TabKey>("overview");
  let showBid = $state(false);
  let busy = $state<"cancel" | "restart" | null>(null);
  let actionError = $state<string | null>(null);

  const jobView = $derived.by((): JobView | null => {
    if (live) return live;
    if (detail) return { ...detail, attempts: detail.attempts.length };
    return null;
  });

  async function load(): Promise<void> {
    try {
      const d = await getJob(id);
      detail = d;
      notFound = false;
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        notFound = true;
        detail = null;
      } else {
        throw e;
      }
    }
  }

  // Reload attempts whenever the job first mounts or its state/attempt count changes, without
  // refetching on every snapshot tick (e.g. just a usage number changing).
  let lastKey = "";
  $effect(() => {
    const key = `${id}:${live?.state ?? ""}:${live?.attempts ?? ""}`;
    if (key === lastKey) return;
    lastKey = key;
    void load();
  });

  $effect(() => {
    headingEl?.focus();
  });

  function onkeydown(e: KeyboardEvent): void {
    if (e.key === "Escape" && !showBid) onclose();
  }

  const mascotImg = $derived(mascot.pick(notFound ? "hmm" : "thinking"));

  async function doCancel(): Promise<void> {
    if (!jobView) return;
    if (!window.confirm(`Cancel #${jobView.id} ${jobView.name}?`)) return;
    busy = "cancel";
    actionError = null;
    try {
      await cancelJob(id);
    } catch (e) {
      actionError = e instanceof ApiError ? e.message : String(e);
    } finally {
      busy = null;
    }
  }

  async function doRestart(): Promise<void> {
    busy = "restart";
    actionError = null;
    try {
      await restartJob(id);
    } catch (e) {
      actionError = e instanceof ApiError ? e.message : String(e);
    } finally {
      busy = null;
    }
  }

  function doRestartWith(): void {
    if (detail) onrestartwith(detail);
  }

  async function saveBid(bid: number): Promise<void> {
    actionError = null;
    try {
      await setBid(id, bid);
    } catch (e) {
      actionError = e instanceof ApiError ? e.message : String(e);
      throw e;
    }
  }

  function gitShort(commit: string | null): string {
    return commit ? commit.slice(0, 7) : "–";
  }

  function graceText(job: JobView): string {
    return job.preemptible ? `${job.grace}s · preemptible` : "not preemptible";
  }

  function grafanaHref(url: string, job: JobView): string {
    const from = Math.round((job.spans[0]?.[0] ?? job.start_time ?? job.submit_time) * 1000);
    const to = job.end_time !== null ? `${Math.round(job.end_time * 1000)}` : "now";
    return `${url}?from=${from}&to=${to}`;
  }

  const showAttempts = $derived(detail !== null && (detail.attempts.length > 1 || detail.preemptions > 0));
</script>

<svelte:window onkeydown={onkeydown} />
<button type="button" class="scrim open" aria-label="Close" onclick={onclose}></button>
<div class="drawer open" role="dialog" aria-modal="true" aria-labelledby="jobpanel-heading" data-show={activeTab}>
  {#if notFound}
    <div class="dhead">
      <h2 id="jobpanel-heading" tabindex="-1" bind:this={headingEl}>No job #{id}.</h2>
      <button class="x" type="button" onclick={onclose} aria-label="Close">✕</button>
    </div>
    <img src={mascotImg} alt="" width="72" height="72" />
  {:else if jobView === null}
    <div class="dhead">
      <h2 id="jobpanel-heading" tabindex="-1" bind:this={headingEl}>Loading…</h2>
      <button class="x" type="button" onclick={onclose} aria-label="Close">✕</button>
    </div>
    <img src={mascotImg} alt="" width="72" height="72" />
  {:else}
    <div class="dhead">
      <JobChip id={jobView.id} />
      <h2 id="jobpanel-heading" tabindex="-1" bind:this={headingEl}>{jobView.name}</h2>
      <StatePill job={jobView} />
      <button class="x" type="button" onclick={onclose} aria-label="Close">✕</button>
    </div>
    <div class="note">
      {jobView.note}
      {#if jobView.submitter}<span class="faint">· {jobView.submitter}</span>{/if}
    </div>

    <div class="tabs" role="tablist">
      {#each TABS as t (t.key)}
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === t.key}
          class:on={activeTab === t.key}
          onclick={() => (activeTab = t.key)}
        >
          {t.label}
        </button>
      {/each}
    </div>

    <div class="acts">
      {#if FINISHED.has(jobView.state)}
        <button class="btn primary" type="button" disabled={busy !== null} onclick={doRestart}>↻ restart</button>
        <button class="btn" type="button" disabled={busy !== null || detail === null} onclick={doRestartWith}>
          restart with changes…
        </button>
      {:else}
        <button class="btn" type="button" disabled={busy !== null} onclick={() => (showBid = true)}>
          ★ bid {jobView.bid} ✎
        </button>
        <button class="btn danger" type="button" disabled={busy !== null} onclick={doCancel}>cancel</button>
      {/if}
    </div>
    {#if actionError}<div class="acterr">{actionError}</div>{/if}

    <section data-tab="overview">
      <ReasonBox job={jobView} {detail} />
      <Facts job={jobView} {detail} {now} />
      {#if showAttempts && detail}
        <AttemptsBar {detail} {now} />
      {/if}
    </section>

    <section data-tab="metrics"></section>
    <section data-tab="logs overview"></section>
    <section data-tab="events overview"></section>

    <div class="kv" data-tab="overview">
      <span class="k">command</span><span class="mono">{jobView.command}</span>
      <span class="k">directory</span><span class="mono">{jobView.cwd}</span>
      <span class="k">git</span><span class="mono">{gitShort(jobView.git_commit)}</span>
      <span class="k">grace</span><span>{graceText(jobView)}</span>
      <span class="k">retries</span><span>{jobView.retries_used} of {jobView.retries} used</span>
      <span class="k">tags</span><span>{jobView.tags.length > 0 ? jobView.tags.join(", ") : "–"}</span>
      <span class="k">submitted</span><span>{hm(jobView.submit_time)}</span>
      {#if grafanaUrl}
        <span class="k">grafana</span>
        <span><a href={grafanaHref(grafanaUrl, jobView)} target="_blank" rel="noopener">Grafana</a></span>
      {/if}
    </div>
  {/if}
</div>

{#if showBid && jobView}
  <BidDialog bid={jobView.bid} onsave={saveBid} onclose={() => (showBid = false)} />
{/if}
