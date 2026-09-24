<script lang="ts">
  import { fmtGib, gib, dur, hm, metric, reasonLabel } from "../lib/format";
  import { jobColor } from "../lib/colors";
  import { mascot } from "../lib/mascot.svelte";
  import { EMPTY_FILTER, effectiveSort, isActive, type Filter, type SortKey, type StateFilter } from "../lib/jobfilter";
  import FilterBar from "./FilterBar.svelte";
  import JobChip from "./JobChip.svelte";
  import JobSprite from "./JobSprite.svelte";
  import Sparkline from "./Sparkline.svelte";
  import StatePill from "./StatePill.svelte";
  import TagSummary from "./TagSummary.svelte";
  import type { JobView, SparkMap } from "../lib/types";

  const ZERO_COUNTS: Record<StateFilter, number> = { running: 0, awaiting: 0, queued: 0, completed: 0, failed: 0, cancelled: 0 };

  interface Props {
    jobs: JobView[];
    pool: number;
    now: number;
    selected: number | null;
    onopen: (id: number) => void;
    filter?: Filter;
    total?: number;
    counts?: Record<StateFilter, number>;
    known?: { tags: string[]; by: string[] };
    // The full (unfiltered-by-search/state/other-tags) job list `jobs` was drawn from, for the tag
    // summary strip: it covers every job with the active tag regardless of what else is filtered,
    // not just the rows currently on screen. Defaults to `jobs` so callers/tests that don't pass
    // it (and aren't exercising more than one filter at once) keep working.
    sourceJobs?: JobView[];
    // Row sparklines by job id, fetched on their own slow schedule (see lib/sparks.svelte.ts).
    // Jobs that never reported a numeric metric are simply absent.
    sparks?: SparkMap;
    onfilter?: (f: Filter, replace?: boolean) => void;
  }
  let {
    jobs, now, selected, onopen,
    filter = EMPTY_FILTER, total = jobs.length, counts = ZERO_COUNTS, known = { tags: [], by: [] },
    sourceJobs = jobs, sparks = {}, onfilter = () => {},
  }: Props = $props();

  const active = $derived(isActive(filter));

  // Jobs carrying the single active tag, out of the *full* source list — not `jobs` (the rows
  // left after search/state/other filters are also applied). The tag summary and its bulk actions
  // always cover every job with that tag, regardless of what else is currently filtering the table.
  const tagJobs = $derived(
    filter.tags.length === 1 ? sourceJobs.filter((j) => j.tags.includes(filter.tags[0])) : [],
  );

  // New key → desc for these (the "biggest/most recent first" reading); asc for the rest.
  const DESC_DEFAULT = new Set<SortKey>(["id", "bid", "memory", "time", "submitted", "ended"]);
  interface SortOption { key: SortKey; desc: boolean; label: string }
  // Both directions for every key, so the phone select (which replaces the hidden column headers)
  // can pick a direction too, not just a key. Encoded as "<key>:<a|d>" for the <option> value.
  const SORT_OPTIONS: SortOption[] = [
    { key: "id", desc: true, label: "newest first" },
    { key: "id", desc: false, label: "oldest first" },
    { key: "state", desc: false, label: "state" },
    { key: "state", desc: true, label: "state, reversed" },
    { key: "bid", desc: true, label: "bid high → low" },
    { key: "bid", desc: false, label: "bid low → high" },
    { key: "memory", desc: true, label: "memory high → low" },
    { key: "memory", desc: false, label: "memory low → high" },
    { key: "time", desc: true, label: "time high → low" },
    { key: "time", desc: false, label: "time low → high" },
    { key: "by", desc: false, label: "submitter A → Z" },
    { key: "by", desc: true, label: "submitter Z → A" },
  ];
  function optValue(o: { key: SortKey; desc: boolean }): string {
    return `${o.key}:${o.desc ? "d" : "a"}`;
  }

  function sortFor(key: SortKey): { key: SortKey; desc: boolean } {
    // Compare against the *actual* (nullable) filter.sort, not effectiveSort's fallback default —
    // effectiveSort defaults to {key:"id",desc:true} even when nothing has been clicked yet, so
    // comparing against it would treat a first click on the id/job header as "same key, flip" and
    // sort ascending instead of the intended "new key → desc".
    const cur = filter.sort;
    if (cur !== null && cur.key === key) return { key, desc: !cur.desc };
    return { key, desc: DESC_DEFAULT.has(key) };
  }
  function onSort(key: SortKey): void {
    onfilter({ ...filter, sort: sortFor(key) }, false);
  }
  function isSorted(key: SortKey): boolean {
    return active && effectiveSort(filter).key === key;
  }
  function arrow(): string {
    return effectiveSort(filter).desc ? "▼" : "▲";
  }
  function onSortSelect(e: Event): void {
    const value = (e.target as HTMLSelectElement).value;
    if (!value) return;
    const [key, dir] = value.split(":") as [SortKey, "a" | "d"];
    onfilter({ ...filter, sort: { key, desc: dir === "d" } }, false);
  }
  // Compares against the *actual* (nullable) `filter.sort`, not `effectiveSort`'s fallback — the
  // fallback defaults to id/desc even when nothing has been chosen yet, which would otherwise
  // make the phone select show "newest first" as already picked and swallow the first change
  // event when the user picks it themselves.
  const sortSelectValue = $derived(filter.sort ? optValue(filter.sort) : "");

  function addTag(tag: string): void {
    if (filter.tags.includes(tag)) return;
    onfilter({ ...filter, tags: [...filter.tags, tag] }, false);
  }
  function addBy(by: string): void {
    if (filter.by.includes(by)) return;
    onfilter({ ...filter, by: [...filter.by, by] }, false);
  }
  function onTagClick(e: MouseEvent, tag: string): void {
    e.stopPropagation();
    addTag(tag);
  }
  function onByClick(e: MouseEvent, by: string): void {
    e.stopPropagation();
    addBy(by);
  }

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
    // Cloud jobs waiting on a person to approve their cost (State.AWAITING), grouped above
    // Queued: a fresh submission and a paused re-approval are both something to act on now,
    // where a queued job is just waiting its turn.
    const awaiting = jobs.filter((j) => j.state === "awaiting").sort(queuedCompare);
    const queued = jobs.filter((j) => j.state === "queued").sort(queuedCompare);
    const finished = jobs.filter((j) => FINISHED_STATES.has(j.state)).sort((a, b) => (b.end_time ?? 0) - (a.end_time ?? 0));
    const result: Group[] = [];
    if (running.length > 0) result.push({ label: "Running", jobs: running });
    if (awaiting.length > 0) result.push({ label: "Awaiting approval", jobs: awaiting });
    if (queued.length > 0) result.push({ label: "Queued", jobs: queued });
    if (finished.length > 0) result.push({ label: "Recently finished", jobs: finished });
    return result;
  });

  const runningCount = $derived(jobs.filter((j) => j.state === "running").length);
  const queuedCount = $derived(jobs.filter((j) => j.state === "queued").length);
  const awaitingCount = $derived(jobs.filter((j) => j.state === "awaiting").length);

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
    const total = expected(job);
    if (total <= 0) return 0;
    return Math.min(100, (job.run_time / total) * 100);
  }

  /** Expected total run time: the approved cloud window for a cloud job (so its bar and "of ~"
   * reading track what a person actually signed off on, not an estimate), else projected from
   * progress reports when the job sends them. */
  function expected(job: JobView): number {
    if (job.cloud !== null && job.cloud.approved_seconds !== null) return job.cloud.approved_seconds;
    return job.eta_source === "progress" ? job.expected_runtime : job.est_runtime;
  }

  function expectedTitle(job: JobView): string | undefined {
    return job.eta_source === "progress" ? `from progress reports (estimated ${dur(job.est_runtime)})` : undefined;
  }

  function queuedStart(job: JobView): string {
    return job.projected.length > 0 ? `starts ~${hm(job.projected[0][0])}` : "waiting";
  }

  function queuedSub(job: JobView): string {
    return `est. ${dur(job.est_runtime)}${job.run_time > 0 ? ` · ran ${dur(job.run_time)}` : ""}`;
  }

  /** The one metric pasar picked for this job's row, or null when it never reported numbers. */
  function spark(job: JobView) {
    return sparks[job.id] ?? null;
  }

  function cardMeta(job: JobView): string {
    if (isLive(job)) {
      const mem = job.cloud !== null ? `${job.cloud.gpu} · ${job.cloud.target}` : `${gib(job.usage ?? 0)}/${gib(job.limit)} GiB`;
      return `${dur(job.run_time)} of ~${dur(expected(job))} · ${mem}`;
    }
    if (job.state === "queued" || job.state === "awaiting") {
      return `${queuedStart(job)} · ${job.cloud !== null ? `${job.cloud.gpu} · ${job.cloud.target}` : job.mode === "whole" ? "whole GPU" : fmtGib(job.limit)}`;
    }
    return `ran ${dur(job.run_time)} · ended ${hm(job.end_time ?? now)}`;
  }

  function open(id: number): void {
    onopen(id);
  }
  function onActivate(e: KeyboardEvent, id: number): void {
    if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
      e.preventDefault(); // stop Space from scrolling the page
      open(id);
    }
  }
</script>

{#snippet cloudBadge(job: JobView)}
  {#if job.cloud !== null}
    <span class="cloudbadge" role="img" aria-label="runs in the cloud on {job.cloud.target}" title="runs in the cloud · {job.cloud.target}">
      <svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true" focusable="false">
        <path d="M7 18a4 4 0 0 1-.6-7.96 5 5 0 0 1 9.44-2A4.5 4.5 0 0 1 17.5 18H7Z" fill="currentColor" />
      </svg>
    </span>
  {/if}
{/snippet}

{#snippet row(job: JobView)}
  <tr class="row" class:sel={selected === job.id} tabindex="0" onclick={() => open(job.id)} onkeydown={(e) => onActivate(e, job.id)}>
    <td>
      <div class="jid">
        <JobSprite {job} />
        <JobChip id={job.id} tags={job.tags} />
        <div>
          <div class="jname">{job.name}</div>
          {#if job.tags.length > 0}
            <div class="jtags">
              {#each job.tags as tag (tag)}
                <button type="button" class="tagpill" style="background: {jobColor(job)}1f; color: {jobColor(job)}" onclick={(e) => onTagClick(e, tag)}>{tag}</button>
              {/each}
            </div>
          {/if}
          <div class="jsub" class:fail={jobSub(job).failed}>{jobSub(job).text}</div>
        </div>
      </div>
    </td>
    <td><span class="statecell"><StatePill {job} />{@render cloudBadge(job)}</span></td>
    <td><span class="bid" class:hi={job.bid > 1000}>★ {job.bid}</span></td>
    <td>
      {#if job.cloud !== null}
        <div class="small">{job.cloud.gpu} · {job.cloud.target}</div>
      {:else if job.mode === "whole"}
        <div class="small">whole GPU</div>
        <div class="small faint">{fmtGib(job.limit)}</div>
      {:else if isLive(job)}
        <div class="meter">
          <div class="small">{gib(job.usage ?? 0)} / {gib(job.limit)} GiB</div>
          <div class="bar">
            <i class:over={job.over_limit} style="width: {memPct(job)}%; {job.over_limit ? '' : `background: ${jobColor(job)}`}"></i>
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
          <div class="small">{dur(job.run_time)} <span class="faint" title={expectedTitle(job)}>/ ~{dur(expected(job))}</span></div>
          <div class="bar"><i style="width: {timePct(job)}%; background: linear-gradient(90deg, #a9d8f5, #cdbcf5)"></i></div>
        </div>
      {:else if job.state === "queued" || job.state === "awaiting"}
        <div class="small">{queuedStart(job)}</div>
        <div class="small faint">{queuedSub(job)}</div>
      {:else}
        <div class="small">ended {hm(job.end_time ?? now)}</div>
        <div class="small faint">ran {dur(job.run_time)}</div>
      {/if}
    </td>
    <td class="metric">
      {#if spark(job)}
        <div class="mhead"><span class="mkey">{spark(job)!.key}</span> <b>{metric(spark(job)!.latest)}</b></div>
        <Sparkline points={spark(job)!.points} color={jobColor(job)} height={22} format={metric} />
      {/if}
    </td>
    <td class="small dim">
      {#if job.submitter}
        <button type="button" class="link" onclick={(e) => onByClick(e, job.submitter)}>{job.submitter}</button>
      {:else}
        –
      {/if}
    </td>
  </tr>
{/snippet}

{#snippet card(job: JobView)}
  <div class="card" role="button" tabindex="0" class:sel={selected === job.id} onclick={() => open(job.id)} onkeydown={(e) => onActivate(e, job.id)}>
    <div class="top">
      <JobSprite {job} />
      <JobChip id={job.id} tags={job.tags} />
      <!-- Name, bid, tags, state and metric all live in this one column so they line up under
           the leading icons no matter how many of them render (chip alone, or chip + sprite),
           rather than the fixed `margin-left` a previous, icon-count-agnostic layout used. -->
      <div class="card-body">
        <div class="card-head">
          <b>{job.name}</b>
          <span class="spacer"></span>
          <span class="bid" class:hi={job.bid > 1000}>★ {job.bid}</span>
        </div>
        {#if job.tags.length > 0}
          <div class="jtags card-tags">
            {#each job.tags as tag (tag)}
              <button type="button" class="tagpill" style="background: {jobColor(job)}1f; color: {jobColor(job)}" onclick={(e) => onTagClick(e, tag)}>{tag}</button>
            {/each}
          </div>
        {/if}
        <div class="meta">
          <StatePill {job} />{@render cloudBadge(job)}
          {#if job.state === "failed"}
            <span class="fail">{jobSub(job).text}</span> · {hm(job.end_time ?? now)}
          {:else}
            {cardMeta(job)}
          {/if}
        </div>
        {#if isLive(job)}
          <div class="bar"><i style="width: {timePct(job)}%; background: linear-gradient(90deg, #a9d8f5, #cdbcf5)"></i></div>
        {/if}
        {#if spark(job)}
          <div class="cardmetric">
            <div class="mhead"><span class="mkey">{spark(job)!.key}</span> <b>{metric(spark(job)!.latest)}</b></div>
            <Sparkline points={spark(job)!.points} color={jobColor(job)} height={26} format={metric} />
          </div>
        {/if}
      </div>
    </div>
  </div>
{/snippet}

<div class="sec">
  <h3>
    Jobs
    {#if active}
      <span class="dim">{jobs.length} match · of {total}</span>
    {:else}
      <span class="dim">{runningCount} running · {queuedCount} queued{awaitingCount > 0 ? ` · ${awaitingCount} awaiting` : ""}</span>
    {/if}
    <span class="spacer"></span>
    <select class="sortsel" aria-label="sort" value={sortSelectValue} onchange={onSortSelect}>
      <option value="" disabled>sort…</option>
      {#each SORT_OPTIONS as opt (optValue(opt))}<option value={optValue(opt)}>{opt.label}</option>{/each}
    </select>
  </h3>
  <FilterBar {filter} {counts} {known} onchange={onfilter} />
  {#if filter.tags.length === 1}
    {#key filter.tags[0]}
      <TagSummary tag={filter.tags[0]} jobs={tagJobs} />
    {/key}
  {/if}
  {#if jobs.length === 0}
    {#if active}
      <div class="empty">
        <p>No jobs match.</p>
        <button class="btn" type="button" onclick={() => onfilter(EMPTY_FILTER, false)}>clear</button>
      </div>
    {:else}
      <div class="empty">
        <img src={mascot.pick("idle")} alt="" width="72" height="72" />
        <p>No jobs yet. Try  pasar submit --time 10m -- python train.py</p>
      </div>
    {/if}
  {:else}
    <table>
      <thead>
        <tr>
          <th class:sorted={isSorted("id")}><button type="button" class="hcell" onclick={() => onSort("id")}>job{#if isSorted("id")}<span class="arr">{arrow()}</span>{/if}</button></th>
          <th class:sorted={isSorted("state")}><button type="button" class="hcell" onclick={() => onSort("state")}>state{#if isSorted("state")}<span class="arr">{arrow()}</span>{/if}</button></th>
          <th class:sorted={isSorted("bid")}><button type="button" class="hcell" onclick={() => onSort("bid")}>bid{#if isSorted("bid")}<span class="arr">{arrow()}</span>{/if}</button></th>
          <th class:sorted={isSorted("memory")}><button type="button" class="hcell" onclick={() => onSort("memory")}>memory{#if isSorted("memory")}<span class="arr">{arrow()}</span>{/if}</button></th>
          <th class:sorted={isSorted("time")}><button type="button" class="hcell" onclick={() => onSort("time")}>time{#if isSorted("time")}<span class="arr">{arrow()}</span>{/if}</button></th>
          <th>metric</th>
          <th class:sorted={isSorted("by")}><button type="button" class="hcell" onclick={() => onSort("by")}>by{#if isSorted("by")}<span class="arr">{arrow()}</span>{/if}</button></th>
        </tr>
      </thead>
      <tbody>
        {#if active}
          {#each jobs as job (job.id)}
            {@render row(job)}
          {/each}
        {:else}
          {#each groups as group (group.label)}
            <tr class="group"><td colspan="7">{group.label}</td></tr>
            {#each group.jobs as job (job.id)}
              {@render row(job)}
            {/each}
          {/each}
        {/if}
      </tbody>
    </table>
    <div class="cards">
      {#if active}
        {#each jobs as job (job.id)}
          {@render card(job)}
        {/each}
      {:else}
        {#each groups as group (group.label)}
          <div class="grp">{group.label}</div>
          {#each group.jobs as job (job.id)}
            {@render card(job)}
          {/each}
        {/each}
      {/if}
    </div>
  {/if}
</div>

<style>
  table { width: 100%; border-collapse: separate; border-spacing: 0; }
  th { text-align: left; font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--ink-3); font-weight: 800; padding: 6px 10px; }
  th.sorted { color: var(--accent-ink); }
  .hcell { background: none; border: 0; padding: 0; margin: 0; font: inherit; text-transform: inherit; letter-spacing: inherit; color: inherit; cursor: pointer; }
  .arr { font-size: 10px; margin-left: 3px; }
  td { padding: 10px; border-top: 1.5px solid #f7eff3; vertical-align: middle; }
  tr.row { cursor: pointer; }
  tr.row:hover td { background: #fff6f9; }
  tr.row.sel td { background: #fdf0f5; }
  tr.group td { border: 0; padding: 12px 10px 4px; font-size: 11.5px; font-weight: 900; color: var(--ink-3); text-transform: uppercase; letter-spacing: 0.6px; cursor: default; }
  .statecell { display: inline-flex; align-items: center; gap: 5px; }
  .cloudbadge { display: inline-flex; color: var(--cloud-ic); }
  .jid { display: flex; align-items: center; gap: 10px; }
  .jname { font-weight: 800; }
  .jsub { font-size: 12px; color: var(--ink-2); font-weight: 600; max-width: 320px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .jsub.fail { color: var(--fail); }
  .jtags { display: flex; flex-wrap: wrap; gap: 4px; margin: 2px 0; }
  .tagpill { padding: 1px 8px; border-radius: 99px; font-size: 10.5px; font-weight: 800; line-height: 1.5; border: 0; cursor: pointer; text-decoration: underline dotted; }
  .card-tags { margin: 6px 0 0; }

  .bar i.over { background: var(--stop); }

  td.metric { min-width: 132px; }
  .mhead { font-size: 11.5px; font-weight: 700; color: var(--ink-2); display: flex; align-items: baseline; gap: 5px; }
  .mkey { text-transform: lowercase; color: var(--ink-3); max-width: 84px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .mhead b { color: var(--ink); font-variant-numeric: tabular-nums; }

  .empty { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; padding: 24px 0; color: var(--ink-2); font-weight: 700; text-align: center; }
  .empty img { width: 72px; height: 72px; }
  .empty p { margin: 0; }

  .cards { display: none; }
  .sortsel { display: none; border: 1.5px solid var(--line-2); border-radius: 99px; padding: 5px 12px; font-size: 12px; font-weight: 800; background: var(--card); color: var(--ink); }

  @media (max-width: 760px) {
    table { display: none; }
    .cards { display: block; }
    .card { background: var(--card); border: 1.5px solid var(--line); border-radius: 20px; padding: 12px 13px; margin-top: 8px; cursor: pointer; }
    .card.sel { background: #fdf0f5; }
    .card .top { display: flex; align-items: flex-start; gap: 9px; }
    .card .card-body { flex: 1; min-width: 0; }
    .card .card-head { display: flex; align-items: center; gap: 9px; }
    .card .meta { font-size: 12.5px; color: var(--ink-2); font-weight: 700; margin: 6px 0 0; }
    .card .meta .fail { color: var(--fail); }
    .card .bar { height: 7px; border-radius: 99px; background: #f4ecf0; overflow: hidden; margin: 8px 0 0; }
    .card .bar i { display: block; height: 100%; border-radius: 99px; }
    .card .cardmetric { margin: 8px 0 0; }
    .cards .grp { font-size: 11.5px; font-weight: 900; color: var(--ink-3); text-transform: uppercase; letter-spacing: 0.6px; margin: 14px 2px 0; }
    .sortsel { display: inline-block !important; }
  }
</style>
