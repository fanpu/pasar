import { render, screen } from "@testing-library/svelte";
import { describe, expect, it } from "vitest";
import Header from "../components/Header.svelte";
import Banner from "../components/Banner.svelte";
import Tiles from "../components/Tiles.svelte";
import Sparkline from "../components/Sparkline.svelte";
import Toast from "../components/Toast.svelte";
import JobChip from "../components/JobChip.svelte";
import StatePill from "../components/StatePill.svelte";
import { GIB, job, status } from "./fixtures";

describe("dashboard pieces", () => {
  it("header shows the mood", () => {
    render(Header, { mood: { state: "happy", say: "2 jobs cooking", sub: "next up: #48 at ~14:47", banner: null }, image: "/m.png", bounceKey: 0, onsubmit: () => {} });
    expect(screen.getByText("2 jobs cooking")).toBeInTheDocument();
    expect(screen.getByText("next up: #48 at ~14:47")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /submit/i })).toBeInTheDocument();
  });
  it("disconnect banner wins", () => {
    render(Banner, { banner: { tone: "bad", text: "boom" }, connected: false });
    expect(screen.getByText(/Lost connection to pasard/)).toBeInTheDocument();
    expect(screen.queryByText("boom")).toBeNull();
  });
  it("shows the given banner when connected", () => {
    render(Banner, { banner: { tone: "warn", text: "careful now" }, connected: true });
    expect(screen.getByText("careful now")).toBeInTheDocument();
  });
  it("renders nothing when connected with no banner", () => {
    const { container } = render(Banner, { banner: null, connected: true });
    expect(container.querySelector(".banner")).toBeNull();
  });
  it("pool tile numbers", () => {
    const s = status({ pool: 105 * GIB, reserved: 58 * GIB, free: 47 * GIB });
    render(Tiles, { status: s, jobs: [job({ id: 42, state: "running", limit: 37.4 * GIB })], gpu: null });
    expect(screen.getByText("47.0 GiB free")).toBeInTheDocument();
    expect(screen.getByText(/58\.0/)).toBeInTheDocument();
    expect(screen.getAllByText("–").length).toBe(3);
  });
  it("gpu tiles show rounded values and a sparkline", () => {
    const s = status({ pool: 105 * GIB, reserved: 0, free: 105 * GIB });
    render(Tiles, {
      status: s,
      jobs: [],
      gpu: { power_w: [[1, 60.6], [2, 61.4]], temp_c: [[1, 63.2], [2, 64.1]], util_pct: [[1, 92.5], [2, 93.2]] },
    });
    expect(screen.getByText("61")).toBeInTheDocument();
    expect(screen.getByText("64")).toBeInTheDocument();
    expect(screen.getByText("93")).toBeInTheDocument();
  });
  it("sparkline exposes a min-max summary", () => {
    render(Sparkline, { points: [[1, 10], [2, 20], [3, 15]], color: "#3b8fd9" });
    expect(screen.getByRole("img", { name: "10–20" })).toBeInTheDocument();
  });
  it("toast shows only while a message is set", () => {
    const { rerender } = render(Toast, { message: "#48 sweep-wd started!", image: "/m.png" });
    expect(screen.getByRole("status")).toHaveTextContent("#48 sweep-wd started!");
    rerender({ message: null, image: "/m.png" });
    expect(screen.getByRole("status")).toHaveTextContent("");
  });
  it("job chip shows the id", () => {
    render(JobChip, { id: 42 });
    expect(screen.getByText("42")).toBeInTheDocument();
  });
  it("pill text", () => {
    const { rerender } = render(StatePill, { job: job({ state: "queued", preemptions: 2 }) });
    expect(screen.getByText("queued · preempted ×2")).toBeInTheDocument();
    rerender({ job: job({ state: "failed", reason: "gpu_oom" }) });
    expect(screen.getByText("GPU out of memory")).toBeInTheDocument();
    rerender({ job: job({ state: "stopping", stop_requested: "preempt" }) });
    expect(screen.getByText("preempting")).toBeInTheDocument();
  });
});
