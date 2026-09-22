<script lang="ts">
  import { onMount } from "svelte";
  import { Live } from "./lib/live.svelte";
  import { router } from "./lib/router.svelte";
  import { mood, latestTemp, type MascotState } from "./lib/mood";
  import { mascot } from "./lib/mascot.svelte";
  import { transitions, type Transition } from "./lib/transitions";
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
  <Banner banner={currentMood.banner} connected={live.connected} />

  {#if live.snapshot}
    <Tiles status={live.snapshot.status} jobs={live.snapshot.jobs} gpu={live.gpu} />
    <Timeline
      jobs={live.snapshot.jobs}
      pool={live.snapshot.status.pool}
      now={live.snapshot.status.now}
      onopen={(id) => router.go(`/jobs/${id}`)}
    />
    <JobTable
      jobs={live.snapshot.jobs}
      pool={live.snapshot.status.pool}
      now={live.snapshot.status.now}
      selected={router.route.name === "job" ? router.route.id : null}
      onopen={(id) => router.go(`/jobs/${id}`)}
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
