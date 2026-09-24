<script lang="ts">
  import { getJob, cancelJob, setBid, restartJob, getEvents, getGpu, getMetrics, getUsage, ApiError } from "../lib/api";
  import { mascot } from "../lib/mascot.svelte";
  import { dur, fmtGib, gib, hm, metric } from "../lib/format";
  import { jobColor, JOB_COLORS } from "../lib/colors";
  import { resultSize } from "../lib/cloud";
  import { eventRows } from "../lib/eventlog";
  import { progressSeries } from "../lib/series";
  import JobChip from "./JobChip.svelte";
  import StatePill from "./StatePill.svelte";
  import Facts from "./Facts.svelte";
  import ReasonBox from "./ReasonBox.svelte";
  import AttemptsBar from "./AttemptsBar.svelte";
  import BidDialog from "./BidDialog.svelte";
  import LogView from "./LogView.svelte";
  import LineChart from "./LineChart.svelte";
  import EventList from "./EventList.svelte";
  import type { GpuSeries, JobDetail, JobEvent, JobView, MetricSummary, Series } from "../lib/types";

  interface Props {
    id: number;
    live: JobView | null;
    now: number;
    grafanaUrl: string | null;
    // Other jobs from the live snapshot, used only to note which other running jobs share the
    // GPU whose power/temperature charts this panel shows. Optional so existing callers/tests
    // that don't have the full job list keep working — the note is simply omitted.
    jobs?: JobView[];
    // Lets the tags list act as filter shortcuts. Optional so existing callers/tests that don't
    // care about filtering keep working — the tags then render as plain text, as before.
    onfilter?: (tag: string) => void;
    onclose: () => void;
    onrestartwith: (job: JobDetail) => void;
  }
  let { id, live, now, grafanaUrl, jobs = [], onfilter, onclose, onrestartwith }: Props = $props();

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
  let loadError = $state<string | null>(null);
  let headingEl = $state<HTMLElement | null>(null);
  let drawerEl = $state<HTMLElement | null>(null);
  let activeTab = $state<TabKey>("overview");
  let showBid = $state(false);
  let busy = $state<"cancel" | "restart" | null>(null);
  let actionError = $state<string | null>(null);

  const jobView = $derived.by((): JobView | null => {
    if (live) return live;
    if (detail) return { ...detail, attempts: detail.attempts.length };
    return null;
  });

  // Primitives pulled out of `jobView` so effects that key off them don't re-run on every
  // snapshot tick — `jobView` (and `live`) get a new object identity each tick even when nothing
  // relevant changed, but a $derived primitive only propagates when its own value changes.
  const curState = $derived(jobView?.state ?? null);
  const curStartTime = $derived(jobView?.start_time ?? null);
  const curCheckpointTs = $derived(jobView?.last_checkpoint?.ts ?? null);
  const curProgressTs = $derived(jobView?.progress?.ts ?? null);
  const isLiveState = $derived(curState === "running" || curState === "stopping");
  const isFinishedState = $derived(curState !== null && FINISHED.has(curState));

  let usage = $state<Series>([]);
  let gpu = $state<GpuSeries>({ power_w: [], temp_c: [], util_pct: [] });
  let events = $state<JobEvent[]>([]);
  let metricsSummary = $state<MetricSummary[]>([]);

  function keyFor(forId: number): string {
    return `${forId}:${live?.state ?? ""}:${live?.attempts ?? ""}`;
  }

  // Bumped on every load() call so a response that's no longer for the latest request (e.g. a
  // slow fetch for a job we've since navigated away from) is ignored instead of overwriting
  // the current job's detail.
  let requestId = 0;
  // Tracks the id/state/attempts key of the last *successful* (or 404'd) load, so a snapshot
  // tick that didn't change anything meaningful skips refetching — but a transient error leaves
  // this stale, so the next tick retries.
  let committedKey = "";
  let lastId: number | null = null;

  async function load(forId: number, key: string): Promise<void> {
    const myRequest = ++requestId;
    try {
      const d = await getJob(forId);
      if (myRequest !== requestId) return;
      detail = d;
      notFound = false;
      loadError = null;
      committedKey = key;
    } catch (e) {
      if (myRequest !== requestId) return;
      if (e instanceof ApiError && e.status === 404) {
        notFound = true;
        detail = null;
        loadError = null;
        committedKey = key;
      } else {
        // Leave committedKey stale (don't commit it) so the next snapshot tick retries.
        loadError = e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e);
      }
    }
  }

  // Reload attempts whenever the job first mounts or its state/attempt count changes, without
  // refetching on every snapshot tick (e.g. just a usage number changing) — unless the last
  // load failed with something other than a 404, in which case committedKey stays stale and
  // this retries once per tick until it succeeds.
  $effect(() => {
    void now; // read so this effect (and therefore a retry check) reruns once per snapshot tick
    if (id !== lastId) {
      lastId = id;
      detail = null;
      notFound = false;
      loadError = null;
      committedKey = "";
      showBid = false;
      busy = null;
      actionError = null;
      usage = [];
      gpu = { power_w: [], temp_c: [], util_pct: [] };
      events = [];
      metricsSummary = [];
      committedEventsKey = "";
    }
    const key = keyFor(id);
    if (key === committedKey) return;
    void load(id, key);
  });

  $effect(() => {
    headingEl?.focus();
  });

  // Memory usage + GPU power/temperature, polled every 5s while the job is live. Torn down (and
  // the interval cleared) as soon as the job stops being live, or the panel moves to another job.
  // `now` ticks on (almost) every snapshot; track it in a plain variable rather than reading it
  // directly inside the polling effect below, so a fresh `now` doesn't tear down and restart the
  // 5s interval on every tick.
  let latestNow = 0;
  $effect(() => {
    latestNow = now;
  });

  // A preempted/failed-and-retried job goes back to "queued" before its next attempt starts.
  // Without this, the metrics tab would keep showing the previous attempt's memory usage chart
  // (it isn't gated on isLiveState the way the GPU charts are) until fresh data arrives.
  $effect(() => {
    if (curState === "queued") usage = [];
  });

  $effect(() => {
    const forId = id;
    if (!isLiveState) return;
    const startTime = curStartTime;

    let cancelled = false;
    async function tick(): Promise<void> {
      const elapsedMin = startTime !== null ? Math.ceil((latestNow - startTime) / 60) : 5;
      const minutes = Math.min(240, Math.max(5, elapsedMin));
      const [u, g] = await Promise.allSettled([getUsage(forId), getGpu(minutes)]);
      if (cancelled || forId !== id) return;
      if (u.status === "fulfilled") usage = u.value;
      if (g.status === "fulfilled") gpu = g.value;
    }
    void tick();
    const timer = setInterval(() => void tick(), 5000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  });

  // Per-attempt power/temperature/energy summary for a finished job, fetched once, plus the
  // memory usage history (shown as a chart if it exists).
  $effect(() => {
    const forId = id;
    if (!isFinishedState) return;
    let cancelled = false;
    (async () => {
      try {
        const [m, u] = await Promise.all([getMetrics(forId), getUsage(forId)]);
        if (cancelled || forId !== id) return;
        metricsSummary = m;
        usage = u;
      } catch {
        // leave whatever was loaded before; the metrics section falls back to "no metrics"
      }
    })();
    return () => {
      cancelled = true;
    };
  });

  // Events power both the Events tab and the metrics tab's progress charts, so they're fetched
  // once here and shared. Refetches on id change or when the live snapshot reports a new
  // checkpoint, a new progress tick, or a state change.
  let committedEventsKey = "";
  let eventsGen = 0;
  $effect(() => {
    const forId = id;
    const key = `${forId}:${curState ?? ""}:${curCheckpointTs ?? ""}:${curProgressTs ?? ""}`;
    if (key === committedEventsKey) return;
    const myGen = ++eventsGen;
    (async () => {
      try {
        const evs = await getEvents(forId);
        if (myGen !== eventsGen) return;
        events = evs;
        committedEventsKey = key;
      } catch {
        // leave committedEventsKey stale so the next relevant change retries
      }
    })();
  });

  // On Escape, only the topmost modal layer should close. The BidDialog (rendered by this
  // component) and JobForm (rendered over this panel by App) both mark themselves
  // aria-modal="true"; if either is open, let it handle Escape instead of also closing this panel.
  function onkeydown(e: KeyboardEvent): void {
    if (e.key !== "Escape") return;
    const modals = document.querySelectorAll('[aria-modal="true"]');
    for (const m of modals) {
      if (m !== drawerEl) return;
    }
    onclose();
  }

  const mascotImg = $derived(mascot.pick(notFound || loadError ? "hmm" : "thinking"));

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

  async function saveBid(bid: number, preempt: boolean): Promise<void> {
    actionError = null;
    try {
      await setBid(id, bid, preempt);
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
    // Build with URL + searchParams rather than string-concatenating "?from=...&to=...": a
    // grafana_url that already has a query string (e.g. an org id) would otherwise end up with
    // two "?"s and a broken link.
    const href = new URL(url);
    href.searchParams.set("from", String(from));
    href.searchParams.set("to", to);
    return href.toString();
  }

  interface CloudFact { l: string; v: string; s: string }

  function money(v: number | null): string {
    return v === null ? "–" : `$${v.toFixed(2)}`;
  }

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  /** "Sep 26"-style label for a sweep/retention date — coarser than `hm` (hour:minute), which
   * would read oddly for something days away. Uses local getters like the rest of format.ts;
   * tests run with TZ=UTC so that's equivalent to UTC there. */
  function dateLabel(ts: number): string {
    const d = new Date(ts * 1000);
    return `${MONTHS[d.getMonth()]} ${d.getDate()}`;
  }

  /** What a finished cloud job's persist dir left behind, in the same four shapes the cloud
   * card's "recent results" line uses: pulled home, still on the target awaiting the retention
   * sweep, nothing saved, or the last pull's own error. `null` (job.cloud.persist is `null`)
   * until the job is terminal, in which case this fact is simply omitted. */
  function persistFact(cloud: NonNullable<JobView["cloud"]>): CloudFact | null {
    const p = cloud.persist;
    if (p === null) return null;
    if (p.last_error) return { l: "results", v: "pull failed", s: p.last_error };
    if (p.pulled_to !== null) {
      const stillRemote = p.sweeps_at !== null ? ` · still on ${cloud.target} until ${dateLabel(p.sweeps_at)}` : "";
      return { l: "results", v: `pulled ${resultSize(p.bytes)}`, s: `→ ${p.pulled_to}${stillRemote}` };
    }
    if (p.sweeps_at !== null) return { l: "results", v: `at ${cloud.target}`, s: `until ${dateLabel(p.sweeps_at)}` };
    return { l: "results", v: "nothing saved", s: "" };
  }

  /** The cloud facts grid for a cloud job: null for a job that ran locally. No approve/reject or
   * extend buttons here — those live on the cloud card, which a person reaches from the main
   * page; this panel is read-only. */
  function cloudFacts(job: JobView): CloudFact[] | null {
    const c = job.cloud;
    if (c === null) return null;
    const facts: CloudFact[] = [
      { l: "target", v: c.target, s: c.gpu },
      {
        l: "cost",
        v: money(c.estimated_cost),
        s: c.max_cost !== null ? `up to ${money(c.max_cost)}${c.user_capped ? " · your cap" : ""}` : "estimate unavailable",
      },
      {
        l: "approved time",
        v: c.approved_seconds !== null ? dur(c.approved_seconds) : "not yet approved",
        s: c.full_seconds !== null && c.full_seconds !== c.approved_seconds ? `of ${dur(c.full_seconds)} total` : "",
      },
      { l: "job cap spent", v: money(c.job_spent), s: c.job_cap !== null ? `of ${money(c.job_cap)}` : "" },
      { l: "phase", v: c.phase ?? "–", s: "" },
    ];
    if (c.needs_more_time !== null) {
      facts.push({ l: "needs more time", v: `+${dur(c.needs_more_time)}`, s: "extend it from the cloud card" });
    }
    const results = persistFact(c);
    if (results !== null) facts.push(results);
    return facts;
  }

  const memoryFormat = (v: number): string => `${gib(v)} GiB`;
  const powerFormat = (v: number): string => `${Math.round(v)} W`;
  const tempFormat = (v: number): string => `${Math.round(v)} °C`;

  /** i-th progress series' colour, drawn from the job palette starting just after the job's own
   * colour so it never repeats the job's own chip/timeline colour. */
  function progressColor(job: JobView, i: number): string {
    const base = JOB_COLORS.indexOf(jobColor(job));
    return JOB_COLORS[(Math.max(0, base) + 1 + i) % JOB_COLORS.length];
  }

  const progSeries = $derived(progressSeries(events));

  const otherRunningIds = $derived.by((): number[] => {
    if (jobView === null) return [];
    return jobs
      .filter((j) => j.id !== jobView!.id && j.state === "running")
      .map((j) => j.id)
      .sort((a, b) => a - b);
  });
  const gpuNote = $derived(
    otherRunningIds.length > 0
      ? `GPU-wide · shared with ${otherRunningIds.map((i) => `#${i}`).join(", ")}`
      : "GPU-wide",
  );

  interface AttemptMetricRow {
    attempt: number;
    avgPower: number | null;
    maxPower: number | null;
    maxTemp: number | null;
    energyWh: number | null;
  }

  function buildMetricRows(rows: MetricSummary[]): AttemptMetricRow[] {
    const byAttempt = new Map<number, AttemptMetricRow>();
    for (const r of rows) {
      let row = byAttempt.get(r.attempt);
      if (!row) {
        row = { attempt: r.attempt, avgPower: null, maxPower: null, maxTemp: null, energyWh: null };
        byAttempt.set(r.attempt, row);
      }
      if (r.metric === "power_w") {
        row.avgPower = r.avg;
        row.maxPower = r.max;
      } else if (r.metric === "temp_c") {
        row.maxTemp = r.max;
      } else if (r.metric === "energy_j") {
        row.energyWh = r.total !== null ? r.total / 3600 : null;
      }
    }
    return [...byAttempt.values()].sort((a, b) => a.attempt - b.attempt);
  }
  const metricsSummaryRows = $derived(buildMetricRows(metricsSummary));

  const hasGpuData = $derived(gpu.power_w.length > 0 || gpu.temp_c.length > 0);
  const hasUsageData = $derived(usage.length > 0);
  const hasMetricsSummary = $derived(metricsSummaryRows.length > 0);
  const noMetrics = $derived(!hasGpuData && !hasUsageData && !hasMetricsSummary);
</script>

<svelte:window onkeydown={onkeydown} />
<button type="button" class="scrim open" aria-label="Close" onclick={onclose}></button>
<div class="drawer open" role="dialog" aria-modal="true" aria-labelledby="jobpanel-heading" data-show={activeTab} bind:this={drawerEl}>
  {#if notFound}
    <div class="dhead">
      <h2 id="jobpanel-heading" tabindex="-1" bind:this={headingEl}>No job #{id}.</h2>
      <button class="x" type="button" onclick={onclose} aria-label="Close">✕</button>
    </div>
    <img src={mascotImg} alt="" width="72" height="72" />
  {:else if jobView === null}
    <div class="dhead">
      <h2 id="jobpanel-heading" tabindex="-1" bind:this={headingEl}>
        {loadError ? `Couldn't load job #${id}: ${loadError}` : "Loading…"}
      </h2>
      <button class="x" type="button" onclick={onclose} aria-label="Close">✕</button>
    </div>
    <img src={mascotImg} alt="" width="72" height="72" />
  {:else}
    <div class="dhead">
      <JobChip id={jobView.id} tags={jobView.tags} />
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
    {#if loadError}<div class="acterr">Couldn't load job #{id}: {loadError}</div>{/if}

    <section data-tab="overview">
      <ReasonBox job={jobView} {detail} />
      <Facts job={jobView} {detail} {now} />
      {#if jobView.cloud}
        {@const cloud = jobView.cloud}
        <div class="cloudblock">
          <h3 class="cloudhead">Cloud</h3>
          <div class="facts">
            {#each cloudFacts(jobView) ?? [] as f (f.l)}
              <div class="fact">
                <div class="l">{f.l}</div>
                <div class="v">{f.v}</div>
                <div class="s">{f.s}</div>
              </div>
            {/each}
          </div>
          {#if cloud.console_url}
            <a href={cloud.console_url} target="_blank" rel="noopener">Modal ↗</a>
          {/if}
        </div>
      {/if}
      {#if detail}
        <AttemptsBar {detail} {now} />
      {/if}
    </section>

    <section data-tab="metrics">
      {#if noMetrics}
        <p class="dim">No metrics for this job.</p>
      {:else}
        <div class="charts">
          {#if hasUsageData}
            <LineChart
              label="memory"
              points={usage}
              color={jobColor(jobView)}
              format={memoryFormat}
              max={jobView.limit * 1.1}
              note={`limit ${fmtGib(jobView.limit)}`}
            />
          {/if}
          {#if isLiveState}
            <LineChart label="power" points={gpu.power_w} color={JOB_COLORS[1]} format={powerFormat} note={gpuNote} />
            <LineChart label="temperature" points={gpu.temp_c} color={JOB_COLORS[4]} format={tempFormat} note={gpuNote} />
          {/if}
          {#each Object.entries(progSeries) as [key, points], i (key)}
            <LineChart label={key} {points} color={progressColor(jobView, i)} format={metric} />
          {/each}
        </div>
        {#if isFinishedState && hasMetricsSummary}
          <div class="facts">
            {#each metricsSummaryRows as row (row.attempt)}
              <div class="fact">
                <div class="l">attempt {row.attempt}</div>
                <div class="v">{row.avgPower !== null ? `${Math.round(row.avgPower)} W avg` : "–"}</div>
                <div class="s">
                  max {row.maxPower !== null ? `${Math.round(row.maxPower)} W` : "–"}
                  · {row.maxTemp !== null ? `${Math.round(row.maxTemp)} °C` : "–"}
                  · {row.energyWh !== null ? `${row.energyWh.toFixed(1)} Wh` : "–"}
                </div>
              </div>
            {/each}
          </div>
        {/if}
      {/if}
    </section>
    <section data-tab="logs overview">
      <LogView id={jobView.id} follow={isLiveState} />
    </section>
    <section data-tab="events overview">
      {#if detail}
        <EventList rows={eventRows(detail, events)} />
      {/if}
    </section>

    <div class="kv" data-tab="overview">
      <span class="k">command</span><span class="mono">{jobView.command}</span>
      <span class="k">directory</span><span class="mono">{jobView.cwd}</span>
      <span class="k">git</span><span class="mono">{gitShort(jobView.git_commit)}</span>
      <span class="k">grace</span><span>{graceText(jobView)}</span>
      <span class="k">retries</span><span>{jobView.retries_used} of {jobView.retries} used</span>
      <span class="k">tags</span>
      <span>
        {#if jobView.tags.length > 0}
          {#each jobView.tags as tag, i (tag)}
            {i > 0 ? ", " : ""}{#if onfilter}<button type="button" class="link" onclick={() => onfilter(tag)}>{tag}</button>{:else}<span>{tag}</span>{/if}
          {/each}
        {:else}
          –
        {/if}
      </span>
      <span class="k">submitted</span><span>{hm(jobView.submit_time)}</span>
      {#if grafanaUrl}
        <span class="k">grafana</span>
        <span><a href={grafanaHref(grafanaUrl, jobView)} target="_blank" rel="noopener">Grafana</a></span>
      {/if}
    </div>
  {/if}
</div>

{#if showBid && jobView}
  <BidDialog
    bid={jobView.bid}
    preempt={jobView.preempt}
    onsave={saveBid}
    onclose={() => {
      showBid = false;
      actionError = null;
    }}
  />
{/if}

<style>
  h2:focus {
    outline: none;
  }
  .cloudhead {
    margin: 14px 0 8px;
    font-size: 13px;
    font-weight: 900;
    color: var(--ink-2);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
</style>
