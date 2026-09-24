<script lang="ts">
  import * as api from "../lib/api";
  import { gpuLabel, money, persistLine, phaseLook, whyBack } from "../lib/cloud";
  import { jobColor } from "../lib/colors";
  import { dur } from "../lib/format";
  import type { CloudBlock, CloudTarget, JobView } from "../lib/types";
  import ApproveDialog from "./ApproveDialog.svelte";
  import JobChip from "./JobChip.svelte";
  import Modal from "./Modal.svelte";
  import StatePill from "./StatePill.svelte";

  interface Props {
    cloud: CloudBlock;
    // The snapshot's jobs: the running cloud ones are drawn from here (the cloud block itself
    // only lists the running jobs that need a person, in `needs_time`).
    jobs?: JobView[];
    now: number;
    onopen?: (id: number) => void;
    // A short line for the app's toast after an approve, extend or reject goes through.
    onnotice?: (message: string) => void;
  }
  let { cloud, jobs = [], now, onopen = () => {}, onnotice = () => {} }: Props = $props();

  function targetOf(job: JobView): CloudTarget | null {
    return cloud.targets.find((t) => t.name === job.cloud?.target) ?? null;
  }

  // Jobs flagged as needing more time first (they want a person), then the rest, newest first.
  const running = $derived.by(() => {
    const flagged = new Set(cloud.needs_time.map((j) => j.id));
    const rest = jobs
      .filter((j) => j.cloud !== null && (j.state === "running" || j.state === "stopping") && !flagged.has(j.id))
      .sort((a, b) => b.id - a.id);
    return [...cloud.needs_time, ...rest];
  });

  function pct(part: number, whole: number): number {
    return whole > 0 ? Math.max(0, Math.min(100, (part / whole) * 100)) : 0;
  }

  function elapsed(job: JobView): number {
    return job.start_time === null ? 0 : Math.max(0, now - job.start_time);
  }

  function priceLine(job: JobView): string {
    const c = job.cloud!;
    const buys = c.approved_seconds === null ? "" : `buys ${dur(c.approved_seconds)} · `;
    return `${buys}est ${money(c.estimated_cost)} · up to ${money(c.max_cost)}`;
  }

  function targetLine(t: CloudTarget): string {
    const name = t.configured ? t.name : `${t.name} (not set up)`;
    return `${name} · ${money(t.spent_today)} today / ${money(t.daily_budget)} · `
      + `${money(t.spent_month)} this month / ${money(t.monthly_budget)} · job cap ${money(t.max_job_cost)}`;
  }

  // `job_spent` counts a live attempt at the ceiling reserved for it, not what it has burnt yet
  // (see `Ledger.job_spent`), so a job a minute in must not read as having spent it all.
  function spentLine(job: JobView): string {
    const c = job.cloud!;
    const held = `${money(c.job_spent)} spent or held`;
    return c.job_cap === null ? held : `${held} · cap ${money(c.job_cap)}`;
  }

  function flaggedToExtend(job: JobView): boolean {
    return job.state === "running" && job.cloud?.needs_more_time != null;
  }

  // Which job the approve dialog is for, never a copy of it: the dialog reads the job live from
  // the snapshot on every render, so the price it confirms is the one the daemon holds now.
  let approving = $state<{ id: number; extend: boolean } | null>(null);
  // The last live copy, shown (confirm disabled) only if the job leaves its list while the dialog
  // is open — approved or rejected elsewhere, expired, finished — rather than vanishing mid-read.
  let lastSeen: JobView | null = null;
  const approvingLive = $derived.by(() => {
    if (approving === null) return null;
    const { id, extend } = approving;
    if (!extend) return cloud.awaiting.find((j) => j.id === id) ?? null;
    return running.find((j) => j.id === id && j.state === "running") ?? null;
  });
  const approvingJob = $derived.by(() => {
    if (approvingLive !== null) lastSeen = approvingLive;
    return approving !== null && lastSeen?.id === approving.id ? lastSeen : null;
  });
  const approvingGone = $derived.by(() => {
    if (approving === null || approvingLive !== null) return null;
    return approving.extend
      ? `#${approving.id} isn't running any more.`
      : `#${approving.id} isn't waiting for approval any more: it was approved, rejected or expired elsewhere.`;
  });
  let rejecting = $state<JobView | null>(null);
  let rejectBusy = $state(false);
  let rejectError = $state<string | null>(null);

  function askReject(job: JobView): void {
    rejectError = null;
    rejecting = job;
  }

  // Like the approve dialog: nothing closes the question while its request is in flight.
  function keepIt(): void {
    if (rejectBusy) return;
    rejecting = null;
  }

  async function confirmReject(): Promise<void> {
    if (rejecting === null || rejectBusy) return;
    const id = rejecting.id;
    rejectBusy = true;
    rejectError = null;
    try {
      await api.reject(id);
      rejecting = null;
      onnotice(`#${id} rejected`);
    } catch (e) {
      rejectError = e instanceof Error ? e.message : String(e);
    } finally {
      rejectBusy = false;
    }
  }
</script>

{#if cloud.targets.length > 0}
  <section class="sec cloud" aria-label="Cloud">
    <div class="chead">
      <h3>Cloud</h3>
      <div class="tlist">
        {#each cloud.targets as t (t.name)}
          <div class="trow">
            <span class="cloud-sub">
              {targetLine(t)}
            </span>
            <div class="cloud-budget">
              <div class="lbl"><span>this month</span><span>{money(t.spent_month)} / {money(t.monthly_budget)}</span></div>
              <div class="bar" role="meter" aria-label="{t.name} this month" aria-valuemin="0"
                aria-valuemax={t.monthly_budget} aria-valuenow={t.spent_month}>
                <i style="width: {pct(t.spent_month, t.monthly_budget)}%"></i>
              </div>
            </div>
          </div>
        {/each}
      </div>
    </div>

    {#if cloud.awaiting.length > 0}
      <div class="csect" data-section="awaiting">
        <div class="stitle">Awaiting your OK <span class="count">{cloud.awaiting.length}</span></div>
        {#each cloud.awaiting as job (job.id)}
          {@const c = job.cloud!}
          {@const why = whyBack(job)}
          <div class="crow" data-job={job.id}>
            <JobChip id={job.id} tags={job.tags} />
            <div class="who">
              <button type="button" class="jname link" onclick={() => onopen(job.id)}>{job.name}</button>
              <div class="jsub">
                {#each job.tags as tag (tag)}
                  <span class="tagpill" style="background: {jobColor(job)}1f; color: {jobColor(job)}">{tag}</span>
                {/each}
                {job.submitter}
              </div>
            </div>
            <div class="specs">
              {gpuLabel(c, targetOf(job))}
              <span class="faint">{c.target}</span>
            </div>
            <div class="money">
              <div class="est">{priceLine(job)}</div>
              <div class="cap">
                {#if c.job_cap === null}{money(c.job_spent)} spent · no job cap{:else}{money(c.job_spent)} of {money(c.job_cap)} job cap spent{/if}
              </div>
              {#if why}<div class="reason">{why}</div>{/if}
            </div>
            <div class="racts">
              <button class="btn primary small" type="button" onclick={() => (approving = { id: job.id, extend: false })}>Approve…</button>
              <button class="btn quiet small" type="button" onclick={() => askReject(job)}>Reject</button>
            </div>
          </div>
        {/each}
      </div>
    {/if}

    {#if running.length > 0}
      <div class="csect" data-section="running">
        <div class="stitle">Running in the cloud <span class="count">{running.length}</span></div>
        {#each running as job (job.id)}
          {@const c = job.cloud!}
          {@const look = phaseLook(c.phase)}
          {@const flag = flaggedToExtend(job)}
          <div class="crow" class:flag data-job={job.id}>
            <JobChip id={job.id} tags={job.tags} />
            <div class="who">
              <button type="button" class="jname link" onclick={() => onopen(job.id)}>{job.name}</button>
              <div class="jsub">{job.submitter} · {c.target}</div>
              {#if flag}<span class="needsmore">needs ~{dur(c.needs_more_time!)} more</span>{/if}
            </div>
            <div class="phase-wrap">
              {#if look}
                <span class="pill {look.cls}"><span class="ic" aria-hidden="true">{look.icon}</span>{look.text}</span>
              {:else}
                <StatePill {job} />
              {/if}
            </div>
            <div class="timebar">
              {#if c.approved_seconds !== null}
                <div class="t">{dur(elapsed(job))} / {dur(c.approved_seconds)} approved</div>
                <div class="bar"><i class:over={flag} style="width: {pct(elapsed(job), c.approved_seconds)}%"></i></div>
              {:else}
                <div class="t">{dur(elapsed(job))} so far</div>
              {/if}
            </div>
            <div class="money">
              <div class="cap">{spentLine(job)}</div>
            </div>
            <div class="racts">
              {#if c.console_url}
                <a class="dash" href={c.console_url} target="_blank" rel="noopener noreferrer">Modal ↗</a>
              {/if}
              {#if flag}
                <button class="btn small" type="button" onclick={() => (approving = { id: job.id, extend: true })}>Give more time…</button>
              {/if}
            </div>
          </div>
        {/each}
      </div>
    {/if}

    {#if cloud.recent.length > 0}
      <div class="csect" data-section="recent">
        <div class="stitle">Recent results</div>
        {#each cloud.recent as job (job.id)}
          {@const c = job.cloud!}
          <div class="crow" data-job={job.id}>
            <JobChip id={job.id} tags={job.tags} />
            <div class="who">
              <button type="button" class="jname link" onclick={() => onopen(job.id)}>{job.name}</button>
              <div class="jsub">{job.submitter} · {c.target}</div>
            </div>
            <div class="phase-wrap"><StatePill {job} /></div>
            <div class="money"><div class="cap">{persistLine(c.persist, c.target)}</div></div>
          </div>
        {/each}
      </div>
    {/if}
  </section>
{/if}

{#if approving && approvingJob}
  <ApproveDialog
    job={approvingJob}
    target={targetOf(approvingJob)}
    extend={approving.extend}
    gone={approvingGone}
    onclose={() => (approving = null)}
    ondone={onnotice}
  />
{/if}

{#if rejecting}
  <Modal title="Reject #{rejecting.id}?" onclose={keepIt}>
    <p class="rq">Reject #{rejecting.id}? It won't run.</p>
    {#if rejectError}<p class="err" role="alert">{rejectError}</p>{/if}
    <div class="macts">
      <button class="btn" type="button" disabled={rejectBusy} onclick={keepIt}>Keep it</button>
      <button class="btn danger" type="button" disabled={rejectBusy} onclick={confirmReject}>Reject</button>
    </div>
  </Modal>
{/if}

<style>
  .chead { display: flex; align-items: flex-start; gap: 10px; }
  .chead h3 { margin: 0; line-height: 20px; }
  .tlist { flex: 1; display: flex; flex-direction: column; gap: 8px; min-width: 0; }
  .trow { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; flex-wrap: wrap; }
  .cloud-sub { font-weight: 700; font-size: 12.5px; color: var(--ink-2); line-height: 20px; }
  .cloud-budget { width: 260px; flex: none; }
  .cloud-budget .lbl { display: flex; justify-content: space-between; font-size: 11.5px; font-weight: 800; color: var(--ink-3); text-transform: uppercase; letter-spacing: .4px; margin-bottom: 4px; }
  .cloud-budget .bar { height: 8px; border-radius: 99px; background: #f4ecf0; overflow: hidden; }
  .cloud-budget .bar i { display: block; height: 100%; border-radius: 99px; background: linear-gradient(90deg, #ffb8cd, var(--accent)); }

  .csect { margin-top: 16px; }
  .stitle { font-size: 11.5px; font-weight: 900; color: var(--ink-3); text-transform: uppercase; letter-spacing: .6px; margin-bottom: 8px; display: flex; align-items: center; gap: 8px; }
  .stitle .count { background: var(--gray-bg); color: var(--ink-2); border-radius: 99px; padding: 1px 8px; font-size: 11px; }

  .crow { display: flex; align-items: center; gap: 14px; padding: 12px 4px; border-top: 1.5px solid #f7eff3; }
  .stitle + .crow { border-top: 0; }
  .who { min-width: 190px; flex: none; }
  .jname { font-weight: 800; text-decoration: none; }
  .jname:hover { text-decoration: underline dotted; }
  .jsub { font-size: 11.5px; color: var(--ink-2); font-weight: 700; display: flex; align-items: center; gap: 6px; margin-top: 2px; }
  .tagpill { padding: 1px 8px; border-radius: 99px; font-size: 10.5px; font-weight: 800; line-height: 1.5; }
  .specs { min-width: 200px; flex: none; font-size: 12.5px; font-weight: 700; color: var(--ink); }
  .specs .faint { display: block; font-size: 11.5px; font-weight: 700; margin-top: 1px; }
  .money { min-width: 190px; flex: 1; font-size: 12.5px; font-weight: 700; }
  .money .est { color: var(--ink); font-weight: 800; }
  .money .cap { color: var(--ink-2); margin-top: 3px; }
  .money .reason { color: var(--stop); font-weight: 700; margin-top: 3px; }
  .racts { display: flex; align-items: center; gap: 8px; flex: none; }
  .phase-wrap { min-width: 150px; flex: none; }
  .timebar { width: 160px; flex: none; }
  .timebar .t { font-size: 11.5px; font-weight: 700; color: var(--ink-2); margin-bottom: 3px; }
  .timebar .bar { height: 7px; border-radius: 99px; background: #f4ecf0; overflow: hidden; }
  .timebar .bar i { display: block; height: 100%; border-radius: 99px; background: linear-gradient(90deg, #a9d8f5, #cdbcf5); }
  .timebar .bar i.over { background: var(--stop); }
  .dash { color: var(--ink-2); font-weight: 800; font-size: 12.5px; text-decoration: none; white-space: nowrap; }
  .dash:hover { text-decoration: underline; }
  .crow.flag { background: #fff8ec; margin: 0 -8px; padding: 12px 8px; border-radius: 14px; }
  .needsmore { display: inline-flex; align-items: center; gap: 5px; background: var(--stop-bg); color: var(--stop); border-radius: 99px; padding: 2px 9px; font-size: 11px; font-weight: 800; margin-top: 4px; }

  .btn.small { padding: 5px 12px; font-size: 12.5px; }
  .btn.quiet { background: none; border-color: transparent; color: var(--ink-2); }
  .btn:disabled { opacity: .6; cursor: default; }

  .rq { font-weight: 700; margin: 0 0 4px; }
  .err { color: var(--fail); font-weight: 800; font-size: 12.5px; margin: 8px 0 0; }
  .macts { display: flex; gap: 8px; margin-top: 14px; }
  .macts .btn { flex: 1; }

  @media (max-width: 760px) {
    .crow { flex-wrap: wrap; gap: 8px 12px; }
    .who, .specs, .money, .phase-wrap { min-width: 0; }
    .cloud-budget { width: 100%; }
  }
</style>
