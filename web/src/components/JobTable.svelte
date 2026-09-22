<script lang="ts">
  import { fmtGib, gib, dur, hm, reasonLabel } from "../lib/format";
  import { jobColor } from "../lib/colors";
  import { mascot } from "../lib/mascot.svelte";
  import JobChip from "./JobChip.svelte";
  import StatePill from "./StatePill.svelte";
  import type { JobView } from "../lib/types";

  interface Props {
    jobs: JobView[];
    pool: number;
    now: number;
    selected: number | null;
    onopen: (id: number) => void;
  }
  let { jobs, now, selected, onopen }: Props = $props();

  const FINISHED_STATES = new Set<JobView["state"]>(["completed", "failed", "cancelled"]);

  // A "stopping" job is still actively running (it's just been asked to stop), so it shares the
  // Running group and the running-style memory/time cells with a plain "running" job.
  function isLive(job: JobView): boolean {
    return job.state === "running" || job.state === "stopping";
  }

  function queuedCompare(a: JobView, b: JobView): number {
    const aProj = a.projected.length > 0;
    const bProj = b.projected.length > 0;
    if (aProj && bProj) return a.projected[0][0] - b.projected[0][0];
    if (aProj !== bProj) return aProj ? -1 : 1;
    if (a.bid !== b.bid) return b.bid - a.bid;
    return a.queue_time - b.queue_time;
  }

  interface Group {
    label: string;
    jobs: JobView[];
  }

  const groups = $derived.by((): Group[] => {
    const running = jobs.filter(isLive).sort((a, b) => (a.start_time ?? 0) - (b.start_time ?? 0));
    const queued = jobs.filter((j) => j.state === "queued").sort(queuedCompare);
    const finished = jobs.filter((j) => FINISHED_STATES.has(j.state)).sort((a, b) => (b.end_time ?? 0) - (a.end_time ?? 0));
    const result: Group[] = [];
    if (running.length > 0) result.push({ label: "Running", jobs: running });
    if (queued.length > 0) result.push({ label: "Queued", jobs: queued });
    if (finished.length > 0) result.push({ label: "Recently finished", jobs: finished });
    return result;
  });

  const runningCount = $derived(jobs.filter((j) => j.state === "running").length);
  const queuedCount = $derived(jobs.filter((j) => j.state === "queued").length);

  function jobSub(job: JobView): { text: string; failed: boolean } {
    if (job.state === "failed") {
      const label = reasonLabel(job.reason);
      return { text: job.summary ? `${label}: ${job.summary}` : label, failed: true };
    }
    return { text: job.note, failed: false };
  }

  function memPct(job: JobView): number {
    if (job.limit <= 0) return 0;
    return Math.min(100, ((job.usage ?? 0) / job.limit) * 100);
  }

  function timePct(job: JobView): number {
    if (job.est_runtime <= 0) return 0;
    return Math.min(100, (job.run_time / job.est_runtime) * 100);
  }

  function queuedStart(job: JobView): string {
    return job.projected.length > 0 ? `starts ~${hm(job.projected[0][0])}` : "waiting";
  }

  function queuedSub(job: JobView): string {
    return `est. ${dur(job.est_runtime)}${job.run_time > 0 ? ` · ran ${dur(job.run_time)}` : ""}`;
  }

  function lost(job: JobView): { text: string; faint: boolean } {
    const total = job.lost.preemption + job.lost.failure;
    const text = (total > 0 ? dur(total) : "–") + (job.lost.known ? "" : "?");
    return { text, faint: total === 0 };
  }

  function cardMeta(job: JobView): string {
    if (isLive(job)) return `${dur(job.run_time)} of ~${dur(job.est_runtime)} · ${gib(job.usage ?? 0)}/${gib(job.limit)} GiB`;
    if (job.state === "queued") return `${queuedStart(job)} · ${job.mode === "whole" ? "whole GPU" : fmtGib(job.limit)}`;
    return `ran ${dur(job.run_time)} · ended ${hm(job.end_time ?? now)}`;
  }

  function open(id: number): void {
    onopen(id);
  }
  function onActivate(e: KeyboardEvent, id: number): void {
    if (e.key === "Enter") {
      e.preventDefault();
      open(id);
    }
  }
</script>

<div class="sec">
  <h3>Jobs <span class="dim">{runningCount} running · {queuedCount} queued</span></h3>
  {#if jobs.length === 0}
    <div class="empty">
      <img src={mascot.pick("idle")} alt="" width="72" height="72" />
      <p>No jobs yet. Try  pasar submit --time 10m -- python train.py</p>
    </div>
  {:else}
    <table>
      <thead>
        <tr><th>job</th><th>state</th><th>bid</th><th>memory</th><th>time</th><th>lost</th><th>by</th></tr>
      </thead>
      <tbody>
        {#each groups as group (group.label)}
          <tr class="group"><td colspan="7">{group.label}</td></tr>
          {#each group.jobs as job (job.id)}
            <tr class="row" class:sel={selected === job.id} tabindex="0" onclick={() => open(job.id)} onkeydown={(e) => onActivate(e, job.id)}>
              <td>
                <div class="jid">
                  <JobChip id={job.id} />
                  <div>
                    <div class="jname">{job.name}</div>
                    <div class="jsub" class:fail={jobSub(job).failed}>{jobSub(job).text}</div>
                  </div>
                </div>
              </td>
              <td><StatePill {job} /></td>
              <td><span class="bid" class:hi={job.bid > 1000}>★ {job.bid}</span></td>
              <td>
                {#if job.mode === "whole"}
                  <div class="small">whole GPU</div>
                  <div class="small faint">{fmtGib(job.limit)}</div>
                {:else if isLive(job)}
                  <div class="meter">
                    <div class="small">{gib(job.usage ?? 0)} / {gib(job.limit)} GiB</div>
                    <div class="bar">
                      <i class:over={job.over_limit} style="width: {memPct(job)}%; {job.over_limit ? '' : `background: ${jobColor(job.id)}`}"></i>
                    </div>
                  </div>
                {:else}
                  <div class="small">{fmtGib(job.limit)}</div>
                  <div class="small faint">{job.peak > 0 ? `peak ${fmtGib(job.peak)}` : "requested"}</div>
                {/if}
              </td>
              <td>
                {#if isLive(job)}
                  <div class="meter">
                    <div class="small">{dur(job.run_time)} <span class="faint">/ ~{dur(job.est_runtime)}</span></div>
                    <div class="bar"><i style="width: {timePct(job)}%; background: linear-gradient(90deg, #a9d8f5, #cdbcf5)"></i></div>
                  </div>
                {:else if job.state === "queued"}
                  <div class="small">{queuedStart(job)}</div>
                  <div class="small faint">{queuedSub(job)}</div>
                {:else}
                  <div class="small">ended {hm(job.end_time ?? now)}</div>
                  <div class="small faint">ran {dur(job.run_time)}</div>
                {/if}
              </td>
              <td class="small" class:faint={lost(job).faint}>{lost(job).text}</td>
              <td class="small dim">{job.submitter || "–"}</td>
            </tr>
          {/each}
        {/each}
      </tbody>
    </table>
    <div class="cards">
      {#each groups as group (group.label)}
        <div class="grp">{group.label}</div>
        {#each group.jobs as job (job.id)}
          <div class="card" role="button" tabindex="0" class:sel={selected === job.id} onclick={() => open(job.id)} onkeydown={(e) => onActivate(e, job.id)}>
            <div class="top">
              <JobChip id={job.id} />
              <b>{job.name}</b>
              <span class="spacer"></span>
              <span class="bid" class:hi={job.bid > 1000}>★ {job.bid}</span>
            </div>
            <div class="meta">
              <StatePill {job} />
              {#if job.state === "failed"}
                <span class="fail">{jobSub(job).text}</span> · {hm(job.end_time ?? now)}
              {:else}
                {cardMeta(job)}
              {/if}
            </div>
            {#if isLive(job)}
              <div class="bar"><i style="width: {timePct(job)}%; background: linear-gradient(90deg, #a9d8f5, #cdbcf5)"></i></div>
            {/if}
          </div>
        {/each}
      {/each}
    </div>
  {/if}
</div>

<style>
  table { width: 100%; border-collapse: separate; border-spacing: 0; }
  th { text-align: left; font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--ink-3); font-weight: 800; padding: 6px 10px; }
  td { padding: 10px; border-top: 1.5px solid #f7eff3; vertical-align: middle; }
  tr.row { cursor: pointer; }
  tr.row:hover td { background: #fff6f9; }
  tr.row.sel td { background: #fdf0f5; }
  tr.group td { border: 0; padding: 12px 10px 4px; font-size: 11.5px; font-weight: 900; color: var(--ink-3); text-transform: uppercase; letter-spacing: 0.6px; cursor: default; }
  .jid { display: flex; align-items: center; gap: 10px; }
  .jname { font-weight: 800; }
  .jsub { font-size: 12px; color: var(--ink-2); font-weight: 600; max-width: 320px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .jsub.fail { color: var(--fail); }

  .bar i.over { background: var(--stop); }

  .empty { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; padding: 24px 0; color: var(--ink-2); font-weight: 700; text-align: center; }
  .empty img { width: 72px; height: 72px; }
  .empty p { margin: 0; }

  .cards { display: none; }

  @media (max-width: 760px) {
    table { display: none; }
    .cards { display: block; }
    .card { background: var(--card); border: 1.5px solid var(--line); border-radius: 20px; padding: 12px 13px; margin-top: 8px; cursor: pointer; }
    .card.sel { background: #fdf0f5; }
    .card .top { display: flex; align-items: center; gap: 9px; }
    .card .meta { font-size: 12.5px; color: var(--ink-2); font-weight: 700; margin: 6px 0 0 43px; }
    .card .meta .fail { color: var(--fail); }
    .card .bar { height: 7px; border-radius: 99px; background: #f4ecf0; overflow: hidden; margin: 8px 0 0 43px; }
    .card .bar i { display: block; height: 100%; border-radius: 99px; }
    .cards .grp { font-size: 11.5px; font-weight: 900; color: var(--ink-3); text-transform: uppercase; letter-spacing: 0.6px; margin: 14px 2px 0; }
  }
</style>
