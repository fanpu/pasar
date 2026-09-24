import { render, screen, fireEvent, waitFor, within } from "@testing-library/svelte";
import { describe, expect, it, vi, beforeEach } from "vitest";
import JobPanel from "../components/JobPanel.svelte";
import JobForm from "../components/JobForm.svelte";
import * as api from "../lib/api";
import { ApiError } from "../lib/api";
import { attempt, job, jobDetail, NOW } from "./fixtures";
import { cloudJobView, cloudPersist } from "./fixtures/cloud";
import type { JobDetail } from "../lib/types";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    getJob: vi.fn(),
    cancelJob: vi.fn(),
    setBid: vi.fn(),
    restartJob: vi.fn(),
    submitJob: vi.fn(),
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

  it("Escape closes only a JobForm modal opened over the panel, not the panel underneath", async () => {
    const onclose = vi.fn();
    const onFormClose = vi.fn();
    const liveJob = job({ id: 42, name: "llama-sft", state: "queued" });
    vi.mocked(api.getJob).mockResolvedValue(baseDetail({ state: "queued" }));
    render(JobPanel, { id: 42, live: liveJob, now: NOW, grafanaUrl: null, onclose, onrestartwith: noopRestartWith });
    await screen.findByText("llama-sft");
    render(JobForm, { mode: "submit", onclose: onFormClose, ondone: (): void => {} });
    await screen.findByText("Submit a job");

    await fireEvent.keyDown(window, { key: "Escape" });

    expect(onFormClose).toHaveBeenCalled();
    expect(onclose).not.toHaveBeenCalled();
  });

  it("builds the Grafana link with URLSearchParams so an existing query string isn't broken", async () => {
    const liveJob = job({
      id: 42, name: "llama-sft", state: "running", start_time: NOW - 600,
      spans: [[NOW - 600, null, null]],
    });
    vi.mocked(api.getJob).mockResolvedValue(baseDetail({ state: "running", start_time: NOW - 600 }));
    const { container } = render(JobPanel, {
      id: 42, live: liveJob, now: NOW, grafanaUrl: "https://grafana.example/d/abc?orgId=1",
      onclose: noop, onrestartwith: noopRestartWith,
    });

    const link = await waitFor(() => {
      const a = container.querySelector<HTMLAnchorElement>('a[href*="grafana.example"]');
      expect(a).not.toBeNull();
      return a!;
    });
    const url = new URL(link.href);
    expect(url.origin).toBe("https://grafana.example");
    expect(url.pathname).toBe("/d/abc");
    expect(url.searchParams.get("orgId")).toBe("1");
    expect(url.searchParams.get("from")).toBe(String(Math.round((NOW - 600) * 1000)));
    expect(url.searchParams.get("to")).toBe("now");
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

  it("renders tags as buttons that call onfilter with the tag", async () => {
    const onfilter = vi.fn();
    const liveJob = job({ id: 42, name: "llama-sft", state: "queued", tags: ["sweep-a", "big"] });
    vi.mocked(api.getJob).mockResolvedValue(baseDetail({ state: "queued", tags: ["sweep-a", "big"] }));
    render(JobPanel, {
      id: 42, live: liveJob, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith, onfilter,
    });
    const btn = await screen.findByRole("button", { name: "sweep-a" });
    await fireEvent.click(btn);
    expect(onfilter).toHaveBeenCalledWith("sweep-a");
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

    await waitFor(() => expect(api.setBid).toHaveBeenCalledWith(42, 1500, false));
  });

  it("can ask to preempt from the bid dialog", async () => {
    const liveJob = job({ id: 42, name: "llama-sft", state: "queued", bid: 800 });
    vi.mocked(api.getJob).mockResolvedValue(baseDetail({ state: "queued", bid: 800 }));
    vi.mocked(api.setBid).mockResolvedValue(liveJob);
    render(JobPanel, { id: 42, live: liveJob, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith });
    await fireEvent.click(await screen.findByText("★ bid 800 ✎"));
    await fireEvent.click(screen.getByLabelText("also stop lower-bid jobs to start now"));
    await fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.setBid).toHaveBeenCalledWith(42, 800, true));
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

  it("clears the memory usage chart once a running job is requeued (preempted or retried)", async () => {
    const liveJob = job({ id: 42, name: "llama-sft", state: "running", start_time: NOW - 600, limit: 6 * 1024 ** 3 });
    vi.mocked(api.getJob).mockResolvedValue(baseDetail({ state: "running", start_time: NOW - 600 }));
    vi.mocked(api.getUsage).mockResolvedValue([[NOW - 60, 2 * 1024 ** 3], [NOW, 3 * 1024 ** 3]]);
    const { container, rerender } = render(JobPanel, {
      id: 42, live: liveJob, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith,
    });
    await waitFor(() => expect(container.querySelector('[data-tab~="metrics"] svg[role="img"]')).not.toBeNull());

    const queuedJob = job({ id: 42, name: "llama-sft", state: "queued", attempts: 2 });
    await rerender({ live: queuedJob });

    await waitFor(() =>
      expect(container.querySelector('[data-tab~="metrics"] svg[role="img"]')).toBeNull(),
    );
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

  describe("cloud facts", () => {
    it("omits the Cloud block for a local job", async () => {
      const liveJob = job({ id: 42, name: "llama-sft", state: "running", start_time: NOW - 600 });
      vi.mocked(api.getJob).mockResolvedValue(baseDetail({ state: "running", start_time: NOW - 600 }));
      const { container } = render(JobPanel, {
        id: 42, live: liveJob, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith,
      });
      await screen.findByText("llama-sft");
      expect(container.querySelector(".cloudblock")).toBeNull();
    });

    it("shows target, GPU, phase and cost for a running cloud job", async () => {
      const liveJob = cloudJobView(
        { id: 261, name: "train-sft", state: "running", start_time: NOW - 600, run_time: 5000 },
        {
          target: "modal-a", gpu: "H100", phase: "running",
          estimated_cost: 5, max_cost: 7, user_capped: false,
          approved_seconds: 6750, full_seconds: 6750, job_cap: 10, job_spent: 3.2,
        },
      );
      vi.mocked(api.getJob).mockResolvedValue(jobDetail({
        id: 261, name: "train-sft", state: "running", start_time: NOW - 600, run_time: 5000, cloud: liveJob.cloud,
      }));
      const { container } = render(JobPanel, {
        id: 261, live: liveJob, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith,
      });
      await screen.findByText("train-sft");
      const block = within(container.querySelector(".cloudblock")!);
      expect(block.getByText("Cloud")).toBeInTheDocument();
      expect(block.getByText("modal-a")).toBeInTheDocument();
      expect(block.getByText("H100")).toBeInTheDocument();
      expect(block.getByText("running")).toBeInTheDocument();
      expect(block.getByText("$5.00")).toBeInTheDocument();
      expect(block.getByText("up to $7.00")).toBeInTheDocument();
      expect(block.getByText("1h52")).toBeInTheDocument(); // dur(6750)
      expect(block.getByText("$3.20")).toBeInTheDocument();
      expect(block.getByText("of $10.00")).toBeInTheDocument();
    });

    it("shows a needs-more-time fact only when the job's own pace has fallen behind", async () => {
      const liveJob = cloudJobView(
        { id: 262, name: "train-long", state: "running", start_time: NOW - 19000, run_time: 19000 },
        { approved_seconds: 18000, full_seconds: 18000, needs_more_time: 1800 },
      );
      vi.mocked(api.getJob).mockResolvedValue(jobDetail({
        id: 262, name: "train-long", state: "running", start_time: NOW - 19000, run_time: 19000, cloud: liveJob.cloud,
      }));
      const { container } = render(JobPanel, {
        id: 262, live: liveJob, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith,
      });
      await screen.findByText("train-long");
      const block = within(container.querySelector(".cloudblock")!);
      expect(block.getByText("needs more time")).toBeInTheDocument();
      expect(block.getByText("+30m")).toBeInTheDocument();
      // No approve/extend button here — that lives on the cloud card, not this read-only panel.
      expect(block.queryByRole("button")).toBeNull();
    });

    it("shows a Modal console link only when console_url is set", async () => {
      const withConsole = cloudJobView(
        { id: 263, name: "eval-batch", state: "running", start_time: NOW - 600 },
        { console_url: "https://modal.com/apps/placeholder/eval-batch" },
      );
      vi.mocked(api.getJob).mockResolvedValue(jobDetail({
        id: 263, name: "eval-batch", state: "running", start_time: NOW - 600, cloud: withConsole.cloud,
      }));
      const { container } = render(JobPanel, {
        id: 263, live: withConsole, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith,
      });
      await screen.findByText("eval-batch");
      const link = within(container.querySelector(".cloudblock")!).getByText("Modal ↗");
      expect(link.closest("a")).toHaveAttribute("href", "https://modal.com/apps/placeholder/eval-batch");
    });

    it("shows each persist outcome: pulled, still on target, nothing saved, and a pull error", async () => {
      const cases: [string, ReturnType<typeof cloudPersist>, string, string][] = [
        [
          "pulled",
          cloudPersist({ bytes: 512 * 1024 * 1024, pulled_to: "/home/agent-3/pasar-pulled/220" }),
          "pulled 0.5 GiB",
          "→ /home/agent-3/pasar-pulled/220",
        ],
        [
          "still-remote",
          cloudPersist({ sweeps_at: NOW + 6 * 86400 }),
          "at modal-a",
          "until Sep 27",
        ],
        ["nothing", cloudPersist(), "nothing saved", ""],
        ["errored", cloudPersist({ last_error: "network timeout" }), "pull failed", "network timeout"],
      ];
      for (const [id_, persist, expectV, expectS] of cases) {
        const liveJob = cloudJobView(
          { id: 270, name: `finished-${id_}`, state: "completed", end_time: NOW - 60 },
          { persist },
        );
        vi.mocked(api.getJob).mockResolvedValue(jobDetail({
          id: 270, name: `finished-${id_}`, state: "completed", end_time: NOW - 60, cloud: liveJob.cloud,
        }));
        const { container, unmount } = render(JobPanel, {
          id: 270, live: liveJob, now: NOW, grafanaUrl: null, onclose: noop, onrestartwith: noopRestartWith,
        });
        await screen.findByText(`finished-${id_}`);
        const block = within(container.querySelector(".cloudblock")!);
        expect(block.getByText(expectV)).toBeInTheDocument();
        if (expectS) expect(block.getByText(expectS)).toBeInTheDocument();
        unmount();
      }
    });
  });
});
