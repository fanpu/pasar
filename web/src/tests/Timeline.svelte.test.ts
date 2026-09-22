import { render, screen, fireEvent, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Timeline from "../components/Timeline.svelte";
import * as api from "../lib/api";
import { GIB, job, NOW } from "./fixtures";

vi.mock("../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/api")>()),
  getJobsBetween: vi.fn(),
}));

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
  it("shows an empty state", () => {
    render(Timeline, { jobs: [], pool: 105 * GIB, now: NOW, onopen: () => {} });
    expect(screen.getByText("Nothing scheduled.")).toBeInTheDocument();
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

  it("pans back with Earlier, shows the range, and returns with now", async () => {
    render(Timeline, { jobs: [], pool: 105 * GIB, now: NOW, onopen: () => {} });
    expect(screen.queryByRole("button", { name: "now" })).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Earlier" }));
    // live 12:56 – 18:26, moved back 2h45
    expect(screen.getByText(/10:11 – 15:41/)).toBeInTheDocument();
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

  it("zoom buttons change the window length", async () => {
    render(Timeline, { jobs: [], pool: 105 * GIB, now: NOW, onopen: () => {} });
    await fireEvent.click(screen.getByRole("button", { name: "Zoom out" }));
    await fireEvent.click(screen.getByRole("button", { name: "Earlier" }));
    // 11h live window reaches 4h ahead (07:26 – 18:26), moved back 5h30
    expect(screen.getByText(/01:56 – 12:56/)).toBeInTheDocument();
  });
});
