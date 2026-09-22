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
    getLog: vi.fn(),
    followLog: vi.fn(),
    getEvents: vi.fn(),
    getUsage: vi.fn(),
    getMetrics: vi.fn(),
    getGpu: vi.fn(),
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
    vi.mocked(api.getLog).mockReset().mockResolvedValue({ text: "", offset: 0 });
    vi.mocked(api.followLog).mockReset().mockReturnValue(() => {});
    vi.mocked(api.getEvents).mockReset().mockResolvedValue([]);
    vi.mocked(api.getUsage).mockReset().mockResolvedValue([]);
    vi.mocked(api.getMetrics).mockReset().mockResolvedValue([]);
    vi.mocked(api.getGpu).mockReset().mockResolvedValue({ power_w: [], temp_c: [], util_pct: [] });
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

  it("ignores a stale getJob response for a job the panel has since navigated away from", async () => {
    let resolveJob1!: (d: JobDetail) => void;
    const job1Promise = new Promise<JobDetail>((resolve) => {
      resolveJob1 = resolve;
    });
    const job1Detail = baseDetail({
      id: 1, name: "job-one", state: "failed", reason: "gpu_oom", summary: "oom1",
      attempts: [attempt({ job_id: 1, log_tail: "job1-log-line" })],
    });
    const job2Detail = baseDetail({
      id: 2, name: "job-two", state: "failed", reason: "gpu_oom", summary: "oom2",
      attempts: [attempt({ job_id: 2, log_tail: "job2-log-line" })],
    });
    vi.mocked(api.getJob).mockImplementation((requestedId: number) =>
      requestedId === 1 ? job1Promise : Promise.resolve(job2Detail),
    );

    const liveJob1 = job({ id: 1, name: "job-one", state: "failed", reason: "gpu_oom", summary: "oom1" });
    const liveJob2 = job({ id: 2, name: "job-two", state: "failed", reason: "gpu_oom", summary: "oom2" });
    const { rerender, container } = render(JobPanel, {
      id: 1, live: liveJob1, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith,
    });

    // Navigate to job 2 before job 1's getJob call resolves.
    await rerender({ id: 2, live: liveJob2 });
    await waitFor(() => expect(container.querySelector(".tail")?.textContent).toContain("job2-log-line"));

    // Job 1's stale response arrives late; it must not clobber job 2's detail.
    resolveJob1(job1Detail);
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(container.querySelector(".tail")?.textContent).toContain("job2-log-line");
    expect(container.querySelector(".tail")?.textContent).not.toContain("job1-log-line");
  });

  it("shows an inline error (and retries on the next snapshot tick) when getJob fails for a reason other than 404", async () => {
    vi.mocked(api.getJob).mockRejectedValueOnce(new ApiError(500, "boom"));
    vi.mocked(api.getJob).mockResolvedValueOnce(baseDetail({ state: "queued" }));
    const { rerender } = render(JobPanel, {
      id: 42, live: null, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith,
    });

    expect(await screen.findByText("Couldn't load job #42: boom")).toBeInTheDocument();

    // A later snapshot tick (a fresh `now`) should retry the load and succeed.
    await rerender({ now: NOW + 2 });

    expect(await screen.findByText("llama-sft")).toBeInTheDocument();
  });

  it("shows the memory chart in the metrics tab for a running job with usage history", async () => {
    const liveJob = job({ id: 42, name: "llama-sft", state: "running", start_time: NOW - 600, limit: 6 * 1024 ** 3 });
    vi.mocked(api.getJob).mockResolvedValue(baseDetail({ state: "running", start_time: NOW - 600 }));
    vi.mocked(api.getUsage).mockResolvedValue([[NOW - 60, 2 * 1024 ** 3], [NOW, 3 * 1024 ** 3]]);
    const { container } = render(JobPanel, {
      id: 42, live: liveJob, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith,
    });

    await waitFor(() => expect(api.getUsage).toHaveBeenCalledWith(42));
    await waitFor(() => expect(container.querySelector('[data-tab~="metrics"] svg[role="img"]')).not.toBeNull());
    const svg = container.querySelector('[data-tab~="metrics"] svg[role="img"]');
    expect(svg?.getAttribute("aria-label")).toMatch(/^memory: latest/);
  });

  it("shows 'No metrics for this job.' when there's no usage or GPU history", async () => {
    const liveJob = job({ id: 42, name: "llama-sft", state: "queued" });
    vi.mocked(api.getJob).mockResolvedValue(baseDetail({ state: "queued" }));
    render(JobPanel, { id: 42, live: liveJob, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith });

    expect(await screen.findByText("No metrics for this job.")).toBeInTheDocument();
  });

  it("shows the event timeline in the events tab", async () => {
    const liveJob = job({ id: 42, name: "llama-sft", state: "running", start_time: NOW - 600 });
    vi.mocked(api.getJob).mockResolvedValue(baseDetail({
      state: "running", start_time: NOW - 600, submit_time: NOW - 700,
      attempts: [attempt({ n: 1, job_id: 42, start_time: NOW - 600 })],
    }));
    vi.mocked(api.getEvents).mockResolvedValue([{ attempt: 1, ts: NOW - 100, kind: "checkpoint", step: 400, payload: {} }]);
    render(JobPanel, { id: 42, live: liveJob, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith });

    expect(await screen.findByText("checkpoint · step 400")).toBeInTheDocument();
  });

  it("shows 'No output yet.' in the logs tab when the log is empty", async () => {
    const liveJob = job({ id: 42, name: "llama-sft", state: "queued" });
    vi.mocked(api.getJob).mockResolvedValue(baseDetail({ state: "queued" }));
    render(JobPanel, { id: 42, live: liveJob, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith });

    expect(await screen.findByText("No output yet.")).toBeInTheDocument();
  });

  it("notes other running jobs sharing the GPU on the power/temperature charts", async () => {
    const liveJob = job({ id: 42, name: "llama-sft", state: "running", start_time: NOW - 600 });
    const other = job({ id: 7, name: "other", state: "running" });
    vi.mocked(api.getJob).mockResolvedValue(baseDetail({ state: "running", start_time: NOW - 600 }));
    vi.mocked(api.getGpu).mockResolvedValue({
      power_w: [[NOW - 60, 50], [NOW, 55]], temp_c: [[NOW - 60, 60], [NOW, 61]], util_pct: [],
    });
    render(JobPanel, {
      id: 42, live: liveJob, now: NOW, grafanaUrl: null, jobs: [liveJob, other], onclose: noop, onrestartwith: noopRestartWith,
    });

    const notes = await screen.findAllByText("GPU-wide · shared with #7");
    expect(notes.length).toBe(2); // power and temperature charts
  });

  it("re-fetches events after navigating away and back to the same job", async () => {
    const jobA = job({ id: 1, name: "job-a", state: "running", start_time: NOW - 600 });
    const jobB = job({ id: 2, name: "job-b", state: "running", start_time: NOW - 600 });
    vi.mocked(api.getJob).mockImplementation((jid: number) =>
      Promise.resolve(baseDetail({ id: jid, name: jid === 1 ? "job-a" : "job-b", state: "running", start_time: NOW - 600 })),
    );
    vi.mocked(api.getEvents).mockImplementation((jid: number) =>
      Promise.resolve([{ attempt: 1, ts: NOW - 50, kind: "note", step: null, payload: { text: `note-${jid}` } }]),
    );

    const { rerender } = render(JobPanel, {
      id: 1, live: jobA, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith,
    });
    expect(await screen.findByText("note-1")).toBeInTheDocument();

    await rerender({ id: 2, live: jobB });
    expect(await screen.findByText("note-2")).toBeInTheDocument();

    await rerender({ id: 1, live: jobA });
    expect(await screen.findByText("note-1")).toBeInTheDocument();
  });
});
