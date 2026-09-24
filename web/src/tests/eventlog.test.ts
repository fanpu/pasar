import { describe, expect, it } from "vitest";
import { eventRows } from "../lib/eventlog";
import { attempt, jobDetail, NOW } from "./fixtures";
import type { JobEvent } from "../lib/types";

describe("eventRows", () => {
  it("builds newest-first rows from attempts and events, breaking ties toward the lifecycle row", () => {
    const detail = jobDetail({
      id: 42,
      submit_time: NOW - 5000,
      submitter: "alice",
      attempts: [
        attempt({
          n: 1, job_id: 42, start_time: NOW - 4000, end_time: NOW - 3000,
          end_kind: "preempted", wasted_work: 360,
        }),
        attempt({
          n: 2, job_id: 42, start_time: NOW - 2900, end_time: NOW - 100,
          end_kind: "failed", reason: "gpu_oom",
          summary: "CUDA out of memory. Tried to allocate 2.00 GiB",
          restart_cost: 300,
        }),
      ],
    });
    const events: JobEvent[] = [
      { attempt: 1, ts: NOW - 3500, kind: "checkpoint", step: 3400, payload: {} },
      { attempt: 2, ts: NOW - 2900, kind: "resumed", step: 3900, payload: {} },
      { attempt: 2, ts: NOW - 2000, kind: "note", step: null, payload: { text: "switched to max-len 131072" } },
      {
        attempt: 2, ts: NOW - 1500, kind: "progress", step: 4200,
        payload: { step: 4200, total_steps: 10000, loss: 1.5, ts: NOW - 1500 },
      },
    ];

    expect(eventRows(detail, events)).toEqual([
      { ts: NOW - 100, icon: "✕", text: "GPU out of memory: CUDA out of memory. Tried to allocate 2.00 GiB" },
      { ts: NOW - 2000, icon: "📝", text: "switched to max-len 131072" },
      { ts: NOW - 2900, icon: "▶", text: "attempt 2 started" },
      { ts: NOW - 2900, icon: "▶", text: "resumed · step 3900 (restart cost 5m)" },
      { ts: NOW - 3000, icon: "⏸", text: "preempted · 6m unsaved work" },
      { ts: NOW - 3500, icon: "💾", text: "checkpoint · step 3400" },
      { ts: NOW - 4000, icon: "▶", text: "attempt 1 started" },
      { ts: NOW - 5000, icon: "◷", text: "submitted by alice" },
    ]);
  });

  it("says why a cloud attempt paused and that it waits for approval", () => {
    const detail = jobDetail({
      id: 9,
      submit_time: NOW - 5000,
      submitter: "",
      attempts: [
        attempt({ n: 1, job_id: 9, start_time: NOW - 4000, end_time: NOW - 3000, end_kind: "paused", reason: "time_limit" }),
        attempt({ n: 2, job_id: 9, start_time: NOW - 2000, end_time: NOW - 1000, end_kind: "paused", reason: "cloud_preempted" }),
      ],
    });
    const rows = eventRows(detail, []);
    expect(rows).toContainEqual({ ts: NOW - 3000, icon: "⏸", text: "attempt 1 paused · out of approved time · waiting for approval" });
    expect(rows).toContainEqual({ ts: NOW - 1000, icon: "⏸", text: "attempt 2 paused · taken back by the provider · waiting for approval" });
  });

  it("falls back to bare labels with no submitter, no step and no restart cost", () => {
    const detail = jobDetail({
      id: 7,
      submit_time: NOW - 1000,
      submitter: "",
      attempts: [
        attempt({ n: 1, job_id: 7, start_time: NOW - 900, end_time: NOW - 800, end_kind: "cancelled" }),
      ],
    });
    const events: JobEvent[] = [
      { attempt: 1, ts: NOW - 850, kind: "checkpoint", step: null, payload: {} },
      { attempt: 1, ts: NOW - 820, kind: "resumed", step: null, payload: {} },
    ];

    expect(eventRows(detail, events).map((r) => r.text)).toEqual([
      "cancelled",
      "resumed",
      "checkpoint",
      "attempt 1 started",
      "submitted",
    ]);
  });

  it("marks a completed attempt with a check and a failed attempt with just the label when the summary is empty", () => {
    const detail = jobDetail({
      id: 3,
      submit_time: NOW - 200,
      attempts: [
        attempt({ n: 1, job_id: 3, start_time: NOW - 190, end_time: NOW - 100, end_kind: "completed" }),
        attempt({
          n: 2, job_id: 3, start_time: NOW - 90, end_time: NOW - 10,
          end_kind: "failed", reason: "signal", summary: "",
        }),
      ],
    });
    const rows = eventRows(detail, []);
    expect(rows[0]).toEqual({ ts: NOW - 10, icon: "✕", text: "killed by a signal" });
    const completedRow = rows.find((r) => r.text === "completed");
    expect(completedRow).toEqual({ ts: NOW - 100, icon: "✓", text: "completed" });
  });
});
