import { render, screen, fireEvent, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App.svelte";
import * as api from "../lib/api";
import { mascot } from "../lib/mascot.svelte";
import { router } from "../lib/router.svelte";
import { job, jobDetail, status } from "./fixtures";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, getGpu: vi.fn(), getMascot: vi.fn(), getAllJobs: vi.fn(), getJob: vi.fn() };
});

class FakeEventSource {
  static last: FakeEventSource | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  constructor(public url: string) {
    FakeEventSource.last = this;
  }
  close(): void {}
  emit(data: unknown): void {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(data) }));
  }
}

describe("App", () => {
  beforeEach(() => {
    vi.mocked(api.getGpu).mockReset().mockResolvedValue({ power_w: [], temp_c: [], util_pct: [] });
    vi.mocked(api.getMascot).mockReset().mockResolvedValue({});
    vi.mocked(api.getAllJobs).mockReset().mockResolvedValue([]);
    vi.mocked(api.getJob).mockReset();
    vi.stubGlobal("EventSource", FakeEventSource);
    history.pushState({}, "", "/");
    // Reset the shared mascot singleton so an earlier test's loaded manifest doesn't leak in.
    mascot.manifest = {};
    mascot.loaded = false;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("re-picks the header mascot once the manifest loads, even though the mood state hasn't changed", async () => {
    let resolveManifest!: (m: Record<string, string[]>) => void;
    vi.mocked(api.getMascot).mockReturnValue(
      new Promise((resolve) => {
        resolveManifest = resolve;
      }),
    );
    const { container } = render(App);

    // No snapshot has arrived yet, so the mood stays "thinking" the whole time: before the
    // manifest resolves, the header must be showing the built-in art for that state.
    const imgBefore = container.querySelector<HTMLImageElement>(".mascot")!;
    expect(imgBefore.src).toContain("/mascot/builtin/thinking.png");

    resolveManifest({ thinking: ["/mascot/thinking-1.png"] });
    await waitFor(() => {
      const img = container.querySelector<HTMLImageElement>(".mascot")!;
      expect(img.src).toContain("/mascot/thinking-1.png");
    });
  });

  it("filters the job list to a tag given in the URL, using jobs from the all-jobs fetch", async () => {
    history.pushState({}, "", "/?tag=sweep-a");
    dispatchEvent(new PopStateEvent("popstate"));
    const matching = job({ id: 101, name: "match-me", tags: ["sweep-a"], state: "completed", end_time: 1000 });
    const other = job({ id: 102, name: "not-me", tags: ["other"], state: "completed", end_time: 1000 });
    vi.mocked(api.getAllJobs).mockResolvedValue([matching, other]);

    const { container } = render(App);
    FakeEventSource.last!.emit({ status: status(), jobs: [] });

    const table = await waitFor(() => {
      const t = container.querySelector("table");
      expect(t).not.toBeNull();
      return t!;
    });
    expect(await within(table).findByText("match-me")).toBeInTheDocument();
    expect(within(table).queryByText("not-me")).toBeNull();
  });

  it("doesn't spin forever when live jobs repeatedly overlap the all-jobs cache while filtered (regression for effect_update_depth_exceeded)", async () => {
    history.pushState({}, "", "/?tag=sweep-a");
    dispatchEvent(new PopStateEvent("popstate"));
    // A fresh object per call, like a real snapshot parsed anew from JSON every tick, even though
    // nothing about the job actually changes — this is exactly what used to make AllJobs#overlay
    // reassign `jobs` on every tick and re-trigger App's own update effect without end.
    const steady = () => job({ id: 5, name: "steady", tags: ["sweep-a"], state: "running", start_time: 1000, run_time: 30 });
    vi.mocked(api.getAllJobs).mockResolvedValue([steady()]);

    const { container } = render(App);
    const es = FakeEventSource.last!;
    es.emit({ status: status(), jobs: [steady()] });

    const table = await waitFor(() => {
      const t = container.querySelector("table");
      expect(t).not.toBeNull();
      return t!;
    });
    expect(within(table).getByText("steady")).toBeInTheDocument();

    // Several more ticks with a content-identical (but reference-distinct) live job.
    for (let i = 0; i < 10; i++) {
      es.emit({ status: status({ version: i + 2 }), jobs: [steady()] });
    }

    // Still just the one row, still responsive — no runaway loop, no crash.
    expect(within(table).getAllByText("steady").length).toBe(1);
  });

  it("clicking a tag in the job panel filters and closes the panel with a single history push", async () => {
    history.pushState({}, "", "/jobs/42");
    dispatchEvent(new PopStateEvent("popstate"));
    const liveJob = job({ id: 42, name: "llama-sft", state: "queued", tags: ["sweep-a"] });
    vi.mocked(api.getJob).mockResolvedValue(jobDetail({ id: 42, name: "llama-sft", state: "queued", tags: ["sweep-a"] }));

    render(App);
    FakeEventSource.last!.emit({ status: status(), jobs: [liveJob] });

    const drawer = await waitFor(() => {
      const d = document.querySelector<HTMLElement>(".drawer.open");
      expect(d).not.toBeNull();
      return d!;
    });
    const tagBtn = await within(drawer).findByRole("button", { name: "sweep-a" });

    const pushSpy = vi.spyOn(history, "pushState");
    const replaceSpy = vi.spyOn(history, "replaceState");
    await fireEvent.click(tagBtn);

    expect(pushSpy).toHaveBeenCalledTimes(1);
    expect(replaceSpy).not.toHaveBeenCalled();
    expect(router.route).toEqual({ name: "home" });
    expect(router.search).toBe("?tag=sweep-a");
    pushSpy.mockRestore();
    replaceSpy.mockRestore();
  });
});
