<script lang="ts">
  import { onMount, untrack } from "svelte";
  import { Live } from "./lib/live.svelte";
  import { router } from "./lib/router.svelte";
  import { mood, latestTemp, type MascotState } from "./lib/mood";
  import { mascot } from "./lib/mascot.svelte";
  import { transitions, type Transition } from "./lib/transitions";
  import { AllJobs } from "./lib/alljobs.svelte";
  import { Sparks } from "./lib/sparks.svelte";
  import {
    effectiveSort, filterToSearch, isActive, knownValues, matches, parseFilter, sortJobs, stateCounts,
    type Filter,
  } from "./lib/jobfilter";
  import Header from "./components/Header.svelte";
  import Banner from "./components/Banner.svelte";
  import Tiles from "./components/Tiles.svelte";
  import Timeline from "./components/Timeline.svelte";
  import JobTable from "./components/JobTable.svelte";
  import Toast from "./components/Toast.svelte";
  import JobPanel from "./components/JobPanel.svelte";
  import JobForm from "./components/JobForm.svelte";
  import type { JobDetail, JobView, Snapshot } from "./lib/types";

  const live = new Live();
  const allJobs = new AllJobs();
  const sparks = new Sparks();

  // The filter lives in the URL (see router.svelte.ts) so back/forward and bookmarks work, and it
  // survives opening a job. While it's active, the full job history (past and present) is kept
  // fresh in `allJobs`; the table and schedule chart both read from it via `source`.
  const filter = $derived(parseFilter(router.search));
  const filterActive = $derived(isActive(filter));
  // `untrack` around the call: `allJobs.update` reads and (via #overlay) writes `allJobs.jobs`,
  // so without this the effect would depend on its own write and re-trigger itself every tick —
  // it should only re-run when the filter's active-ness or the live snapshot itself changes.
  $effect(() => {
    const liveJobs = live.snapshot?.jobs ?? [];
    const active = filterActive;
    untrack(() => allJobs.update(active, liveJobs));
  });
  // While a filter is active, `source` is the (kept-fresh) all-jobs cache: the table, counts and
  // total need the full history, not just the live snapshot's ~24h window. Once cleared, there's
  // no reason to keep reading the cache — it stops being refreshed the instant `filterActive`
  // goes false (see `allJobs.update` above), so it would otherwise go stale (e.g. a job finishing
  // wouldn't update the "running" chip's count).
  const source = $derived(filterActive ? (allJobs.jobs ?? live.snapshot?.jobs ?? []) : (live.snapshot?.jobs ?? []));
  const filterCounts = $derived(stateCounts(source, filter));
  // The "+ tag"/"+ submitter" dropdowns list every value ever seen, so they read from the live
  // snapshot *and* the cache (when it's been loaded) regardless of whether the filter is active
  // right now — unlike `source`, they shouldn't go blank (or lose history) just because the
  // filter was cleared.
  const knownSource = $derived.by(() => {
    const liveJobs = live.snapshot?.jobs ?? [];
    const cache = allJobs.jobs;
    if (cache === null) return liveJobs;
    const liveIds = new Set(liveJobs.map((j) => j.id));
    return [...liveJobs, ...cache.filter((j) => !liveIds.has(j.id))];
  });
  const filterKnown = $derived(knownValues(knownSource));
  const tableJobs = $derived(
    filterActive ? sortJobs(source.filter((j) => matches(j, filter)), effectiveSort(filter)) : (live.snapshot?.jobs ?? []),
  );
  // Row sparklines follow whatever's actually on screen, so filtering to a sweep fetches that
  // sweep's curves. `untrack` for the same reason as `allJobs.update` above: the call writes the
  // store's own state, and the effect must only re-run when the visible rows change.
  $effect(() => {
    const ids = tableJobs.map((j) => j.id);
    untrack(() => sparks.update(ids));
  });

  function setFilter(f: Filter, replace = false): void {
    router.setSearch(filterToSearch(f), replace);
  }
  // Filters by the tag and navigates back to "/" (closing the panel) in a single history push —
  // two separate router calls (setFilter then router.go) would each push their own entry, so one
  // click would need two Backs to undo.
  function addTagFilter(tag: string): void {
    const next = filter.tags.includes(tag) ? filter : { ...filter, tags: [...filter.tags, tag] };
    router.go(`/${filterToSearch(next)}`);
  }

  let showSubmit = $state(false);
  let restartWith = $state<JobDetail | null>(null);

  function afterFormDone(newJob: JobView): void {
    router.go(`/jobs/${newJob.id}`);
  }

  let recent = $state<Transition | null>(null);
  let bounceKey = $state(0);

  interface ToastItem {
    message: string;
    image: string;
  }
  let toastQueue = $state<ToastItem[]>([]);
  let toast = $state<ToastItem | null>(null);
  let toastTimer: ReturnType<typeof setTimeout> | null = null;

  function advanceToast(): void {
    if (toast !== null || toastQueue.length === 0) return;
    const [next, ...rest] = toastQueue;
    toast = next;
    toastQueue = rest;
    toastTimer = setTimeout(() => {
      toast = null;
      advanceToast();
    }, 2800);
  }

  function enqueueToast(message: string, imageState: MascotState): void {
    const item: ToastItem = { message, image: mascot.pick(imageState) };
    const grown = [...toastQueue, item];
    toastQueue = grown.length > 5 ? grown.slice(grown.length - 5) : grown;
    advanceToast();
  }

  function toastFor(t: Transition): { message: string; image: MascotState } | null {
    switch (t.kind) {
      case "started":
        return { message: `#${t.job.id} ${t.job.name} started!`, image: "start" };
      case "completed":
        return { message: `#${t.job.id} finished!`, image: "done" };
      case "failed":
        return { message: `#${t.job.id} crashed`, image: "failed" };
      case "oom":
        return { message: `#${t.job.id} ran out of memory`, image: "oom" };
      case "lost":
        return { message: `#${t.job.id} went missing`, image: "confused" };
      case "preempted":
        return { message: `#${t.job.id} was preempted`, image: "preempted" };
      case "cancelled":
        return null;
    }
  }

  function handleSnapshot(prev: Snapshot | null, next: Snapshot): void {
    const trs = transitions(prev?.jobs ?? null, next.jobs, next.status.now);
    const meaningful = trs.filter((t) => t.kind !== "started" && t.kind !== "cancelled");
    if (meaningful.length > 0) recent = meaningful[meaningful.length - 1];
    for (const t of trs) {
      const info = toastFor(t);
      if (info === null) continue;
      enqueueToast(info.message, info.image);
      if (t.kind === "started") bounceKey += 1;
    }
  }

  const currentMood = $derived(
    mood({
      status: live.snapshot?.status ?? null,
      jobs: live.snapshot?.jobs ?? [],
      tempC: latestTemp(live.gpu),
      recent,
      now: live.snapshot?.status.now ?? Date.now() / 1000,
    }),
  );

  let headerImage = $state(mascot.pick("thinking"));
  // Keyed on the mood state *and* whether the mascot manifest has loaded: without `mascot.loaded`
  // here, a snapshot arriving before mascot.load() resolves would pick (and then keep) the
  // built-in art for the current state, never re-picking once the user's own mascot art is ready.
  let lastMoodKey: string | null = null;
  $effect(() => {
    const key = `${currentMood.state}:${mascot.loaded}`;
    if (key !== lastMoodKey) {
      lastMoodKey = key;
      headerImage = mascot.pick(currentMood.state);
    }
  });

  $effect(() => {
    const jobs = live.snapshot?.jobs ?? [];
    const r = jobs.filter((j) => j.state === "running").length;
    const q = jobs.filter((j) => j.state === "queued").length;
    document.title = `pasar · ${r} running, ${q} queued`;
  });

  function openSubmit(): void {
    showSubmit = true;
  }

  onMount(() => {
    const unsubscribe = live.onSnapshot(handleSnapshot);
    live.start();
    void mascot.load();
    return () => {
      unsubscribe();
      live.stop();
      if (toastTimer !== null) clearTimeout(toastTimer);
    };
  });
</script>

<div class="wrap">
  <Header mood={currentMood} image={headerImage} {bounceKey} onsubmit={openSubmit} />
  <Banner banner={currentMood.banner} connected={live.connected} hasConnectedOnce={live.snapshot !== null} />

  {#if live.snapshot}
    <Tiles status={live.snapshot.status} jobs={live.snapshot.jobs} gpu={live.gpu} />
    <Timeline
      jobs={live.snapshot.jobs}
      pool={live.snapshot.status.pool}
      now={live.snapshot.status.now}
      onopen={(id) => router.go(`/jobs/${id}`)}
      dim={filterActive ? (j) => !matches(j, filter) : undefined}
    />
    <JobTable
      jobs={tableJobs}
      pool={live.snapshot.status.pool}
      now={live.snapshot.status.now}
      selected={router.route.name === "job" ? router.route.id : null}
      onopen={(id) => router.go(`/jobs/${id}`)}
      {filter}
      total={source.length}
      counts={filterCounts}
      known={filterKnown}
      sourceJobs={source}
      sparks={sparks.data}
      onfilter={setFilter}
    />
  {/if}

  {#if router.route.name === "job"}
    {@const jobId = router.route.id}
    <JobPanel
      id={jobId}
      live={live.snapshot?.jobs.find((j) => j.id === jobId) ?? null}
      now={live.snapshot?.status.now ?? Date.now() / 1000}
      grafanaUrl={live.snapshot?.status.grafana_url ?? null}
      jobs={live.snapshot?.jobs ?? []}
      onfilter={addTagFilter}
      onclose={() => router.go("/")}
      onrestartwith={(job) => { restartWith = job; }}
    />
  {/if}

  {#if showSubmit}
    <JobForm mode="submit" onclose={() => (showSubmit = false)} ondone={afterFormDone} />
  {/if}
  {#if restartWith}
    <JobForm mode="restart" job={restartWith} onclose={() => (restartWith = null)} ondone={afterFormDone} />
  {/if}
</div>
<Toast message={toast?.message ?? null} image={toast?.image ?? ""} />
