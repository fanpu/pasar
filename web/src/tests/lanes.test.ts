import { describe, expect, it } from "vitest";
import { cloudLanes } from "../lib/lanes";
import type { EndKind } from "../lib/types";
import { job, NOW } from "./fixtures";
import { cloudJobView } from "./fixtures/cloud";

const T0 = NOW - 6 * 3600;
const T1 = NOW + 3600;

describe("cloudLanes", () => {
  it("groups attempts into one lane per account, named and sorted", () => {
    const lanes = cloudLanes([
      cloudJobView({ id: 1, state: "completed", spans: [[NOW - 3600, NOW - 1800, "completed"]] }, { target: "modal-b", owner: "Second" }),
      cloudJobView({ id: 2, state: "completed", spans: [[NOW - 5000, NOW - 4000, "completed"]] }, { target: "modal-a", owner: "First" }),
      cloudJobView({ id: 3, state: "failed", spans: [[NOW - 900, NOW - 600, "failed"]] }, { target: "modal-a", owner: "First" }),
    ], NOW, T0, T1);
    expect(lanes.map((l) => [l.target, l.owner, l.bars.map((b) => b.id)])).toEqual([
      ["modal-a", "First", [2, 3]],
      ["modal-b", "Second", [1]],
    ]);
  });

  it("draws a running attempt solid to now and its approved window after that", () => {
    const [lane] = cloudLanes([
      cloudJobView({ id: 5, state: "running", spans: [[NOW - 1200, null, null]] }, { approved_seconds: 3600 }),
    ], NOW, T0, T1);
    expect(lane.bars).toEqual([
      { id: 5, attempt: 1, kind: "run", start: NOW - 1200, end: NOW, until: NOW + 2400, row: 0, endKind: null },
    ]);
  });

  it("stops a running attempt at now once it has run past its window, or when the window is unknown", () => {
    const lanes = cloudLanes([
      cloudJobView({ id: 5, state: "running", spans: [[NOW - 7200, null, null]] }, { approved_seconds: 3600 }),
      cloudJobView({ id: 6, state: "running", spans: [[NOW - 600, null, null]] }, { approved_seconds: null, target: "modal-b" }),
    ], NOW, T0, T1);
    expect(lanes.map((l) => [l.bars[0].end, l.bars[0].until])).toEqual([[NOW, null], [NOW, null]]);
  });

  it("draws a finished attempt from its start to its end", () => {
    const [lane] = cloudLanes([
      cloudJobView({ id: 7, state: "failed", spans: [[NOW - 3000, NOW - 1000, "failed"]] }, { approved_seconds: 3600 }),
    ], NOW, T0, T1);
    expect(lane.bars).toEqual([
      { id: 7, attempt: 1, kind: "past", start: NOW - 3000, end: NOW - 1000, until: null, row: 0, endKind: "failed" },
    ]);
  });

  it("shows a paused job's attempts as separate bars with the gap between them, on one row", () => {
    const [lane] = cloudLanes([
      cloudJobView({ id: 8, state: "running", spans: [[NOW - 5000, NOW - 4000, "paused" as EndKind], [NOW - 2000, null, null]] }, { approved_seconds: 3000 }),
    ], NOW, T0, T1);
    expect(lane.rows).toBe(1);
    expect(lane.bars.map((b) => [b.attempt, b.kind, b.start, b.end])).toEqual([
      [1, "past", NOW - 5000, NOW - 4000],
      [2, "run", NOW - 2000, NOW],
    ]);
  });

  it("puts attempts that run at once on the same account on separate rows", () => {
    const [lane] = cloudLanes([
      cloudJobView({ id: 1, state: "running", spans: [[NOW - 3000, null, null]] }, { approved_seconds: 3600 }),
      cloudJobView({ id: 2, state: "running", spans: [[NOW - 1000, null, null]] }, { approved_seconds: 3600 }),
      cloudJobView({ id: 3, state: "completed", spans: [[NOW - 5000, NOW - 4000, "completed"]] }),
    ], NOW, T0, T1);
    expect(lane.rows).toBe(2);
    expect(lane.bars.map((b) => [b.id, b.row])).toEqual([[3, 0], [1, 0], [2, 1]]);
  });

  it("draws nothing for a job that has not run yet", () => {
    expect(cloudLanes([
      cloudJobView({ id: 1, state: "awaiting", spans: [] }, { approved_seconds: 3600 }),
      cloudJobView({ id: 2, state: "queued", spans: [], projected: [[NOW, NOW + 600]] }),
    ], NOW, T0, T1)).toEqual([]);
  });

  it("has no lanes when no cloud attempt is in the window, and ignores local jobs", () => {
    const old = cloudJobView({ id: 1, state: "completed", spans: [[NOW - 9 * 3600, NOW - 8 * 3600, "completed"]] });
    const local = job({ id: 2, state: "running", spans: [[NOW - 600, null, null]] });
    expect(cloudLanes([old, local], NOW, T0, T1)).toEqual([]);
    expect(cloudLanes([old], NOW, NOW - 10 * 3600, T1)).toHaveLength(1);
  });

  it("keeps a lane for a running attempt whose approved window reaches into view", () => {
    const run = cloudJobView({ id: 1, state: "running", spans: [[NOW - 600, null, null]] }, { approved_seconds: 7200 });
    expect(cloudLanes([run], NOW, NOW + 1800, NOW + 3600)).toHaveLength(1);
    expect(cloudLanes([run], NOW, NOW + 7000, NOW + 9000)).toEqual([]);
  });
});
