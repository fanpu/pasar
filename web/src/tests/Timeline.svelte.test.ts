import { render, screen, fireEvent, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Timeline from "../components/Timeline.svelte";
import * as api from "../lib/api";
import { jobColor } from "../lib/colors";
import { GIB, job, NOW } from "./fixtures";
import { cloudJob } from "./fixtures/cloud";

vi.mock("../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/api")>()),
  getJobsBetween: vi.fn(),
}));

// jsdom has no PointerEvent; a MouseEvent carrying the pointer fields is enough here.
if (!("PointerEvent" in globalThis)) {
  class PointerEvent extends MouseEvent {
    pointerId: number;
    pointerType: string;
    constructor(type: string, init: PointerEventInit = {}) {
      super(type, init);
      this.pointerId = init.pointerId ?? 0;
      this.pointerType = init.pointerType ?? "mouse";
    }
  }
  vi.stubGlobal("PointerEvent", PointerEvent);
}

beforeEach(() => {
  vi.mocked(api.getJobsBetween).mockReset().mockResolvedValue([]);
});

describe("Timeline", () => {
  it("renders blocks as buttons that open the job", async () => {
    const onopen = vi.fn();
    render(Timeline, { jobs: [job({ id: 42, name: "llama", state: "running", limit: 30 * GIB, spans: [[NOW - 600, null, null]], projected: [[NOW - 600, NOW + 600]] })], pool: 105 * GIB, now: NOW, onopen });
    const b = screen.getByRole("button", { name: /#42 llama/ });
    await fireEvent.click(b);
    await fireEvent.keyDown(b, { key: "Enter" });
    expect(onopen).toHaveBeenCalledTimes(2);
    expect(onopen).toHaveBeenCalledWith(42);
  });
  it("leaves cloud jobs off the chart: they hold none of this machine's memory", () => {
    const spans: [number, number | null, null][] = [[NOW - 600, null, null]];
    const local = job({ id: 42, name: "llama", state: "running", limit: 30 * GIB, spans, projected: [[NOW - 600, NOW + 600]] });
    const cloud = job({ id: 43, name: "far-away", state: "running", limit: 105 * GIB, spans, projected: [[NOW - 600, NOW + 600]], cloud: cloudJob() });
    render(Timeline, { jobs: [local, cloud], pool: 105 * GIB, now: NOW, onopen: () => {} });
    expect(screen.getByRole("button", { name: /#42 llama/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /#43 far-away/ })).toBeNull();
  });
  it("shows an empty state", () => {
    render(Timeline, { jobs: [], pool: 105 * GIB, now: NOW, onopen: () => {} });
    expect(screen.getByText("Nothing scheduled.")).toBeInTheDocument();
  });

  it("fades blocks the given dim predicate matches, and leaves the rest alone", () => {
    const kept = job({ id: 42, name: "keep", state: "running", spans: [[NOW - 600, null, null]], projected: [[NOW - 600, NOW + 600]] });
    const faded = job({ id: 43, name: "fade", state: "running", spans: [[NOW - 600, null, null]], projected: [[NOW - 600, NOW + 600]] });
    render(Timeline, { jobs: [kept, faded], pool: 105 * GIB, now: NOW, onopen: () => {}, dim: (j) => j.id === 43 });
    expect(screen.getByRole("button", { name: /#42 keep/ })).not.toHaveAttribute("style");
    expect(screen.getByRole("button", { name: /#43 fade/ }).getAttribute("style")).toContain("opacity: 0.25");
  });

  it("clips a block's label so long names can't spill into neighbouring blocks", () => {
    const { container } = render(Timeline, {
      jobs: [job({ id: 42, name: "llama-sft-lr3e-5-a-very-long-experiment-name", state: "running", limit: 30 * GIB, spans: [[NOW - 600, null, null]], projected: [[NOW - 600, NOW + 600]] })],
      pool: 105 * GIB,
      now: NOW,
      onopen: () => {},
    });
    const text = container.querySelector("text.blklabel");
    expect(text).not.toBeNull();
    const clipPath = text!.getAttribute("clip-path");
    expect(clipPath).toMatch(/^url\(#.+\)$/);
    const id = clipPath!.slice("url(#".length, -1);
    expect(container.querySelector(`clipPath#${id}`)).not.toBeNull();
  });

  it("activates a block with the Space key too", async () => {
    const onopen = vi.fn();
    render(Timeline, { jobs: [job({ id: 42, name: "llama", state: "running", limit: 30 * GIB, spans: [[NOW - 600, null, null]], projected: [[NOW - 600, NOW + 600]] })], pool: 105 * GIB, now: NOW, onopen });
    const b = screen.getByRole("button", { name: /#42 llama/ });
    await fireEvent.keyDown(b, { key: " " });
    expect(onopen).toHaveBeenCalledWith(42);
  });

  it("pans back, shows the range, and returns with now", async () => {
    render(Timeline, { jobs: [], pool: 105 * GIB, now: NOW, onopen: () => {} });
    expect(screen.queryByRole("button", { name: "now" })).toBeNull();
    await fireEvent.keyDown(window, { key: "a" });
    await fireEvent.keyUp(window, { key: "a" });
    expect(screen.getByText(/12:47 – 18:17/)).toBeInTheDocument();
    expect(screen.getByText("Nothing ran in this window.")).toBeInTheDocument();
    await fireEvent.click(screen.getByRole("button", { name: "now" }));
    expect(screen.getByText("memory over time")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "now" })).toBeNull();
  });

  it("fetches older jobs when the window reaches past the live snapshot", async () => {
    const old = job({ id: 7, name: "old-sweep", state: "completed", spans: [[NOW - 5 * 86400, NOW - 5 * 86400 + 7200, "completed"]] });
    vi.mocked(api.getJobsBetween).mockResolvedValue([old]);
    render(Timeline, { jobs: [], pool: 105 * GIB, now: NOW, onopen: () => {} });
    await fireEvent.click(screen.getByRole("button", { name: "1d" }));
    expect(api.getJobsBetween).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByRole("button", { name: "1w" }));
    expect(screen.getByRole("button", { name: "1w" })).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(api.getJobsBetween).toHaveBeenCalledTimes(1));
    const [since, until] = vi.mocked(api.getJobsBetween).mock.calls[0];
    expect(since).toBeLessThanOrEqual(NOW - 6 * 86400);
    expect(until).toBeGreaterThanOrEqual(NOW);
    expect(await screen.findByRole("button", { name: /#7 old-sweep, ran Sep 16/ })).toBeInTheDocument();
  });

  it("prefers the live copy of a job over its fetched one", async () => {
    const live = job({ id: 9, name: "fresh", state: "running", spans: [[NOW - 600, null, null]], projected: [[NOW - 600, NOW + 600]] });
    vi.mocked(api.getJobsBetween).mockResolvedValue([{ ...live, name: "stale" }]);
    render(Timeline, { jobs: [live], pool: 105 * GIB, now: NOW, onopen: () => {} });
    await fireEvent.click(screen.getByRole("button", { name: "1w" }));
    await waitFor(() => expect(api.getJobsBetween).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: /#9 fresh/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /stale/ })).toBeNull();
  });

  it("A and D move the window; W/S zoom", async () => {
    render(Timeline, { jobs: [], pool: 105 * GIB, now: NOW, onopen: () => {} });
    await fireEvent.keyDown(window, { key: "a" });
    await fireEvent.keyUp(window, { key: "a" });
    // a tap moves 0.8 × 5.5h / 30 = 8.8 min earlier: 12:56 → 12:47
    expect(screen.getByText(/12:47 – 18:17/)).toBeInTheDocument();
    await fireEvent.keyDown(window, { key: "W" });
    await fireEvent.keyUp(window, { key: "W" });
    expect(screen.queryByText(/12:47 – 18:17/)).toBeNull();
  });

  it("zooms with a two-finger pinch", async () => {
    const { container } = render(Timeline, { jobs: [], pool: 105 * GIB, now: NOW, onopen: () => {} });
    const svg = container.querySelector("svg.tl")!;
    const touch = (id: number, clientX: number) => ({ pointerId: id, pointerType: "touch", clientX, button: 0 });
    await fireEvent.pointerDown(svg, touch(1, 244));
    await fireEvent.pointerDown(svg, touch(2, 544));
    await fireEvent.pointerMove(svg, touch(1, 94));
    await fireEvent.pointerMove(svg, touch(2, 694));
    await fireEvent.pointerUp(svg, touch(1, 94));
    await fireEvent.pointerUp(svg, touch(2, 694));
    // spreading the fingers to twice as far apart halves the 5.5h window around their midpoint
    const [a, b] = screen.getByText(/\d\d:\d\d – \d\d:\d\d/).textContent!.match(/\d\d:\d\d/g)!
      .map((m) => Number(m.slice(0, 2)) * 60 + Number(m.slice(3)));
    expect(b - a).toBeGreaterThanOrEqual(164);
    expect(b - a).toBeLessThanOrEqual(166);
  });

  it("gives a finished block a muted version of the job's colour instead of the old gray", () => {
    render(Timeline, {
      jobs: [job({ id: 42, name: "llama", state: "completed", spans: [[NOW - 3600, NOW - 1800, "completed"]] })],
      pool: 105 * GIB,
      now: NOW,
      onopen: () => {},
    });
    const block = screen.getByRole("button", { name: /#42 llama, ran/ });
    const rect = block.querySelector("rect")!;
    const c = jobColor({ id: 42, tags: [] });
    expect(rect.getAttribute("fill")).toBe(`${c}17`);
    expect(rect.getAttribute("stroke")).toBe(`${c}66`);
    expect(rect.getAttribute("fill")).not.toBe("#f2edf0");
    expect(rect.getAttribute("stroke")).not.toBe("#e4d9df");
    // no left stripe on finished blocks, unlike running/projected ones (the clipPath's own
    // rect doesn't count — it's nested a level deeper)
    expect(block.querySelectorAll(":scope > rect").length).toBe(1);
  });

  it("ignores the keys while typing or while a dialog is open", async () => {
    render(Timeline, { jobs: [], pool: 105 * GIB, now: NOW, onopen: () => {} });
    const input = document.createElement("input");
    document.body.appendChild(input);
    await fireEvent.keyDown(input, { key: "d" });
    const dialog = document.createElement("div");
    dialog.setAttribute("aria-modal", "true");
    document.body.appendChild(dialog);
    await fireEvent.keyDown(window, { key: "d" });
    await fireEvent.keyDown(window, { key: "d", ctrlKey: true });
    expect(screen.getByText("memory over time")).toBeInTheDocument();
    input.remove();
    dialog.remove();
  });
});
