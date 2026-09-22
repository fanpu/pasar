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
});
