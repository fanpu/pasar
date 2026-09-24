import { describe, expect, it } from "vitest";
import { Mascot } from "../lib/mascot.svelte";
import { latestTemp, mood, RECENT_SECONDS } from "../lib/mood";
import type { Transition } from "../lib/transitions";
import type { GpuSeries } from "../lib/types";
import { GIB, job, NOW, status } from "./fixtures";
import { cloudJobView } from "./fixtures/cloud";

describe("mood", () => {
  it("1. connecting when status is null", () => {
    expect(mood({ status: null, jobs: [], tempC: null, recent: null, now: NOW })).toEqual({
      state: "thinking",
      say: "Connecting…",
      sub: "",
      banner: null,
    });
  });

  it("2. memory pressure with an over-limit job", () => {
    const j45 = job({ id: 45, state: "running", usage: 26.1 * GIB, limit: 24 * GIB, over_limit: true });
    const m = mood({ status: status({ pressure_since: NOW - 18 }), jobs: [j45], tempC: null, recent: null, now: NOW });
    expect(m.state).toBe("sweat");
    expect(m.say).toBe("Memory's getting tight…");
    expect(m.sub).toBe("#45 is over its limit (26.1 / 24.0 GiB)");
    expect(m.banner).toEqual({
      tone: "warn",
      text: "Memory pressure for 18s. If it lasts 30s, a job over its limit will be stopped.",
    });
  });

  it("2. memory pressure without an over-limit job", () => {
    const m = mood({ status: status({ pressure_since: NOW - 5 }), jobs: [], tempC: null, recent: null, now: NOW });
    expect(m.state).toBe("sweat");
    expect(m.sub).toBe("the machine is short on memory");
  });

  it("2. over-limit job without active pressure", () => {
    const j45 = job({ id: 45, state: "running", usage: 26.1 * GIB, limit: 24 * GIB, over_limit: true });
    const m = mood({ status: status(), jobs: [j45], tempC: null, recent: null, now: NOW });
    expect(m.state).toBe("sweat");
    expect(m.sub).toBe("#45 is over its limit (26.1 / 24.0 GiB)");
    expect(m.banner).toEqual({
      tone: "warn",
      text: "#45 is over its memory limit. It keeps running unless the machine runs short.",
    });
  });

  it("3. hot", () => {
    const m = mood({ status: status({ hot_temp_c: 85 }), jobs: [], tempC: 87, recent: null, now: NOW });
    expect(m.state).toBe("hot");
    expect(m.say).toBe("It's hot in here…");
    expect(m.sub).toBe("GPU at 87 °C");
    expect(m.banner).toEqual({ tone: "warn", text: "GPU temperature 87 °C (alert threshold 85 °C)." });
  });

  it("4. recent oom", () => {
    const j = job({ id: 39, name: "tokenizer-bench", state: "failed", reason: "gpu_oom", peak: 11.9 * GIB, limit: 12 * GIB });
    const recent: Transition = { kind: "oom", job: j, at: NOW - 5 };
    const m = mood({ status: status(), jobs: [], tempC: null, recent, now: NOW });
    expect(m.state).toBe("oom");
    expect(m.say).toBe("#39 ran out of memory");
    expect(m.sub).toBe("peaked at 11.9 of 12.0 GiB");
    expect(m.banner).toEqual({ tone: "bad", text: "#39 tokenizer-bench was stopped: GPU out of memory." });
  });

  it("4. recent failed", () => {
    const j = job({ id: 45, state: "failed", reason: "exit", summary: "exit code 1" });
    const recent: Transition = { kind: "failed", job: j, at: NOW - 5 };
    const m = mood({ status: status(), jobs: [], tempC: null, recent, now: NOW });
    expect(m.state).toBe("failed");
    expect(m.say).toBe("#45 crashed");
    expect(m.sub).toBe("exit code 1");
    expect(m.banner).toBeNull();
  });

  it("4. recent failed falls back to the reason label when summary is empty", () => {
    const j = job({ id: 45, state: "failed", reason: "exit", summary: "" });
    const recent: Transition = { kind: "failed", job: j, at: NOW - 5 };
    const m = mood({ status: status(), jobs: [], tempC: null, recent, now: NOW });
    expect(m.sub).toBe("crashed");
  });

  it("4. recent lost", () => {
    const j = job({ id: 41, state: "failed", reason: "lost", summary: "" });
    const recent: Transition = { kind: "lost", job: j, at: NOW - 5 };
    const m = mood({ status: status(), jobs: [], tempC: null, recent, now: NOW });
    expect(m.state).toBe("confused");
    expect(m.say).toBe("#41 went missing");
    expect(m.sub).toBe("its process disappeared (was the machine restarted?)");
    expect(m.banner).toBeNull();
  });

  it("4. recent preempted", () => {
    const j = job({ id: 48, state: "queued", reason: "preempted" });
    const recent: Transition = { kind: "preempted", job: j, at: NOW - 5 };
    const m = mood({ status: status(), jobs: [], tempC: null, recent, now: NOW });
    expect(m.state).toBe("preempted");
    expect(m.say).toBe("Sorry #48, your turn's coming!");
    expect(m.sub).toBe("it'll resume from its last checkpoint");
    expect(m.banner).toBeNull();
  });

  it("4. recent completed", () => {
    const j = job({ id: 45, state: "completed", run_time: 3725, peak: 5.2 * GIB });
    const recent: Transition = { kind: "completed", job: j, at: NOW - 5 };
    const m = mood({ status: status(), jobs: [], tempC: null, recent, now: NOW });
    expect(m.state).toBe("done");
    expect(m.say).toBe("#45 finished!");
    expect(m.sub).toBe("1h02 · peak 5.2 GiB");
    expect(m.banner).toBeNull();
  });

  it("4. a recent transition older than RECENT_SECONDS is ignored", () => {
    expect(RECENT_SECONDS).toBe(60);
    const j = job({ id: 45, state: "completed" });
    const recent: Transition = { kind: "completed", job: j, at: NOW - 61 };
    const m = mood({ status: status(), jobs: [], tempC: null, recent, now: NOW });
    expect(m.state).toBe("idle");
  });

  it("4. started/cancelled fall through to the lower-priority rules", () => {
    const running = job({ id: 7, state: "running" });
    const recent: Transition = { kind: "started", job: running, at: NOW - 5 };
    const m = mood({ status: status(), jobs: [running], tempC: null, recent, now: NOW });
    expect(m.state).toBe("happy");
  });

  it("4.5. a cloud job awaiting approval wins over idle/busy/happy", () => {
    const awaiting1 = cloudJobView({ id: 201, state: "awaiting" }, { max_cost: 8 });
    const m = mood({ status: status(), jobs: [awaiting1], tempC: null, recent: null, now: NOW });
    expect(m.state).toBe("waiting");
    expect(m.say).toBe("A cloud job wants your OK!");
    expect(m.sub).toBe("1 awaiting · up to $8.00");
    expect(m.banner).toBeNull();
  });

  it("4.5. sums up-to cost over every priced awaiting job, and says how many it couldn't price", () => {
    const awaiting1 = cloudJobView({ id: 201, state: "awaiting" }, { max_cost: 8 });
    const awaiting2 = cloudJobView({ id: 202, state: "awaiting" }, { max_cost: 3.5 });
    const awaiting3 = cloudJobView({ id: 203, state: "awaiting" }, { max_cost: null });
    const m = mood({ status: status(), jobs: [awaiting1, awaiting2, awaiting3], tempC: null, recent: null, now: NOW });
    expect(m.sub).toBe("3 awaiting · up to $11.50 (1 unpriced)");
  });

  it("4.5. says so rather than giving a cost when no awaiting job has a known max_cost", () => {
    const awaiting1 = cloudJobView({ id: 201, state: "awaiting" }, { max_cost: null });
    const m = mood({ status: status(), jobs: [awaiting1], tempC: null, recent: null, now: NOW });
    expect(m.sub).toBe("1 awaiting (unpriced)");
  });

  it("4.5. memory pressure/hot alarms still win over an awaiting cloud job", () => {
    const awaiting1 = cloudJobView({ id: 201, state: "awaiting" }, { max_cost: 8 });
    const m = mood({ status: status({ pressure_since: NOW - 5 }), jobs: [awaiting1], tempC: null, recent: null, now: NOW });
    expect(m.state).toBe("sweat");
  });

  it("4.5. a fresh transition's own message still wins over an awaiting cloud job", () => {
    const awaiting1 = cloudJobView({ id: 201, state: "awaiting" }, { max_cost: 8 });
    const finished = job({ id: 45, state: "completed", run_time: 3725, peak: 5.2 * GIB });
    const recent: Transition = { kind: "completed", job: finished, at: NOW - 5 };
    const m = mood({ status: status(), jobs: [awaiting1], tempC: null, recent, now: NOW });
    expect(m.state).toBe("done");
  });

  it("5. idle when nothing is running or queued", () => {
    expect(mood({ status: status(), jobs: [], tempC: null, recent: null, now: NOW })).toEqual({
      state: "idle",
      say: "Nothing running.",
      sub: "submit a job with  pasar submit",
      banner: null,
    });
  });

  it("6. busy when the GPU is packed", () => {
    const running1 = job({ id: 1, state: "running" });
    const running2 = job({ id: 2, state: "running" });
    const queued1 = job({ id: 48, state: "queued", projected: [[NOW + 21 * 60, NOW + 66 * 60]] });
    const m = mood({
      status: status({ free: 5 * GIB }),
      jobs: [running1, running2, queued1],
      tempC: null,
      recent: null,
      now: NOW,
    });
    expect(m.state).toBe("busy");
    expect(m.say).toBe("GPU's packed!");
    expect(m.sub).toBe("2 running, 1 waiting · next slot ~14:47");
    expect(m.banner).toBeNull();
  });

  it("6. busy with no queued projection omits the next slot", () => {
    const running1 = job({ id: 1, state: "running" });
    const queued1 = job({ id: 48, state: "queued" });
    const m = mood({
      status: status({ free: 5 * GIB }),
      jobs: [running1, queued1],
      tempC: null,
      recent: null,
      now: NOW,
    });
    expect(m.state).toBe("busy");
    expect(m.sub).toBe("1 running, 1 waiting");
  });

  it("7. happy with running and queued jobs (not busy)", () => {
    const running1 = job({ id: 1, state: "running" });
    const running2 = job({ id: 2, state: "running" });
    const queued1 = job({ id: 48, state: "queued", projected: [[NOW + 21 * 60, NOW + 66 * 60]] });
    const m = mood({ status: status(), jobs: [running1, running2, queued1], tempC: null, recent: null, now: NOW });
    expect(m.state).toBe("happy");
    expect(m.say).toBe("2 jobs cooking, 1 waiting");
    expect(m.sub).toBe("next up: #48 at ~14:47");
    expect(m.banner).toBeNull();
  });

  it("7. happy singular job, nothing queued", () => {
    const running1 = job({ id: 1, state: "running", start_time: NOW - 600, est_runtime: 1200 });
    const m = mood({ status: status(), jobs: [running1], tempC: null, recent: null, now: NOW });
    expect(m.state).toBe("happy");
    expect(m.say).toBe("1 job cooking");
    expect(m.sub).toBe("all done by ~14:36");
    expect(m.banner).toBeNull();
  });

  it("7. special case: nothing running but jobs are queued (not busy)", () => {
    const queued1 = job({ id: 48, state: "queued", projected: [[NOW + 21 * 60, NOW + 66 * 60]] });
    const m = mood({ status: status(), jobs: [queued1], tempC: null, recent: null, now: NOW });
    expect(m.state).toBe("happy");
    expect(m.say).toBe("1 waiting to start");
    expect(m.sub).toBe("next up: #48 at ~14:47");
    expect(m.banner).toBeNull();
  });

  it("7. special case with no queued projection yields an empty sub", () => {
    const queued1 = job({ id: 48, state: "queued" });
    const m = mood({ status: status(), jobs: [queued1], tempC: null, recent: null, now: NOW });
    expect(m.state).toBe("happy");
    expect(m.say).toBe("1 waiting to start");
    expect(m.sub).toBe("");
  });
});

describe("Mascot.pick", () => {
  it("returns a random url from the manifest", () => {
    const m = new Mascot();
    m.manifest = { done: ["/a", "/b"] };
    expect(m.pick("done", () => 0.99)).toBe("/b");
  });

  it("falls back to the built-in svg when the state has no images", () => {
    const m = new Mascot();
    expect(m.pick("idle")).toBe("/mascot/builtin/idle.png");
  });
});

describe("latestTemp", () => {
  it("returns the last temp_c sample", () => {
    const gpu: GpuSeries = { power_w: [], temp_c: [[1, 50], [2, 60]], util_pct: [] };
    expect(latestTemp(gpu)).toBe(60);
  });

  it("returns null when there's no series or no samples", () => {
    expect(latestTemp(null)).toBeNull();
    expect(latestTemp({ power_w: [], temp_c: [], util_pct: [] })).toBeNull();
  });
});
