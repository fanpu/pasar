import { describe, expect, it } from "vitest";
import { spriteKind, spriteLabel, spriteState, spriteUrl } from "../lib/jobsprite";
import type { JobSpriteManifest } from "../lib/types";
import { job } from "./fixtures";
import { awaitingFresh, awaitingReapproval, cloudJobView } from "./fixtures/cloud";

const EMPTY: JobSpriteManifest = { local: {}, cloud: {} };

describe("spriteKind", () => {
  it("is local for a plain job and cloud for one with a cloud block", () => {
    expect(spriteKind(job())).toBe("local");
    expect(spriteKind(cloudJobView())).toBe("cloud");
  });
});

describe("spriteState", () => {
  it("maps a plain running job to running, and one over its memory limit to over", () => {
    expect(spriteState(job({ state: "running", over_limit: false }))).toBe("running");
    expect(spriteState(job({ state: "running", over_limit: true }))).toBe("over");
  });

  it("maps a cloud job starting or pending to starting, running once it's actually running", () => {
    expect(spriteState(cloudJobView({ state: "running" }, { phase: "starting" }))).toBe("starting");
    expect(spriteState(cloudJobView({ state: "running" }, { phase: "pending" }))).toBe("starting");
    expect(spriteState(cloudJobView({ state: "running" }, { phase: "running" }))).toBe("running");
  });

  it("never marks a cloud job over its memory limit even if over_limit were somehow set", () => {
    expect(spriteState(cloudJobView({ state: "running", over_limit: true }, { phase: "running" }))).toBe("running");
  });

  it("maps stopping the same way for either kind", () => {
    expect(spriteState(job({ state: "stopping" }))).toBe("stopping");
    expect(spriteState(cloudJobView({ state: "stopping" }))).toBe("stopping");
  });

  it("maps a re-queued-after-preemption local job to preempted, a plain queued one to queued", () => {
    expect(spriteState(job({ state: "queued", preemptions: 2 }))).toBe("preempted");
    expect(spriteState(job({ state: "queued", preemptions: 0 }))).toBe("queued");
  });

  it("never marks a cloud queued job preempted, even with a nonzero preemptions count", () => {
    expect(spriteState(cloudJobView({ state: "queued", preemptions: 3 }))).toBe("queued");
  });

  it("maps a fresh awaiting job to awaiting, and one back for reapproval (has a reason) to paused", () => {
    expect(spriteState(awaitingFresh)).toBe("awaiting");
    expect(spriteState(awaitingReapproval)).toBe("paused");
  });

  it("passes completed/failed/cancelled straight through", () => {
    expect(spriteState(job({ state: "completed" }))).toBe("completed");
    expect(spriteState(job({ state: "failed" }))).toBe("failed");
    expect(spriteState(job({ state: "cancelled" }))).toBe("cancelled");
  });
});

describe("spriteUrl", () => {
  it("uses the exact state's sprite when one exists", () => {
    const manifest: JobSpriteManifest = { local: { running: ["/mascot/jobs/local-running.png"] }, cloud: {} };
    expect(spriteUrl(manifest, job({ id: 1, state: "running" }))).toBe("/mascot/jobs/local-running.png");
  });

  it("falls back to a plain neighbour when the exact state is missing", () => {
    // "over" has no sprite of its own here, so a local job over its limit falls back to its
    // neighbour, "running".
    const manifest: JobSpriteManifest = { local: { running: ["/mascot/jobs/local-running.png"] }, cloud: {} };
    expect(spriteUrl(manifest, job({ id: 1, state: "running", over_limit: true }))).toBe("/mascot/jobs/local-running.png");
  });

  it("never crosses from one kind's sprites into the other's", () => {
    // Only local has anything; a cloud job in the same state must not borrow it.
    const manifest: JobSpriteManifest = { local: { running: ["/mascot/jobs/local-running.png"] }, cloud: {} };
    expect(spriteUrl(manifest, cloudJobView({ id: 1, state: "running" }, { phase: "running" }))).toBeNull();
  });

  it("falls back to idle when neither the state nor its neighbour has a sprite", () => {
    const manifest: JobSpriteManifest = { local: { idle: ["/mascot/jobs/local-idle.png"] }, cloud: {} };
    expect(spriteUrl(manifest, job({ id: 1, state: "completed" }))).toBe("/mascot/jobs/local-idle.png");
  });

  it("renders nothing when nothing in the chain has a sprite (an empty manifest looks like today)", () => {
    expect(spriteUrl(EMPTY, job({ id: 1, state: "running" }))).toBeNull();
  });

  it("picks a variant deterministically by job id, and keeps picking the same one", () => {
    const urls = ["/a.png", "/b.png", "/c.png"];
    const manifest: JobSpriteManifest = { local: { running: urls }, cloud: {} };
    const first = spriteUrl(manifest, job({ id: 7, state: "running" }));
    const second = spriteUrl(manifest, job({ id: 7, state: "running" }));
    expect(first).toBe(second);
    expect(urls).toContain(first);
    // Different ids may land on different variants, but always one from the list, always
    // reproducibly.
    for (const id of [0, 1, 2, 3, 4, 100, 101]) {
      const j = job({ id, state: "running" });
      expect(urls).toContain(spriteUrl(manifest, j));
      expect(spriteUrl(manifest, j)).toBe(spriteUrl(manifest, j));
    }
  });
});

describe("spriteLabel", () => {
  it("names the kind and the state", () => {
    expect(spriteLabel(job({ state: "running" }))).toBe("local job, running");
    expect(spriteLabel(cloudJobView({ state: "queued" }))).toBe("cloud job, queued");
  });
});
