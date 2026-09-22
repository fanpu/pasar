import { fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import TagSummary from "../components/TagSummary.svelte";
import * as api from "../lib/api";
import { ApiError } from "../lib/api";
import { job } from "./fixtures";
import type { JobView } from "../lib/types";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, cancelJob: vi.fn(), restartJob: vi.fn() };
});

describe("TagSummary", () => {
  beforeEach(() => {
    vi.mocked(api.cancelJob).mockReset();
    vi.mocked(api.restartJob).mockReset();
  });

  // 1 running, 2 queued (est. 200s, 300s), 2 completed (run 400s/est 380s, run 500s/est 420s), 1 failed.
  const jobs: JobView[] = [
    job({ id: 1, state: "running", run_time: 100 }),
    job({ id: 2, state: "queued", est_runtime: 200 }),
    job({ id: 3, state: "queued", est_runtime: 300 }),
    job({ id: 4, state: "completed", run_time: 400, est_runtime: 380 }),
    job({ id: 5, state: "completed", run_time: 500, est_runtime: 420 }),
    job({ id: 6, state: "failed", run_time: 50 }),
  ];

  it("renders the fact boxes computed from tagSummary", () => {
    render(TagSummary, { tag: "demo", jobs });
    expect(screen.getByText("6")).toBeInTheDocument();
    expect(screen.getByText("1 running · 2 queued")).toBeInTheDocument();
    expect(screen.getByText("2 / 3")).toBeInTheDocument();
    expect(screen.getByText("1 failed")).toBeInTheDocument();
    expect(screen.getByText("17m")).toBeInTheDocument();
    expect(screen.getByText("~8m still queued")).toBeInTheDocument();
    expect(screen.getByText("7m")).toBeInTheDocument();
    expect(screen.getByText("estimates said 6m ✓")).toBeInTheDocument();
  });

  it("shows the cancel and retry buttons with their counts", () => {
    render(TagSummary, { tag: "demo", jobs });
    expect(screen.getByText("cancel 2 queued")).toBeInTheDocument();
    expect(screen.getByText("retry 1 failed")).toBeInTheDocument();
  });

  it("hides the buttons when there's nothing queued or failed", () => {
    render(TagSummary, { tag: "demo", jobs: [job({ id: 1, state: "completed", run_time: 400 })] });
    expect(screen.queryByText(/cancel \d+ queued/)).not.toBeInTheDocument();
    expect(screen.queryByText(/retry \d+ failed/)).not.toBeInTheDocument();
  });

  it("shows '–' for success rate and typical run when nothing has finished yet", () => {
    render(TagSummary, {
      tag: "demo",
      jobs: [job({ id: 1, state: "running", run_time: 10 }), job({ id: 2, state: "queued", est_runtime: 20 })],
    });
    expect(screen.getAllByText("–")).toHaveLength(2);
  });

  it("confirms and cancels queued jobs sequentially, one call per job", async () => {
    let resolveFirst!: (v: JobView) => void;
    const first = new Promise<JobView>((res) => { resolveFirst = res; });
    vi.mocked(api.cancelJob).mockImplementationOnce(() => first);
    vi.mocked(api.cancelJob).mockImplementationOnce(async (id: number) => job({ id }));

    render(TagSummary, { tag: "demo", jobs });
    await fireEvent.click(screen.getByText("cancel 2 queued"));
    expect(await screen.findByText("Cancel 2 queued demo jobs?")).toBeInTheDocument();

    const confirmBtn = screen.getByText("Cancel 2 jobs");
    await fireEvent.click(confirmBtn);

    // Only the first job's call should have gone out until it resolves — proves the calls run
    // sequentially rather than all at once.
    expect(api.cancelJob).toHaveBeenCalledTimes(1);
    expect(api.cancelJob).toHaveBeenNthCalledWith(1, 2);
    expect(confirmBtn).toBeDisabled();

    resolveFirst(job({ id: 2 }));
    await waitFor(() => expect(api.cancelJob).toHaveBeenCalledTimes(2));
    expect(api.cancelJob).toHaveBeenNthCalledWith(2, 3);

    await waitFor(() => expect(screen.getByText("cancelled 2")).toBeInTheDocument());
  });

  it("reports a failed job's error inline and keeps going with the rest", async () => {
    const withTwoFailed = [...jobs, job({ id: 7, state: "failed", run_time: 5 })];
    vi.mocked(api.restartJob)
      .mockRejectedValueOnce(new ApiError(409, "job 6's files are gone … resubmit it instead"))
      .mockResolvedValueOnce(job({ id: 7 }));

    render(TagSummary, { tag: "demo", jobs: withTwoFailed });
    await fireEvent.click(screen.getByText("retry 2 failed"));
    expect(await screen.findByText("Retry 2 failed demo jobs?")).toBeInTheDocument();
    await fireEvent.click(screen.getByText("Retry 2 jobs"));

    await waitFor(() =>
      expect(
        screen.getByText("retried 1 · 1 couldn't be: job 6's files are gone … resubmit it instead"),
      ).toBeInTheDocument(),
    );
    expect(api.restartJob).toHaveBeenCalledTimes(2);
    expect(api.restartJob).toHaveBeenNthCalledWith(1, 6, {});
    expect(api.restartJob).toHaveBeenNthCalledWith(2, 7, {});
  });

  it("uses singular wording in the confirm modal for a single job", async () => {
    render(TagSummary, {
      tag: "demo",
      jobs: [job({ id: 1, state: "failed", run_time: 5 })],
    });
    await fireEvent.click(screen.getByText("retry 1 failed"));
    expect(await screen.findByText("Retry 1 failed demo job?")).toBeInTheDocument();
  });
});
