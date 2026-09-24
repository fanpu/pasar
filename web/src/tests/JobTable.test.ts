import { render, screen, fireEvent, waitFor, within } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";
import JobTable from "../components/JobTable.svelte";
import * as api from "../lib/api";
import { EMPTY_FILTER, type Filter } from "../lib/jobfilter";
import { GIB, job, NOW } from "./fixtures";
import { awaitingFresh, awaitingReapproval, cloudJobView } from "./fixtures/cloud";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, cancelJob: vi.fn(), restartJob: vi.fn() };
});

describe("JobTable", () => {
  it("shows group headings in order and omits empty groups", () => {
    const { container } = render(JobTable, {
      jobs: [
        job({ id: 1, state: "running", start_time: NOW - 600 }),
        job({ id: 2, state: "completed", end_time: NOW - 100 }),
      ],
      pool: 105 * GIB,
      now: NOW,
      selected: null,
      onopen: () => {},
    });
    const headings = Array.from(container.querySelectorAll("tr.group td")).map((td) => td.textContent);
    expect(headings).toEqual(["Running", "Recently finished"]);
    expect(screen.queryByText("Queued")).toBeNull();
  });

  it("renders the header row, group heading rows and one row per job", () => {
    const { container } = render(JobTable, {
      jobs: [
        job({ id: 1, state: "running", start_time: NOW - 600 }),
        job({ id: 2, state: "running", start_time: NOW - 300 }),
        job({ id: 3, state: "queued" }),
        job({ id: 4, state: "completed", end_time: NOW - 100 }),
      ],
      pool: 105 * GIB,
      now: NOW,
      selected: null,
      onopen: () => {},
    });
    const table = container.querySelector("table")!;
    // 1 header + 3 group headings + 4 job rows
    expect(within(table).getAllByRole("row").length).toBe(8);
  });

  it("shows the memory meter for a running shared job", () => {
    render(JobTable, {
      jobs: [job({ id: 5, state: "running", mode: "shared", usage: 31.2 * GIB, limit: 37.4 * GIB, start_time: NOW - 600 })],
      pool: 105 * GIB,
      now: NOW,
      selected: null,
      onopen: () => {},
    });
    expect(screen.getByText("31.2 / 37.4 GiB")).toBeInTheDocument();
  });

  it("marks the memory bar amber when the job is over its limit", () => {
    const { container } = render(JobTable, {
      jobs: [job({ id: 6, state: "running", mode: "shared", usage: 40 * GIB, limit: 37.4 * GIB, over_limit: true, start_time: NOW - 600 })],
      pool: 105 * GIB,
      now: NOW,
      selected: null,
      onopen: () => {},
    });
    expect(container.querySelector(".bar i.over")).not.toBeNull();
  });

  it("shows a projected start time for a queued job", () => {
    render(JobTable, {
      jobs: [job({ id: 7, state: "queued", projected: [[NOW + 21 * 60, NOW + 30 * 60]] })],
      pool: 105 * GIB,
      now: NOW,
      selected: null,
      onopen: () => {},
    });
    expect(screen.getByText("starts ~14:47")).toBeInTheDocument();
  });

  it("shows the failed sub-line with the reason label and summary", () => {
    const { container } = render(JobTable, {
      jobs: [job({ id: 8, state: "failed", reason: "gpu_oom", summary: "CUDA out of memory. Tried to allocate 2.00 GiB", end_time: NOW - 30 })],
      pool: 105 * GIB,
      now: NOW,
      selected: null,
      onopen: () => {},
    });
    // The table and the (CSS-hidden in jsdom) phone cards both render this text, so scope to the table.
    const table = container.querySelector("table")!;
    expect(within(table).getByText("GPU out of memory: CUDA out of memory. Tried to allocate 2.00 GiB")).toBeInTheDocument();
  });

  it("highlights a high bid", () => {
    const { container } = render(JobTable, {
      jobs: [job({ id: 9, state: "queued", bid: 1500 })],
      pool: 105 * GIB,
      now: NOW,
      selected: null,
      onopen: () => {},
    });
    const table = container.querySelector("table")!;
    const bid = within(table).getByText("★ 1500");
    expect(bid).toHaveClass("hi");
    expect(bid.closest("tr")).toBe(table.querySelector("tr.row"));
  });

  it("shows the metric name, its latest value and a sparkline on the row", () => {
    const { container } = render(JobTable, {
      jobs: [job({ id: 10, state: "running", start_time: NOW - 600 })],
      sparks: { 10: { key: "loss", points: [[1, 0.9], [2, 0.412]] as [number, number][], latest: 0.412 } },
      pool: 105 * GIB,
      now: NOW,
      selected: null,
      onopen: () => {},
    });
    const table = container.querySelector("table")!;
    expect(within(table).getByText("loss")).toBeInTheDocument();
    expect(within(table).getByText("0.412")).toBeInTheDocument();
    expect(within(table).getByRole("img", { name: "0.412\u20130.900" })).toBeInTheDocument();
  });

  it("leaves the metric cell empty for a job that never reported one", () => {
    const { container } = render(JobTable, {
      jobs: [job({ id: 11, state: "running", start_time: NOW - 600 })],
      sparks: {},
      pool: 105 * GIB,
      now: NOW,
      selected: null,
      onopen: () => {},
    });
    const table = container.querySelector("table")!;
    expect(within(table).queryByRole("img")).toBeNull();
    expect(table.querySelector("td.metric")!.textContent!.trim()).toBe("");
  });

  it("puts the metric where lost time used to be, which the detail panel still shows", () => {
    const { container } = render(JobTable, {
      jobs: [job({ id: 12, state: "completed", end_time: NOW - 60, lost: { preemption: 660, failure: 0, known: true } })],
      pool: 105 * GIB,
      now: NOW,
      selected: null,
      onopen: () => {},
    });
    const headers = Array.from(container.querySelectorAll("thead th")).map((th) => th.textContent);
    expect(headers).toEqual(["job", "state", "bid", "memory", "time", "metric", "by"]);
    expect(screen.queryByText("11m")).toBeNull();
  });

  it("carries the sparkline onto the phone cards too", () => {
    const { container } = render(JobTable, {
      jobs: [job({ id: 13, state: "running", start_time: NOW - 600 })],
      sparks: { 13: { key: "test_acc", points: [[1, 0.2], [2, 0.8]] as [number, number][], latest: 0.8 } },
      pool: 105 * GIB,
      now: NOW,
      selected: null,
      onopen: () => {},
    });
    const cards = container.querySelector(".cards")!;
    expect(within(cards as HTMLElement).getByText("test_acc")).toBeInTheDocument();
    expect(within(cards as HTMLElement).getByText("0.800")).toBeInTheDocument();
  });

  it("clicking a row and pressing Enter both call onopen with the job id", async () => {
    const onopen = vi.fn();
    const { container } = render(JobTable, {
      jobs: [job({ id: 12, state: "running", start_time: NOW - 600 })],
      pool: 105 * GIB,
      now: NOW,
      selected: null,
      onopen,
    });
    const row = container.querySelector("tr.row")!;
    await fireEvent.click(row);
    await fireEvent.keyDown(row, { key: "Enter" });
    expect(onopen).toHaveBeenCalledTimes(2);
    expect(onopen).toHaveBeenCalledWith(12);
  });

  it("activates a row and a phone card with the Space key too", async () => {
    const onopen = vi.fn();
    const { container } = render(JobTable, {
      jobs: [job({ id: 12, state: "running", start_time: NOW - 600 })],
      pool: 105 * GIB,
      now: NOW,
      selected: null,
      onopen,
    });
    const row = container.querySelector("tr.row")!;
    await fireEvent.keyDown(row, { key: " " });
    expect(onopen).toHaveBeenCalledWith(12);

    const card = container.querySelector(".card")!;
    await fireEvent.keyDown(card, { key: " " });
    expect(onopen).toHaveBeenCalledTimes(2);
  });

  it("shows an empty state with no jobs", () => {
    render(JobTable, { jobs: [], pool: 105 * GIB, now: NOW, selected: null, onopen: () => {} });
    // getByText's default normalizer collapses whitespace, so match against the collapsed form;
    // the component's markup still renders the double space per the design spec.
    expect(screen.getByText("No jobs yet. Try pasar submit --time 10m -- python train.py")).toBeInTheDocument();
  });

  it("shows a running job's expected time from progress instead of its estimate", () => {
    const j = job({ id: 3, state: "running", run_time: 720, est_runtime: 600, expected_runtime: 3600, eta_source: "progress" });
    render(JobTable, { jobs: [j], pool: 100, now: NOW, selected: null, onopen: () => {} });
    const cell = screen.getByTitle("from progress reports (estimated 10m)");
    expect(cell).toHaveTextContent("/ ~1h00");
  });

  it("shows a job's tags as pills, and only when it has tags", () => {
    const { container } = render(JobTable, {
      jobs: [
        job({ id: 20, state: "running", start_time: NOW - 600, tags: ["sweep-a", "big"] }),
        job({ id: 21, state: "running", start_time: NOW - 300 }),
      ],
      pool: 105 * GIB,
      now: NOW,
      selected: null,
      onopen: () => {},
    });
    const tagged = container.querySelectorAll<HTMLElement>("tr.row")[0];
    const untagged = container.querySelectorAll<HTMLElement>("tr.row")[1];
    expect(within(tagged).getByText("sweep-a")).toBeInTheDocument();
    expect(within(tagged).getByText("big")).toBeInTheDocument();
    expect(untagged.querySelector(".jtags")).toBeNull();
  });

  it("clicking a tag pill filters by that tag instead of opening the job", async () => {
    const onopen = vi.fn();
    const onfilter = vi.fn();
    const { container } = render(JobTable, {
      jobs: [job({ id: 30, state: "running", start_time: NOW - 600, tags: ["sweep-a"] })],
      pool: 105 * GIB, now: NOW, selected: null, onopen, onfilter,
    });
    const table = container.querySelector("table")!;
    await fireEvent.click(within(table).getByText("sweep-a"));
    expect(onfilter).toHaveBeenCalledWith({ ...EMPTY_FILTER, tags: ["sweep-a"] }, false);
    expect(onopen).not.toHaveBeenCalled();
  });

  it("clicking the submitter filters by it instead of opening the job", async () => {
    const onopen = vi.fn();
    const onfilter = vi.fn();
    const { container } = render(JobTable, {
      jobs: [job({ id: 31, state: "completed", end_time: NOW - 60, submitter: "opus-triage" })],
      pool: 105 * GIB, now: NOW, selected: null, onopen, onfilter,
    });
    const table = container.querySelector("table")!;
    await fireEvent.click(within(table).getByText("opus-triage"));
    expect(onfilter).toHaveBeenCalledWith({ ...EMPTY_FILTER, by: ["opus-triage"] }, false);
    expect(onopen).not.toHaveBeenCalled();
  });

  it("clicking a header sorts by it, and clicking it again flips the direction", async () => {
    const onfilter = vi.fn();
    const { rerender } = render(JobTable, {
      jobs: [job({ id: 1, bid: 500 }), job({ id: 2, bid: 900 })],
      pool: 105 * GIB, now: NOW, selected: null, onopen: () => {}, onfilter, filter: EMPTY_FILTER,
    });
    await fireEvent.click(screen.getByRole("button", { name: "bid" }));
    expect(onfilter).toHaveBeenCalledWith({ ...EMPTY_FILTER, sort: { key: "bid", desc: true } }, false);

    onfilter.mockClear();
    const sortedByBid: Filter = { ...EMPTY_FILTER, sort: { key: "bid", desc: true } };
    await rerender({ filter: sortedByBid });
    await fireEvent.click(screen.getByRole("button", { name: /bid/ }));
    expect(onfilter).toHaveBeenCalledWith({ ...sortedByBid, sort: { key: "bid", desc: false } }, false);
  });

  it("first click on the job (id) header from the unsorted view sorts newest-first (desc), not asc", async () => {
    // effectiveSort(EMPTY_FILTER) falls back to {key:"id",desc:true} even though filter.sort is
    // actually null; sortFor must compare against the real (nullable) filter.sort, or this first
    // click gets misread as "already sorted by id, so flip" and sorts oldest-first instead.
    const onfilter = vi.fn();
    render(JobTable, {
      jobs: [job({ id: 1 }), job({ id: 2 })],
      pool: 105 * GIB, now: NOW, selected: null, onopen: () => {}, onfilter, filter: EMPTY_FILTER,
    });
    await fireEvent.click(screen.getByRole("button", { name: "job" }));
    expect(onfilter).toHaveBeenCalledWith({ ...EMPTY_FILTER, sort: { key: "id", desc: true } }, false);
  });

  it("shows a flat, filtered list with a match count when a filter is active", () => {
    render(JobTable, {
      jobs: [job({ id: 1, state: "completed", end_time: NOW - 60 })],
      pool: 105 * GIB, now: NOW, selected: null, onopen: () => {},
      filter: { ...EMPTY_FILTER, tags: ["x"] }, total: 42,
    });
    expect(screen.getByText("1 match · of 42")).toBeInTheDocument();
    expect(screen.queryByText("Recently finished")).toBeNull();
  });

  it("shows 'No jobs match.' with a clear button when the filter matches nothing", async () => {
    const onfilter = vi.fn();
    const { container } = render(JobTable, {
      jobs: [], pool: 105 * GIB, now: NOW, selected: null, onopen: () => {},
      onfilter, filter: { ...EMPTY_FILTER, q: "nope" }, total: 5,
    });
    const empty = container.querySelector<HTMLElement>(".empty")!;
    expect(within(empty).getByText("No jobs match.")).toBeInTheDocument();
    await fireEvent.click(within(empty).getByText("clear"));
    expect(onfilter).toHaveBeenCalledWith(EMPTY_FILTER, false);
  });

  it("offers both directions for every key on the phone sort select, with a neutral placeholder when nothing is chosen", () => {
    render(JobTable, {
      jobs: [job({ id: 1 }), job({ id: 2 })],
      pool: 105 * GIB, now: NOW, selected: null, onopen: () => {}, filter: EMPTY_FILTER,
    });
    const select = screen.getByLabelText("sort") as HTMLSelectElement;
    // Nothing chosen yet: the placeholder is selected, not "newest first" (effectiveSort's fallback).
    expect(select.value).toBe("");
    const labels = [...select.options].map((o) => o.textContent);
    expect(labels).toEqual(expect.arrayContaining([
      "sort…", "newest first", "oldest first", "bid high → low", "bid low → high",
    ]));
  });

  it("picking a direction from the phone sort select applies that sort, even from the unsorted (grouped) view", async () => {
    const onfilter = vi.fn();
    render(JobTable, {
      jobs: [job({ id: 1 }), job({ id: 2 })],
      pool: 105 * GIB, now: NOW, selected: null, onopen: () => {}, onfilter, filter: EMPTY_FILTER,
    });
    const select = screen.getByLabelText("sort") as HTMLSelectElement;
    await fireEvent.change(select, { target: { value: "id:d" } });
    expect(onfilter).toHaveBeenCalledWith({ ...EMPTY_FILTER, sort: { key: "id", desc: true } }, false);
  });

  it("feeds the tag summary every job with the tag from sourceJobs, not just the rows left after other filters narrow the table", () => {
    const jobsWithTag = [
      job({ id: 1, tags: ["x"], state: "queued" }),
      job({ id: 2, tags: ["x"], state: "queued" }),
      job({ id: 3, tags: ["x"], state: "failed", end_time: NOW - 10 }),
    ];
    render(JobTable, {
      // The table itself is narrowed (by an extra state filter) down to just the failed job...
      jobs: [jobsWithTag[2]],
      pool: 105 * GIB, now: NOW, selected: null, onopen: () => {},
      filter: { ...EMPTY_FILTER, tags: ["x"], states: ["failed"] }, total: 3,
      sourceJobs: jobsWithTag,
    });
    // ...but the tag summary strip and its bulk actions still cover the whole tag: both queued
    // jobs plus the one failed job, regardless of the state filter also in effect.
    expect(screen.getByText("cancel 2 queued")).toBeInTheDocument();
    expect(screen.getByText("retry 1 failed")).toBeInTheDocument();
  });

  it("keys the tag summary by tag, so a bulk-action result line doesn't carry over when the active tag changes", async () => {
    vi.mocked(api.cancelJob).mockResolvedValue(job({ id: 10 }));
    const jobsForA = [job({ id: 10, tags: ["a"], state: "queued" })];
    const { rerender } = render(JobTable, {
      jobs: jobsForA, pool: 105 * GIB, now: NOW, selected: null, onopen: () => {},
      filter: { ...EMPTY_FILTER, tags: ["a"] }, sourceJobs: jobsForA,
    });
    await fireEvent.click(screen.getByText("cancel 1 queued"));
    await fireEvent.click(await screen.findByText("Cancel 1 job"));
    await waitFor(() => expect(screen.getByText("cancelled 1")).toBeInTheDocument());

    const jobsForB = [job({ id: 11, tags: ["b"], state: "queued" })];
    await rerender({ jobs: jobsForB, filter: { ...EMPTY_FILTER, tags: ["b"] }, sourceJobs: jobsForB });

    expect(screen.queryByText("cancelled 1")).toBeNull();
  });

  describe("cloud jobs", () => {
    it("counts awaiting jobs in the heading, only when there are some", () => {
      const base = { pool: 105 * GIB, now: NOW, selected: null, onopen: () => {} };
      const { unmount } = render(JobTable, { ...base, jobs: [job({ id: 1, state: "running", start_time: NOW - 60 }), awaitingFresh, awaitingReapproval] });
      expect(screen.getByText("1 running · 0 queued · 2 awaiting")).toBeInTheDocument();
      unmount();
      render(JobTable, { ...base, jobs: [job({ id: 1, state: "running", start_time: NOW - 60 })] });
      expect(screen.getByText("1 running · 0 queued")).toBeInTheDocument();
    });

    it("groups awaiting cloud jobs in their own heading, above Queued and below Running", () => {
      const { container } = render(JobTable, {
        jobs: [
          job({ id: 1, state: "running", start_time: NOW - 600 }),
          awaitingFresh,
          job({ id: 2, state: "queued" }),
        ],
        pool: 105 * GIB,
        now: NOW,
        selected: null,
        onopen: () => {},
      });
      const headings = Array.from(container.querySelectorAll("tr.group td")).map((td) => td.textContent);
      expect(headings).toEqual(["Running", "Awaiting approval", "Queued"]);
    });

    it("orders multiple awaiting jobs like the queued group (bid, then queue_time)", () => {
      const { container } = render(JobTable, {
        jobs: [awaitingReapproval, awaitingFresh],
        pool: 105 * GIB,
        now: NOW,
        selected: null,
        onopen: () => {},
      });
      const table = container.querySelector("table")!;
      const names = within(table).getAllByRole("row").slice(2).map((r) => r.textContent);
      // Both bid 1000 (the fixture default); the earlier queue_time (awaitingReapproval) sorts first.
      expect(names[0]).toContain("finetune-a");
      expect(names[1]).toContain("sweep-wd-3");
    });

    it("shows a cloud badge next to the state pill on cloud rows, not on local ones", () => {
      const { container } = render(JobTable, {
        jobs: [job({ id: 1, state: "running", start_time: NOW - 600 }), awaitingFresh],
        pool: 105 * GIB,
        now: NOW,
        selected: null,
        onopen: () => {},
      });
      const rows = container.querySelectorAll("tr.row");
      const localRow = Array.from(rows).find((r) => r.querySelector(".jname")?.textContent === "job");
      const cloudRow = Array.from(rows).find((r) => r.querySelector(".jname")?.textContent === "sweep-wd-3");
      expect(localRow?.querySelector(".cloudbadge")).toBeNull();
      expect(cloudRow?.querySelector(".cloudbadge")).not.toBeNull();
      // Named for a screen reader too, not only in a hover title.
      expect(within(cloudRow as HTMLElement).getByRole("img", { name: "runs in the cloud on modal-a" })).toBeInTheDocument();
    });

    it("shows GPU · target in the memory column for a cloud job instead of GiB", () => {
      const { container } = render(JobTable, {
        jobs: [awaitingFresh],
        pool: 105 * GIB,
        now: NOW,
        selected: null,
        onopen: () => {},
      });
      const table = container.querySelector("table")!;
      expect(within(table).getByText("H100 · modal-a")).toBeInTheDocument();
    });

    it("shows elapsed vs approved cloud time for a running cloud job, not its plain estimate", () => {
      const runningCloud = cloudJobView(
        { id: 300, state: "running", start_time: NOW - 5000, run_time: 5000, est_runtime: 2 * 3600 },
        { approved_seconds: 14400, full_seconds: 14400 },
      );
      const { container } = render(JobTable, {
        jobs: [runningCloud],
        pool: 105 * GIB,
        now: NOW,
        selected: null,
        onopen: () => {},
      });
      const table = container.querySelector("table")!;
      expect(within(table).getByText(/1h23/)).toHaveTextContent("1h23 / ~4h00");
    });
  });
});
