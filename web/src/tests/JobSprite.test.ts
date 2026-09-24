import { render } from "@testing-library/svelte";
import { afterEach, describe, expect, it } from "vitest";
import JobSprite from "../components/JobSprite.svelte";
import { mascot } from "../lib/mascot.svelte";
import { job } from "./fixtures";

describe("JobSprite", () => {
  afterEach(() => {
    mascot.manifest = {};
  });

  it("renders the sprite at the manifest's URL when the job's kind/state has one", () => {
    mascot.manifest = { jobs: { local: { running: ["/mascot/jobs/local-running.png"] }, cloud: {} } };
    const { container } = render(JobSprite, { job: job({ id: 1, state: "running" }) });
    const img = container.querySelector<HTMLImageElement>("img");
    expect(img).not.toBeNull();
    expect(img!.src).toContain("/mascot/jobs/local-running.png");
    expect(img!.alt).toBe("");
    expect(container.querySelector('[role="img"]')?.getAttribute("aria-label")).toBe("local job, running");
  });

  it("renders nothing when the manifest has no job sprites at all", () => {
    mascot.manifest = {};
    const { container } = render(JobSprite, { job: job({ id: 1, state: "running" }) });
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector('[role="img"]')).toBeNull();
  });

  it("renders nothing when the job's kind has sprites but none the fallback chain reaches", () => {
    mascot.manifest = { jobs: { local: {}, cloud: { running: ["/mascot/jobs/cloud-running.png"] } } };
    const { container } = render(JobSprite, { job: job({ id: 1, state: "running" }) });
    expect(container.querySelector("img")).toBeNull();
  });
});
