import { describe, expect, it } from "vitest";
import { transitions } from "../lib/transitions";
import { job, NOW } from "./fixtures";

const kinds = (prev: any[] | null, next: any[]) => transitions(prev, next, NOW).map((t) => [t.kind, t.job.id]);

describe("transitions", () => {
  it("first snapshot yields nothing", () => expect(kinds(null, [job({ state: "running" })])).toEqual([]));

  it("start", () =>
    expect(kinds([job({ id: 1 })], [job({ id: 1, state: "running" })])).toEqual([["started", 1]]));

  it("new job already running counts as started", () =>
    expect(kinds([], [job({ id: 2, state: "running" })])).toEqual([["started", 2]]));

  it("new job that isn't running yet counts as nothing", () =>
    expect(kinds([], [job({ id: 2, state: "queued" })])).toEqual([]));

  it("completed", () =>
    expect(kinds([job({ state: "running" })], [job({ state: "completed" })])).toEqual([["completed", 1]]));

  it("oom variants", () => {
    for (const reason of ["oom", "gpu_oom", "kernel_oom"]) {
      expect(kinds([job({ state: "running" })], [job({ state: "failed", reason })])).toEqual([["oom", 1]]);
    }
  });

  it("lost", () =>
    expect(kinds([job({ state: "running" })], [job({ state: "failed", reason: "lost" })])).toEqual([
      ["lost", 1],
    ]));

  it("crash", () =>
    expect(kinds([job({ state: "running" })], [job({ state: "failed", reason: "exit" })])).toEqual([
      ["failed", 1],
    ]));

  it("cancelled", () =>
    expect(kinds([job({ state: "stopping" })], [job({ state: "cancelled" })])).toEqual([["cancelled", 1]]));

  it("preempted", () =>
    expect(kinds([job({ state: "stopping" })], [job({ state: "queued", reason: "preempted" })])).toEqual([
      ["preempted", 1],
    ]));

  it("retry after a crash", () =>
    expect(kinds([job({ state: "running" })], [job({ state: "queued", reason: "gpu_oom" })])).toEqual([
      ["oom", 1],
    ]));

  it("no change", () =>
    expect(kinds([job({ state: "running" })], [job({ state: "running", bid: 5 })])).toEqual([]));

  it("output order follows next", () => {
    expect(
      kinds(
        [job({ id: 1, state: "running" }), job({ id: 2, state: "running" })],
        [job({ id: 2, state: "completed" }), job({ id: 1, state: "completed" })],
      ),
    ).toEqual([
      ["completed", 2],
      ["completed", 1],
    ]);
  });
});
