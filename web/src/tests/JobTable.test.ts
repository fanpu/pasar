import { render, screen, fireEvent, within } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";
import JobTable from "../components/JobTable.svelte";
import { GIB, job, NOW } from "./fixtures";

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

  it("formats lost time, or a dash when nothing was lost", () => {
    render(JobTable, {
      jobs: [
        job({ id: 10, state: "completed", end_time: NOW - 60, submitter: "alice", lost: { preemption: 660, failure: 0, known: true } }),
        job({ id: 11, state: "completed", end_time: NOW - 120, submitter: "bob", lost: { preemption: 0, failure: 0, known: true } }),
      ],
      pool: 105 * GIB,
      now: NOW,
      selected: null,
      onopen: () => {},
    });
    expect(screen.getByText("11m")).toBeInTheDocument();
    expect(screen.getByText("–")).toBeInTheDocument();
  });

  it("renders '?' when total lost time is 0 and unknown", () => {
    const { container } = render(JobTable, {
      jobs: [
        job({ id: 13, state: "completed", end_time: NOW - 60, submitter: "alice", lost: { preemption: 0, failure: 0, known: false } }),
      ],
      pool: 105 * GIB,
      now: NOW,
      selected: null,
      onopen: () => {},
    });
    const table = container.querySelector("table")!;
    const lostCell = within(table).getByText("?");
    expect(lostCell).toBeInTheDocument();
    expect(lostCell).toHaveAttribute("title", "unknown: the job doesn't report checkpoints");
  });

  it("renders '11m?' when lost time is nonzero and unknown", () => {
    render(JobTable, {
      jobs: [
        job({ id: 14, state: "completed", end_time: NOW - 60, submitter: "alice", lost: { preemption: 660, failure: 0, known: false } }),
      ],
      pool: 105 * GIB,
      now: NOW,
      selected: null,
      onopen: () => {},
    });
    expect(screen.getByText("11m?")).toBeInTheDocument();
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
});
