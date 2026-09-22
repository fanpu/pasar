import { render, screen, fireEvent } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";
import Timeline from "../components/Timeline.svelte";
import { GIB, job, NOW } from "./fixtures";

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
});
