import { render, screen, fireEvent, waitFor } from "@testing-library/svelte";
import { describe, expect, it, vi, beforeEach } from "vitest";
import JobPanel from "../components/JobPanel.svelte";
import * as api from "../lib/api";
import { ApiError } from "../lib/api";
import { attempt, job, jobDetail, NOW } from "./fixtures";
import type { JobDetail } from "../lib/types";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    getJob: vi.fn(),
    cancelJob: vi.fn(),
    setBid: vi.fn(),
    restartJob: vi.fn(),
  };
});

const noop = (): void => {};
const noopRestartWith = (_job: JobDetail): void => {};

function baseDetail(overrides: Partial<JobDetail> = {}): JobDetail {
  return jobDetail({ id: 42, name: "llama-sft", ...overrides });
}

describe("JobPanel", () => {
  beforeEach(() => {
    vi.mocked(api.getJob).mockReset();
    vi.mocked(api.cancelJob).mockReset();
    vi.mocked(api.setBid).mockReset();
    vi.mocked(api.restartJob).mockReset();
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  it("renders the name and pill", async () => {
    const liveJob = job({ id: 42, name: "llama-sft", state: "running", start_time: NOW - 600 });
    vi.mocked(api.getJob).mockResolvedValue(baseDetail({ state: "running", start_time: NOW - 600 }));
    const { container } = render(JobPanel, {
      id: 42, live: liveJob, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith,
    });
    expect(await screen.findByText("llama-sft")).toBeInTheDocument();
    expect(container.querySelector(".s-running")).not.toBeNull();
  });

  it("shows the estimated duration for a running job", async () => {
    const liveJob = job({
      id: 42, name: "llama-sft", state: "running", run_time: 1000, est_runtime: 12600, start_time: NOW - 1000,
    });
    vi.mocked(api.getJob).mockResolvedValue(baseDetail({ state: "running", run_time: 1000, est_runtime: 12600 }));
    render(JobPanel, { id: 42, live: liveJob, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith });
    expect(await screen.findByText("of ~3h30 estimated")).toBeInTheDocument();
  });

  it("shows bid and cancel actions for a queued job", async () => {
    const liveJob = job({ id: 42, name: "llama-sft", state: "queued", bid: 800 });
    vi.mocked(api.getJob).mockResolvedValue(baseDetail({ state: "queued", bid: 800 }));
    render(JobPanel, { id: 42, live: liveJob, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith });
    expect(await screen.findByText("★ bid 800 ✎")).toBeInTheDocument();
    expect(screen.getByText("cancel")).toBeInTheDocument();
  });

  it("shows a restart action for a finished job", async () => {
    const liveJob = job({ id: 42, name: "llama-sft", state: "completed", end_time: NOW - 60 });
    vi.mocked(api.getJob).mockResolvedValue(baseDetail({ state: "completed", end_time: NOW - 60 }));
    render(JobPanel, { id: 42, live: liveJob, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith });
    expect(await screen.findByText("↻ restart")).toBeInTheDocument();
  });

  it("cancels the job when confirm is accepted, and not when confirm is declined", async () => {
    const liveJob = job({ id: 42, name: "llama-sft", state: "queued", bid: 800 });
    vi.mocked(api.getJob).mockResolvedValue(baseDetail({ state: "queued", bid: 800 }));
    vi.mocked(api.cancelJob).mockResolvedValue(liveJob);
    vi.spyOn(window, "confirm").mockReturnValueOnce(true).mockReturnValueOnce(false);
    render(JobPanel, { id: 42, live: liveJob, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith });
    const cancelBtn = await screen.findByText("cancel");

    await fireEvent.click(cancelBtn);
    await waitFor(() => expect(api.cancelJob).toHaveBeenCalledWith(42));

    vi.mocked(api.cancelJob).mockClear();
    await fireEvent.click(cancelBtn);
    expect(api.cancelJob).not.toHaveBeenCalled();
  });

  it("shows the ApiError message when restart is rejected", async () => {
    const liveJob = job({ id: 42, name: "llama-sft", state: "completed", end_time: NOW - 60 });
    vi.mocked(api.getJob).mockResolvedValue(baseDetail({ state: "completed", end_time: NOW - 60 }));
    vi.mocked(api.restartJob).mockRejectedValue(new ApiError(409, "job 42 is running"));
    render(JobPanel, { id: 42, live: liveJob, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith });
    const restartBtn = await screen.findByText("↻ restart");

    await fireEvent.click(restartBtn);
    expect(await screen.findByText("job 42 is running")).toBeInTheDocument();
  });

  it("calls onclose on Escape", async () => {
    const onclose = vi.fn();
    const liveJob = job({ id: 42, name: "llama-sft", state: "queued" });
    vi.mocked(api.getJob).mockResolvedValue(baseDetail({ state: "queued" }));
    render(JobPanel, { id: 42, live: liveJob, now: NOW, grafanaUrl: null, onclose, onrestartwith: noopRestartWith });
    await screen.findByText("llama-sft");

    await fireEvent.keyDown(window, { key: "Escape" });
    expect(onclose).toHaveBeenCalled();
  });

  it("shows the failure reason and the last 8 log-tail lines for an OOM job", async () => {
    const lines = Array.from({ length: 10 }, (_, i) => `line${i + 1}`);
    const finalAttempt = attempt({
      n: 1, job_id: 42, end_kind: "failed", reason: "gpu_oom", log_tail: lines.join("\n"),
    });
    const liveJob = job({
      id: 42, name: "llama-sft", state: "failed", reason: "gpu_oom", summary: "CUDA out of memory",
      end_time: NOW - 60,
    });
    vi.mocked(api.getJob).mockResolvedValue(baseDetail({
      state: "failed", reason: "gpu_oom", summary: "CUDA out of memory", end_time: NOW - 60,
      attempts: [finalAttempt],
    }));
    const { container } = render(JobPanel, {
      id: 42, live: liveJob, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith,
    });

    await waitFor(() => expect(container.querySelector(".reason .t")).not.toBeNull());
    expect(container.querySelector(".reason .t")?.textContent).toBe("GPU out of memory");
    expect(container.querySelector(".tail")).not.toBeNull();
    const tailText = container.querySelector(".tail")!.textContent ?? "";
    expect(tailText).toContain("line10");
    expect(tailText).not.toMatch(/\bline1\b/);
    expect(tailText).not.toMatch(/\bline2\b/);
  });

  it("shows a not-found message when getJob 404s and the job isn't in the live snapshot", async () => {
    vi.mocked(api.getJob).mockRejectedValue(new ApiError(404, "not found"));
    render(JobPanel, { id: 42, live: null, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith });
    expect(await screen.findByText("No job #42.")).toBeInTheDocument();
  });

  it("saves a new bid from the dialog on Enter", async () => {
    const liveJob = job({ id: 42, name: "llama-sft", state: "queued", bid: 800 });
    vi.mocked(api.getJob).mockResolvedValue(baseDetail({ state: "queued", bid: 800 }));
    vi.mocked(api.setBid).mockResolvedValue(liveJob);
    render(JobPanel, { id: 42, live: liveJob, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith });
    const bidBtn = await screen.findByText("★ bid 800 ✎");

    await fireEvent.click(bidBtn);
    const input = await screen.findByRole("spinbutton", { name: "bid" });
    await fireEvent.input(input, { target: { value: "1500" } });
    await fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => expect(api.setBid).toHaveBeenCalledWith(42, 1500));
  });

  it("shows the attempts bar legend with lost time from wasted work and restart cost", async () => {
    const a1 = attempt({
      n: 1, job_id: 42, start_time: NOW - 3600, end_time: NOW - 3000, end_kind: "preempted", wasted_work: 360,
    });
    const a2 = attempt({ n: 2, job_id: 42, start_time: NOW - 2900, end_time: null, restart_cost: 300 });
    const liveJob = job({
      id: 42, name: "llama-sft", state: "running", start_time: NOW - 2900, attempts: 2, preemptions: 1,
    });
    vi.mocked(api.getJob).mockResolvedValue(baseDetail({
      state: "running", start_time: NOW - 2900, submit_time: NOW - 3700, attempts: [a1, a2], preemptions: 1,
    }));
    render(JobPanel, { id: 42, live: liveJob, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith });

    expect(await screen.findByText("lost (6m unsaved work + 5m restart)")).toBeInTheDocument();
  });
});
