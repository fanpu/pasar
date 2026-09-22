import { describe, expect, it } from "vitest";
import { progressSeries } from "../lib/series";
import { NOW } from "./fixtures";
import type { JobEvent } from "../lib/types";

function progressEvent(ts: number, payload: Record<string, unknown>): JobEvent {
  return { attempt: 1, ts, kind: "progress", step: null, payload };
}

describe("progressSeries", () => {
  it("puts loss first, sorts the rest alphabetically, drops step/total_steps/ts and non-numeric values, and caps at 4 series", () => {
    const events: JobEvent[] = [
      progressEvent(NOW - 20, {
        step: 100, total_steps: 1000, ts: NOW - 20, loss: 2.1, lr: 3e-5, tok_s: 40000, grad_norm: 1.2, extra: 5,
      }),
      progressEvent(NOW - 10, {
        step: 200, total_steps: 1000, ts: NOW - 10, loss: 1.9, lr: 2.9e-5, tok_s: 41000, grad_norm: 1.1, extra: 6,
        bad: NaN, label: "x",
      }),
    ];

    const result = progressSeries(events);

    expect(Object.keys(result)).toEqual(["loss", "extra", "grad_norm", "lr"]);
    expect(result.loss).toEqual([[NOW - 20, 2.1], [NOW - 10, 1.9]]);
    expect(result.extra).toEqual([[NOW - 20, 5], [NOW - 10, 6]]);
    expect(result).not.toHaveProperty("tok_s");
    expect(result).not.toHaveProperty("bad");
    expect(result).not.toHaveProperty("label");
    expect(result).not.toHaveProperty("step");
    expect(result).not.toHaveProperty("total_steps");
  });

  it("orders points chronologically even when events arrive out of order", () => {
    const events: JobEvent[] = [
      progressEvent(NOW - 5, { loss: 1.0 }),
      progressEvent(NOW - 15, { loss: 2.0 }),
    ];
    expect(progressSeries(events).loss).toEqual([[NOW - 15, 2.0], [NOW - 5, 1.0]]);
  });

  it("ignores non-progress events and events with no numeric payload", () => {
    const events: JobEvent[] = [
      { attempt: 1, ts: NOW, kind: "note", step: null, payload: { loss: 1 } },
      progressEvent(NOW, {}),
    ];
    expect(progressSeries(events)).toEqual({});
  });
});
