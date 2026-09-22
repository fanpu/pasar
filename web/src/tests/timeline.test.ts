import { describe, expect, it } from "vitest";
import { layout, stack, timeTicks } from "../lib/timeline";
import { GIB, job, NOW } from "./fixtures";

const M = 60;
const pool = 105 * GIB;

describe("timeline layout", () => {
  it("reproduces the reference schedule", () => {
    const jobs = [
      job({ id: 42, state: "running", limit: 37.4 * GIB, usage: 31.2 * GIB, spans: [[NOW - 134 * M, null, null]], projected: [[NOW - 134 * M, NOW + 76 * M]] }),
      job({ id: 45, state: "running", limit: 26.4 * GIB, usage: 19.1 * GIB, spans: [[NOW - 9 * M, null, null]], projected: [[NOW - 9 * M, NOW + 21 * M]] }),
      job({ id: 48, state: "queued", limit: 44 * GIB, projected: [[NOW + 21 * M, NOW + 66 * M]] }),
      job({ id: 46, state: "queued", mode: "whole", mem_request: null, limit: pool, projected: [[NOW + 76 * M, NOW + 166 * M]] }),
      job({ id: 44, state: "completed", limit: 22 * GIB, spans: [[NOW - 94 * M, NOW - 46 * M, "completed"]] }),
    ];
    const got = Object.fromEntries(layout(jobs, pool, NOW).map((b) => [`${b.id}:${b.kind}`, [b.lo / GIB, b.hi / GIB]]));
    expect(got["42:run"][0]).toBe(0);
    expect(got["42:run"][1]).toBeCloseTo(37.4);
    expect(got["44:past"][0]).toBeCloseTo(37.4);
    expect(got["45:run"][0]).toBeCloseTo(37.4);
    expect(got["48:proj"][0]).toBeCloseTo(37.4);
    expect(got["46:proj"]).toEqual([0, 105]);
  });

  it("running block height uses usage when over the limit, capped at the pool", () => {
    const [b] = layout([job({ id: 1, state: "running", limit: 6 * GIB, usage: 8 * GIB, spans: [[NOW - 60, null, null]], projected: [[NOW - 60, NOW + 600]] })], pool, NOW);
    expect(b.hi - b.lo).toBe(8 * GIB);
  });

  it("running job without a projection runs to its estimate", () => {
    const [b] = layout([job({ id: 1, state: "running", est_runtime: 3600, spans: [[NOW - 600, null, null]] })], pool, NOW);
    expect(b.end).toBe(NOW + 3000);
  });

  it("drops history that ended before the window", () => {
    expect(layout([job({ state: "completed", spans: [[NOW - 5 * 3600, NOW - 4 * 3600, "completed"]] })], pool, NOW)).toEqual([]);
  });

  it("projected preemption shows as a later projected segment", () => {
    const bs = layout([job({ id: 7, state: "running", spans: [[NOW - 60, null, null]], projected: [[NOW - 60, NOW + 600], [NOW + 1800, NOW + 2400]] })], pool, NOW);
    expect(bs.map((b) => [b.kind, b.start])).toEqual([["run", NOW - 60], ["proj", NOW + 1800]]);
  });

  it("stack overflows by pinning to the top", () => {
    const bs = stack([
      { id: 1, kind: "run", start: 0, end: 10, lo: 0, hi: 80 },
      { id: 2, kind: "run", start: 1, end: 10, lo: 0, hi: 50 },
    ], 100);
    expect(bs.find((b) => b.id === 2)).toMatchObject({ lo: 50, hi: 100 });
  });

  it("ticks are whole hours inside the window", () => {
    const t = timeTicks(NOW - 90 * M, NOW + 240 * M, 600);
    expect(t.every((x) => x % 3600 === 0 && x >= NOW - 90 * M && x <= NOW + 240 * M)).toBe(true);
    expect(t.length).toBe(6);  // 12:56–18:26 contains 13:00 … 18:00
  });
});
