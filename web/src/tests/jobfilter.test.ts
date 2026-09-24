import { describe, expect, it } from "vitest";
import {
  EMPTY_FILTER, effectiveSort, filterToSearch, isActive, knownValues, matches, parseFilter, sortJobs,
  stateCounts, tagSummary, type Filter,
} from "../lib/jobfilter";
import { job } from "./fixtures";

describe("parseFilter / filterToSearch", () => {
  it("parses all fields, with or without a leading ?", () => {
    const search = "?q=lr&state=failed&state=running&tag=a&tag=b&by=x&sort=-ended";
    const expected: Filter = {
      q: "lr", states: ["failed", "running"], tags: ["a", "b"], by: ["x"], sort: { key: "ended", desc: true },
    };
    expect(parseFilter(search)).toEqual(expected);
    expect(parseFilter(search.slice(1))).toEqual(expected);
  });

  it("defaults to EMPTY_FILTER for an empty search", () => {
    expect(parseFilter("")).toEqual(EMPTY_FILTER);
    expect(parseFilter("?")).toEqual(EMPTY_FILTER);
  });

  it("ignores unknown params and invalid values", () => {
    expect(parseFilter("?state=bogus&tag=&by=&sort=nope&extra=1")).toEqual(EMPTY_FILTER);
  });

  it("drops duplicate values", () => {
    expect(parseFilter("?tag=a&tag=a&state=running&state=running").tags).toEqual(["a"]);
  });

  it("parses a sort key with and without the leading -", () => {
    expect(parseFilter("?sort=bogus").sort).toBeNull();
    expect(parseFilter("?sort=name").sort).toEqual({ key: "name", desc: false });
    expect(parseFilter("?sort=-name").sort).toEqual({ key: "name", desc: true });
  });

  it("round-trips through filterToSearch", () => {
    const f = parseFilter("?q=lr&state=failed&state=running&tag=a&tag=b&by=x&sort=-ended");
    expect(parseFilter(filterToSearch(f))).toEqual(f);
  });

  it("serializes the empty filter to '', and otherwise with stable param order q,state,tag,by,sort", () => {
    expect(filterToSearch(EMPTY_FILTER)).toBe("");
    const f: Filter = { q: "lr", states: ["running"], tags: ["a"], by: ["x"], sort: { key: "id", desc: true } };
    expect(filterToSearch(f)).toBe("?q=lr&state=running&tag=a&by=x&sort=-id");
  });
});

describe("isActive", () => {
  it("is false only for the empty filter", () => {
    expect(isActive(EMPTY_FILTER)).toBe(false);
    expect(isActive({ ...EMPTY_FILTER, q: "x" })).toBe(true);
    expect(isActive({ ...EMPTY_FILTER, states: ["running"] })).toBe(true);
    expect(isActive({ ...EMPTY_FILTER, tags: ["a"] })).toBe(true);
    expect(isActive({ ...EMPTY_FILTER, by: ["a"] })).toBe(true);
    expect(isActive({ ...EMPTY_FILTER, sort: { key: "id", desc: true } })).toBe(true);
  });
});

describe("matches", () => {
  it("q matches #id, id, name, command, note case-insensitively", () => {
    const j = job({ id: 42, name: "train-lr", command: "python train.py", note: "sweep note" });
    expect(matches(j, { ...EMPTY_FILTER, q: "#42" })).toBe(true);
    expect(matches(j, { ...EMPTY_FILTER, q: "42" })).toBe(true);
    expect(matches(j, { ...EMPTY_FILTER, q: "TRAIN-LR" })).toBe(true);
    expect(matches(j, { ...EMPTY_FILTER, q: "train.py" })).toBe(true);
    expect(matches(j, { ...EMPTY_FILTER, q: "sweep" })).toBe(true);
    expect(matches(j, { ...EMPTY_FILTER, q: "nope" })).toBe(false);
  });

  it("a running state filter also matches stopping jobs", () => {
    const running = job({ state: "running" });
    const stopping = job({ state: "stopping" });
    const queued = job({ state: "queued" });
    expect(matches(running, { ...EMPTY_FILTER, states: ["running"] })).toBe(true);
    expect(matches(stopping, { ...EMPTY_FILTER, states: ["running"] })).toBe(true);
    expect(matches(queued, { ...EMPTY_FILTER, states: ["running"] })).toBe(false);
  });

  it("an awaiting cloud job is its own bucket, not folded into queued", () => {
    const awaiting = job({ state: "awaiting" });
    expect(matches(awaiting, { ...EMPTY_FILTER, states: ["awaiting"] })).toBe(true);
    expect(matches(awaiting, { ...EMPTY_FILTER, states: ["queued"] })).toBe(false);
  });

  it("tags require ALL listed, by requires ANY listed", () => {
    const j = job({ tags: ["a", "b"], submitter: "opus" });
    expect(matches(j, { ...EMPTY_FILTER, tags: ["a"] })).toBe(true);
    expect(matches(j, { ...EMPTY_FILTER, tags: ["a", "b"] })).toBe(true);
    expect(matches(j, { ...EMPTY_FILTER, tags: ["a", "c"] })).toBe(false);
    expect(matches(j, { ...EMPTY_FILTER, by: ["opus"] })).toBe(true);
    expect(matches(j, { ...EMPTY_FILTER, by: ["someone-else", "opus"] })).toBe(true);
    expect(matches(j, { ...EMPTY_FILTER, by: ["someone-else"] })).toBe(false);
  });
});

describe("effectiveSort", () => {
  it("defaults to -id when no sort is set", () => {
    expect(effectiveSort(EMPTY_FILTER)).toEqual({ key: "id", desc: true });
    expect(effectiveSort({ ...EMPTY_FILTER, sort: { key: "name", desc: false } })).toEqual({ key: "name", desc: false });
  });
});

describe("sortJobs", () => {
  it("sorts by id, ascending and descending", () => {
    const jobs = [job({ id: 3 }), job({ id: 1 }), job({ id: 2 })];
    expect(sortJobs(jobs, { key: "id", desc: false }).map((j) => j.id)).toEqual([1, 2, 3]);
    expect(sortJobs(jobs, { key: "id", desc: true }).map((j) => j.id)).toEqual([3, 2, 1]);
  });

  it("sorts by name with localeCompare", () => {
    const jobs = [job({ id: 1, name: "banana" }), job({ id: 2, name: "Apple" }), job({ id: 3, name: "cherry" })];
    expect(sortJobs(jobs, { key: "name", desc: false }).map((j) => j.name)).toEqual(["Apple", "banana", "cherry"]);
  });

  it("sorts by state in running, stopping, queued, failed, completed, cancelled order", () => {
    const jobs = [
      job({ id: 1, state: "cancelled" }), job({ id: 2, state: "completed" }), job({ id: 3, state: "failed" }),
      job({ id: 4, state: "queued" }), job({ id: 5, state: "stopping" }), job({ id: 6, state: "running" }),
    ];
    expect(sortJobs(jobs, { key: "state", desc: false }).map((j) => j.state)).toEqual([
      "running", "stopping", "queued", "failed", "completed", "cancelled",
    ]);
  });

  it("sorts by bid and by memory (limit)", () => {
    const jobs = [job({ id: 1, bid: 500, limit: 3 }), job({ id: 2, bid: 1500, limit: 1 })];
    expect(sortJobs(jobs, { key: "bid", desc: false }).map((j) => j.id)).toEqual([1, 2]);
    expect(sortJobs(jobs, { key: "memory", desc: false }).map((j) => j.id)).toEqual([2, 1]);
  });

  it("sorts by time: run_time once started, est_runtime while queued", () => {
    const started = job({ id: 1, start_time: 100, run_time: 50, est_runtime: 999 });
    const queued = job({ id: 2, start_time: null, run_time: 0, est_runtime: 10 });
    expect(sortJobs([started, queued], { key: "time", desc: false }).map((j) => j.id)).toEqual([2, 1]);
  });

  it("sorts by submitted (submit_time) and by (submitter)", () => {
    const jobs = [job({ id: 1, submit_time: 200, submitter: "bob" }), job({ id: 2, submit_time: 100, submitter: "alice" })];
    expect(sortJobs(jobs, { key: "submitted", desc: false }).map((j) => j.id)).toEqual([2, 1]);
    expect(sortJobs(jobs, { key: "by", desc: false }).map((j) => j.id)).toEqual([2, 1]);
  });

  it("sorts by ended (end_time) with nulls last, regardless of direction", () => {
    const jobs = [job({ id: 1, end_time: 100 }), job({ id: 2, end_time: null }), job({ id: 3, end_time: 50 })];
    expect(sortJobs(jobs, { key: "ended", desc: false }).map((j) => j.id)).toEqual([3, 1, 2]);
    expect(sortJobs(jobs, { key: "ended", desc: true }).map((j) => j.id)).toEqual([1, 3, 2]);
  });

  it("breaks ties by id descending", () => {
    const jobs = [job({ id: 1, bid: 100 }), job({ id: 5, bid: 100 }), job({ id: 3, bid: 100 })];
    expect(sortJobs(jobs, { key: "bid", desc: false }).map((j) => j.id)).toEqual([5, 3, 1]);
  });

  it("returns a new array, leaving the input untouched", () => {
    const jobs = [job({ id: 2 }), job({ id: 1 })];
    const sorted = sortJobs(jobs, { key: "id", desc: false });
    expect(sorted).not.toBe(jobs);
    expect(jobs.map((j) => j.id)).toEqual([2, 1]);
  });
});

describe("stateCounts", () => {
  it("counts jobs matching the other filters, ignoring f.states itself", () => {
    const jobs = [
      job({ id: 1, state: "running", tags: ["x"] }),
      job({ id: 2, state: "stopping", tags: ["x"] }),
      job({ id: 3, state: "queued", tags: ["x"] }),
      job({ id: 4, state: "completed", tags: ["y"] }),
      job({ id: 5, state: "failed", tags: ["x"] }),
      job({ id: 6, state: "cancelled", tags: ["x"] }),
      job({ id: 7, state: "awaiting", tags: ["x"] }),
    ];
    const f: Filter = { ...EMPTY_FILTER, tags: ["x"], states: ["completed"] };
    expect(stateCounts(jobs, f)).toEqual({ running: 2, awaiting: 1, queued: 1, completed: 0, failed: 1, cancelled: 1 });
  });
});

describe("knownValues", () => {
  it("returns sorted, unique, non-empty tags and submitters", () => {
    const jobs = [
      job({ tags: ["b", "a"], submitter: "opus" }),
      job({ tags: ["a"], submitter: "" }),
      job({ tags: [], submitter: "opus" }),
      job({ tags: ["c"], submitter: "haiku" }),
    ];
    expect(knownValues(jobs)).toEqual({ tags: ["a", "b", "c"], by: ["haiku", "opus"] });
  });
});

describe("tagSummary", () => {
  it("computes counts, gpu/queued seconds, and estimate accuracy", () => {
    const jobs = [
      job({ id: 1, state: "running", run_time: 30 }),
      job({ id: 2, state: "stopping", run_time: 5 }),
      job({ id: 3, state: "queued", est_runtime: 40 }),
      job({ id: 4, state: "queued", est_runtime: 20 }),
      job({ id: 5, state: "completed", run_time: 100, est_runtime: 90 }),
      job({ id: 6, state: "completed", run_time: 120, est_runtime: 110 }),
      job({ id: 7, state: "failed" }),
      job({ id: 8, state: "cancelled" }),
    ];
    const s = tagSummary(jobs);
    expect(s.total).toBe(8);
    expect(s.running).toBe(2);
    expect(s.queued).toBe(2);
    expect(s.completed).toBe(2);
    expect(s.failed).toBe(1);
    expect(s.cancelled).toBe(1);
    expect(s.gpuSeconds).toBe(30 + 5 + 100 + 120);
    expect(s.queuedSeconds).toBe(60);
    expect(s.medianRun).toBe(110);
    expect(s.medianEstimate).toBe(100);
    expect(s.estimateOk).toBe(true);
  });

  it("estimateOk is false outside 20%, and null when there are no completed jobs", () => {
    const jobs = [job({ id: 1, state: "completed", run_time: 200, est_runtime: 100 })];
    expect(tagSummary(jobs).estimateOk).toBe(false);
    const empty = tagSummary([]);
    expect(empty.medianRun).toBeNull();
    expect(empty.medianEstimate).toBeNull();
    expect(empty.estimateOk).toBeNull();
  });
});
