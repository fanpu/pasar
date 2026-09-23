import { render, screen, fireEvent, waitFor, within } from "@testing-library/svelte";
import { tick } from "svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App.svelte";
import * as api from "../lib/api";
import { AllJobs } from "../lib/alljobs.svelte";
import { mascot } from "../lib/mascot.svelte";
import { router } from "../lib/router.svelte";
import { job, jobDetail, status } from "./fixtures";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual, getGpu: vi.fn(), getMascot: vi.fn(), getAllJobs: vi.fn(), getJob: vi.fn(),
    getSparks: vi.fn(),
  };
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
    vi.mocked(api.getSparks).mockReset().mockResolvedValue({});
    vi.stubGlobal("EventSource", FakeEventSource);
    history.pushState({}, "", "/");
    // The router is a singleton, and pushState alone doesn't tell it anything — without this a
    // filter set by an earlier test stays active and quietly filters the next test's rows away.
    dispatchEvent(new PopStateEvent("popstate"));
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

  it("fetches sparklines for the rows on screen and draws them in the table", async () => {
    vi.mocked(api.getSparks).mockResolvedValue({
      7: { key: "loss", points: [[1, 0.9], [2, 0.412]], latest: 0.412 },
    });
    const { container } = render(App);
    FakeEventSource.last!.emit({
      status: status(),
      jobs: [job({ id: 7, name: "sweep-lr", state: "running", start_time: 1000, run_time: 30 })],
    });

    await waitFor(() => expect(api.getSparks).toHaveBeenCalledWith([7]));
    const table = await waitFor(() => {
      const t = container.querySelector("table");
      expect(t).not.toBeNull();
      return t!;
    });
    expect(await within(table).findByText("loss")).toBeInTheDocument();
    expect(within(table).getByText("0.412")).toBeInTheDocument();
  });

  it("doesn't spin forever when live jobs repeatedly overlap the all-jobs cache while filtered (regression for effect_update_depth_exceeded)", async () => {
    history.pushState({}, "", "/?tag=sweep-a");
    dispatchEvent(new PopStateEvent("popstate"));
    // A fresh object per call, like a real snapshot parsed anew from JSON every tick, even though
    // nothing about the job actually changes — this is exactly what used to make AllJobs#overlay
    // reassign `jobs` on every tick and re-trigger App's own update effect without end.
    const steady = () => job({ id: 5, name: "steady", tags: ["sweep-a"], state: "running", start_time: 1000, run_time: 30 });
    vi.mocked(api.getAllJobs).mockResolvedValue([steady()]);
    // Spy on the real method (not a mock replacement) so we can count how many times App's effect
    // actually invokes it — a self-triggering loop would call this far more than once per tick.
    const updateSpy = vi.spyOn(AllJobs.prototype, "update");

    const { container } = render(App);
    const es = FakeEventSource.last!;
    es.emit({ status: status(), jobs: [steady()] });
    await tick();

    const table = await waitFor(() => {
      const t = container.querySelector("table");
      expect(t).not.toBeNull();
      return t!;
    });
    expect(within(table).getByText("steady")).toBeInTheDocument();

    const callsBefore = updateSpy.mock.calls.length;
    // Several more ticks with a content-identical (but reference-distinct) live job, each flushed
    // individually via `tick()` so Svelte's effects actually run between emissions — emitting all
    // ten synchronously (the old version of this test) let Svelte coalesce them into a single
    // flush and never actually exercised a repeated self-trigger.
    for (let i = 0; i < 10; i++) {
      es.emit({ status: status({ version: i + 2 }), jobs: [steady()] });
      await tick();
    }

    // Still just the one row, still responsive — no runaway loop, no crash. And `update` ran
    // exactly once per tick: if the effect that calls it ever again depends on its own write (the
    // bug `untrack` in App.svelte guards against), this would run away far past 10.
    expect(within(table).getAllByText("steady").length).toBe(1);
    expect(updateSpy.mock.calls.length).toBe(callsBefore + 10);
    updateSpy.mockRestore();
  });

  it("uses only the live snapshot for state counts once the filter is cleared, while the '+ tag' dropdown keeps the tags seen while it was active", async () => {
    history.pushState({}, "", "/?tag=sweep-a");
    dispatchEvent(new PopStateEvent("popstate"));
    const historic = job({ id: 900, name: "ancient", tags: ["ancient-tag"], state: "completed", end_time: 1 });
    vi.mocked(api.getAllJobs).mockResolvedValue([historic]);
    const liveJob = job({ id: 1, name: "now-running", tags: ["sweep-a"], state: "running", start_time: 1000 });

    render(App);
    const es = FakeEventSource.last!;
    es.emit({ status: status(), jobs: [liveJob] });
    await waitFor(() => expect(document.querySelector("table")).not.toBeNull());
    // Wait for the all-jobs fetch to land so the cache is populated (it must not fall out of
    // `filterKnown` again just because the filter below gets cleared).
    await waitFor(() => expect(api.getAllJobs).toHaveBeenCalled());
    await screen.findByLabelText("add tag filter");

    // Clear the filter.
    history.pushState({}, "", "/");
    dispatchEvent(new PopStateEvent("popstate"));
    await tick();

    // The state chip must reflect the live snapshot only (one running job) — not a frozen copy
    // of whatever the all-jobs cache last held.
    expect(screen.getByRole("button", { name: /running 1/ })).toBeInTheDocument();
    // The "+ tag" dropdown still offers a tag only ever seen in the (now-unused) cache, because it
    // reads the live snapshot *plus* the cache, not just whichever one `source` currently is.
    const addTag = screen.getByLabelText("add tag filter") as HTMLSelectElement;
    expect([...addTag.options].map((o) => o.value)).toContain("ancient-tag");
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
