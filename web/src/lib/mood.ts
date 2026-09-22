import { dur, gib, hm, reasonLabel } from "./format";
import type { Transition } from "./transitions";
import type { GpuSeries, JobView, StatusView } from "./types";

export type MascotState =
  | "idle" | "happy" | "start" | "busy" | "waiting" | "sweat" | "hot" | "oom" | "failed"
  | "preempted" | "done" | "thinking" | "hmm" | "confused";

export interface Mood {
  state: MascotState;
  say: string;
  sub: string;
  banner: { tone: "warn" | "bad"; text: string } | null;
}

export const RECENT_SECONDS = 60;

/** Last temp_c sample from a GPU series, or null if there's no series or it's empty. */
export function latestTemp(gpu: GpuSeries | null): number | null {
  if (gpu === null || gpu.temp_c.length === 0) return null;
  return gpu.temp_c[gpu.temp_c.length - 1][1];
}

function memPair(usage: number, limit: number): string {
  return `${gib(usage)} / ${gib(limit)} GiB`;
}

/** The queued job with the earliest projected start, formatted as "next up: #<id> at ~HH:MM",
 * or "" if none of `queued` has a projection. */
function nextUpSub(queued: JobView[]): string {
  const withProjection = queued.filter((j) => j.projected.length > 0);
  if (withProjection.length === 0) return "";
  const earliest = withProjection.reduce((a, b) => (a.projected[0][0] <= b.projected[0][0] ? a : b));
  return `next up: #${earliest.id} at ~${hm(earliest.projected[0][0])}`;
}

/** Earliest projected start among `queued`, as " · next slot ~HH:MM", or "" if none is projected. */
function nextSlotSuffix(queued: JobView[]): string {
  const withProjection = queued.filter((j) => j.projected.length > 0);
  if (withProjection.length === 0) return "";
  const earliest = Math.min(...withProjection.map((j) => j.projected[0][0]));
  return ` · next slot ~${hm(earliest)}`;
}

/** Latest end time across `running`'s projected segments, falling back to start_time +
 * est_runtime for any job with no projection. */
function allDoneSub(running: JobView[]): string {
  const ends = running.map((j) =>
    j.projected.length > 0
      ? Math.max(...j.projected.map(([, end]) => end))
      : (j.start_time ?? 0) + j.est_runtime,
  );
  return `all done by ~${hm(Math.max(...ends))}`;
}

export function mood(input: {
  status: StatusView | null;
  jobs: JobView[];
  tempC: number | null;
  recent: Transition | null;
  now: number;
}): Mood {
  const { status, jobs, tempC, recent, now } = input;

  if (status === null) {
    return { state: "thinking", say: "Connecting…", sub: "", banner: null };
  }

  const runningNow = jobs.filter((j) => j.state === "running");
  const overLimit = runningNow.find((j) => j.over_limit) ?? null;
  if (status.pressure_since !== null || overLimit !== null) {
    const sub = overLimit !== null
      ? `#${overLimit.id} is over its limit (${memPair(overLimit.usage ?? 0, overLimit.limit)})`
      : "the machine is short on memory";
    let banner: Mood["banner"];
    if (status.pressure_since !== null) {
      const secs = Math.round(now - status.pressure_since);
      banner = {
        tone: "warn",
        text: `Memory pressure for ${secs}s. If it lasts 30s, a job over its limit will be stopped.`,
      };
    } else {
      banner = {
        tone: "warn",
        text: `#${overLimit!.id} is over its memory limit. It keeps running unless the machine runs short.`,
      };
    }
    return { state: "sweat", say: "Memory's getting tight…", sub, banner };
  }

  if (tempC !== null && tempC >= status.hot_temp_c) {
    const rounded = Math.round(tempC);
    return {
      state: "hot",
      say: "It's hot in here…",
      sub: `GPU at ${rounded} °C`,
      banner: { tone: "warn", text: `GPU temperature ${rounded} °C (alert threshold ${status.hot_temp_c} °C).` },
    };
  }

  if (recent !== null && now - recent.at < RECENT_SECONDS) {
    const j = recent.job;
    if (recent.kind === "oom") {
      return {
        state: "oom",
        say: `#${j.id} ran out of memory`,
        sub: `peaked at ${gib(j.peak)} of ${gib(j.limit)} GiB`,
        banner: { tone: "bad", text: `#${j.id} ${j.name} was stopped: ${reasonLabel(j.reason)}.` },
      };
    }
    if (recent.kind === "failed") {
      return { state: "failed", say: `#${j.id} crashed`, sub: j.summary || reasonLabel(j.reason), banner: null };
    }
    if (recent.kind === "lost") {
      return {
        state: "confused",
        say: `#${j.id} went missing`,
        sub: j.summary || "its process disappeared (was the machine restarted?)",
        banner: null,
      };
    }
    if (recent.kind === "preempted") {
      return {
        state: "preempted",
        say: `Sorry #${j.id}, your turn's coming!`,
        sub: "it'll resume from its last checkpoint",
        banner: null,
      };
    }
    if (recent.kind === "completed") {
      return {
        state: "done",
        say: `#${j.id} finished!`,
        sub: `${dur(j.run_time)} · peak ${gib(j.peak)} GiB`,
        banner: null,
      };
    }
    // "started", "cancelled": fall through — the toast covers those, not the mascot.
  }

  const running = jobs.filter((j) => j.state === "running" || j.state === "stopping");
  const queued = jobs.filter((j) => j.state === "queued");

  if (running.length === 0 && queued.length === 0) {
    return { state: "idle", say: "Nothing running.", sub: "submit a job with  pasar submit", banner: null };
  }

  if (queued.length > 0 && status.free < 0.1 * status.pool) {
    return {
      state: "busy",
      say: "GPU's packed!",
      sub: `${running.length} running, ${queued.length} waiting${nextSlotSuffix(queued)}`,
      banner: null,
    };
  }

  if (running.length === 0 && queued.length > 0) {
    return { state: "happy", say: `${queued.length} waiting to start`, sub: nextUpSub(queued), banner: null };
  }

  const r = running.length;
  const q = queued.length;
  const say = `${r} job${r === 1 ? "" : "s"} cooking${q > 0 ? `, ${q} waiting` : ""}`;
  const sub = q > 0 ? nextUpSub(queued) : allDoneSub(running);
  return { state: "happy", say, sub, banner: null };
}
